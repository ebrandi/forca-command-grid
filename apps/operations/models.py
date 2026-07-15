"""Operations & war planner (PRD Module N).

Leadership declares an objective (a deployment, home defence, structure timer,
doctrine rollout...) with a target date and the doctrines it needs. The system
scores readiness against that objective and turns the gaps into prep tasks;
each pilot sees their own "prep for this op" checklist.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy

from apps.doctrines.models import Doctrine
from core.mixins import TimeStampedModel


class Operation(TimeStampedModel):
    class Type(models.TextChoices):
        # Combat / fleet types (added for the fleet planner).
        PVP = "pvp", gettext_lazy("PvP fleet")
        ROAM = "roam", gettext_lazy("Roaming gang")
        GATECAMP = "gatecamp", gettext_lazy("Gate camp")
        RATTING = "ratting", gettext_lazy("Ratting fleet")
        MINING = "mining", gettext_lazy("Mining operation")
        LOGISTICS = "logistics", gettext_lazy("Transport / logistics")
        # Original planning types (kept for backwards compatibility).
        DEPLOYMENT = "deployment", gettext_lazy("Deployment")
        WAR_PREP = "war_prep", gettext_lazy("War preparation")
        HOME_DEFENCE = "home_defence", gettext_lazy("Home defence")
        STRUCTURE_TIMER = "structure_timer", gettext_lazy("Structure timer")
        DOCTRINE_ROLLOUT = "doctrine_rollout", gettext_lazy("Doctrine rollout")
        INDUSTRIAL = "industrial", gettext_lazy("Industrial campaign")

    # Types that are combat in nature → SRP coverage is relevant and prompted for.
    PVP_TYPES = frozenset({"pvp", "roam", "gatecamp", "home_defence", "war_prep", "deployment"})

    class Status(models.TextChoices):
        DRAFT = "draft", gettext_lazy("Draft")
        PLANNED = "planned", gettext_lazy("Scheduled")
        ACTIVE = "active", gettext_lazy("Active")
        DONE = "done", gettext_lazy("Completed")
        CANCELLED = "cancelled", gettext_lazy("Cancelled (manual)")
        CANCELLED_AUTO = "cancelled_auto", gettext_lazy("Cancelled — too few sign-ups")

    class Srp(models.TextChoices):
        ALLIANCE = "alliance", gettext_lazy("Alliance SRP")
        CORP = "corp", gettext_lazy("Corp SRP")
        ORGANISER = "organiser", gettext_lazy("Organiser-funded SRP")
        NONE = "none", gettext_lazy("No SRP coverage")

    name = models.CharField(max_length=200)
    type = models.CharField(max_length=20, choices=Type.choices, default=Type.PVP)
    target_at = models.DateTimeField(null=True, blank=True)
    duration_minutes = models.PositiveIntegerField(
        null=True, blank=True, help_text=gettext_lazy("Expected duration in minutes.")
    )
    staging_location_id = models.BigIntegerField(null=True, blank=True)
    formup = models.CharField(max_length=200, blank=True, help_text=gettext_lazy("Form-up / staging location."))
    destination = models.CharField(max_length=200, blank=True, help_text=gettext_lazy("Destination or target area."))
    comms = models.CharField(max_length=200, blank=True, help_text=gettext_lazy("Comms channel / Mumble / Discord voice."))
    link = models.CharField(max_length=500, blank=True, help_text=gettext_lazy("Doctrine, fitting or external link."))
    notes = models.TextField(blank=True)

    # Minimum-participation requirement.
    min_pilots = models.PositiveIntegerField(default=0, help_text=gettext_lazy("Confirmed pilots needed to run."))
    rsvp_deadline = models.DateTimeField(
        null=True, blank=True, help_text=gettext_lazy("Sign-up cut-off (EVE/UTC); must be before form-up.")
    )
    rsvp_offset_minutes = models.PositiveIntegerField(
        null=True, blank=True,
        help_text=gettext_lazy("If set, the deadline tracks this many minutes before form-up."),
    )

    srp = models.CharField(max_length=10, choices=Srp.choices, blank=True)
    requirements_overridden = models.BooleanField(
        default=False, help_text=gettext_lazy("Organiser confirmed the op runs even if the minimum isn't met.")
    )
    override_note = models.CharField(max_length=200, blank=True)

    fc = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", help_text=gettext_lazy("Fleet commander / organiser."),
    )
    status = models.CharField(
        max_length=15, choices=Status.choices, default=Status.PLANNED, db_index=True
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # OPS-4 (3.12): set when this op was materialised from a recurring template, so the beat
    # can dedupe (template, target_at) and never spawn the same instance twice.
    recurring_template = models.ForeignKey(
        "OperationTemplate", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="instances",
    )

    class Meta:
        ordering = ["target_at", "-created_at"]
        indexes = [
            # Supports the SRP fleet-op gate's per-loss window lookup (srp + target_at range).
            models.Index(fields=["srp", "target_at"], name="op_srp_target_idx"),
        ]
        constraints = [
            # OPS-4 (3.12): one instance per (template, form-up time) so two racing beats /
            # a manual "materialise now" can't double-spawn. Partial — manual ops (NULL
            # template) are never constrained.
            models.UniqueConstraint(
                fields=["recurring_template", "target_at"],
                condition=models.Q(recurring_template__isnull=False),
                name="op_template_target_uniq",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    # Statuses that close an op to further planning / sign-ups.
    CLOSED_STATUSES = frozenset({"done", "cancelled", "cancelled_auto"})

    @property
    def is_upcoming(self) -> bool:
        return self.status in (self.Status.DRAFT, self.Status.PLANNED, self.Status.ACTIVE)

    @property
    def is_cancelled(self) -> bool:
        return self.status in (self.Status.CANCELLED, self.Status.CANCELLED_AUTO)

    @property
    def is_open_for_signup(self) -> bool:
        """Pilots may still claim ship slots: scheduled/active and not past the deadline."""
        from django.utils import timezone

        if self.status not in (self.Status.PLANNED, self.Status.ACTIVE):
            return False
        if self.rsvp_deadline and timezone.now() >= self.rsvp_deadline:
            return False
        return True

    @property
    def is_pvp(self) -> bool:
        return self.type in self.PVP_TYPES

    @property
    def effective_fc(self):
        return self.fc or self.created_by


class OperationShipSlot(models.Model):
    """A doctrine ship the organiser wants on the fleet, with min/max and priority.

    Distinct from :class:`OperationDoctrine` (which references whole Doctrine
    objects for the readiness score): this is the concrete *fleet composition* —
    "I need 3 Logi, 2 tackle, 1 booster" — that pilots claim a place in.
    """

    class Role(models.TextChoices):
        # Fleet-role jargon labels (DPS, logi, tackle, scout, EWAR, command ship, …)
        # stay English by policy; only the generic "Other" fallback is translated.
        DPS = "dps", "DPS"
        LOGI = "logi", "Logistics"
        TACKLE = "tackle", "Tackle"
        SCOUT = "scout", "Scout"
        BOOSTER = "booster", "Booster"
        HAULER = "hauler", "Hauler"
        MINER = "miner", "Miner"
        COMMAND = "command", "Command ship"
        EWAR = "ewar", "EWAR"
        OTHER = "other", gettext_lazy("Other")

    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="ship_slots")
    ship_name = models.CharField(max_length=200)
    ship_type_id = models.BigIntegerField(null=True, blank=True)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.DPS)
    min_pilots = models.PositiveIntegerField(default=1, help_text=gettext_lazy("Pilots required on this ship."))
    max_pilots = models.PositiveIntegerField(
        null=True, blank=True, help_text=gettext_lazy("Optional hard cap (blank = no cap).")
    )
    priority = models.PositiveIntegerField(default=1, help_text=gettext_lazy("1 = most needed; shown first."))
    # A slot is either an official doctrine ship (link the pilots can open) …
    doctrine_fit = models.ForeignKey(
        "doctrines.DoctrineFit", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        help_text=gettext_lazy("The doctrine fit this slot is for, if it's an official doctrine ship."),
    )
    # … or a one-off custom ship with a pasted EFT the pilots can read.
    eft_text = models.TextField(blank=True, help_text=gettext_lazy("EFT for a non-doctrine (custom) ship."))
    fitting_link = models.CharField(max_length=500, blank=True)
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self) -> str:
        return f"{self.ship_name} ×{self.min_pilots} ({self.get_role_display()})"

    @property
    def is_doctrine(self) -> bool:
        return self.doctrine_fit_id is not None

    @property
    def doctrine_id(self):
        return self.doctrine_fit.doctrine_id if self.doctrine_fit_id else None

    @property
    def doctrine_name(self) -> str:
        return self.doctrine_fit.doctrine.name if self.doctrine_fit_id else ""


class OperationCommitment(TimeStampedModel):
    """A pilot's place in the fleet: the ship they'll fly + how firm it is.

    A pilot signs up by choosing one of the requested ships and saying whether
    they're definitely *coming* or only *maybe*. Only ``YES`` commitments count
    toward the minimum / viability; ``MAYBE`` is a soft signal. This is the
    *before-the-fleet* record (separate from ``OperationAttendance``, the PAP
    recorded after). One row per pilot per op; changing ship or answer updates it.
    """

    class Response(models.TextChoices):
        YES = "yes", gettext_lazy("Coming")
        MAYBE = "maybe", gettext_lazy("Maybe")

    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="commitments")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="op_commitments"
    )
    slot = models.ForeignKey(
        OperationShipSlot, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="commitments",
    )
    response = models.CharField(max_length=5, choices=Response.choices, default=Response.YES)
    character_name = models.CharField(max_length=200, blank=True)
    # When the one T-minus form-up reminder was sent for this commitment (NULL = not yet).
    # Keyed on the commitment, so it survives the slot delete/recreate on an op edit
    # (``slot`` is ``SET_NULL`` and gets a new PK, but the commitment row is stable).
    reminder_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("operation", "user")
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.character_name or self.user_id} → {self.slot_id} @ {self.operation_id}"

    @property
    def is_maybe(self) -> bool:
        return self.response == self.Response.MAYBE


class OperationCancellation(models.Model):
    """An immutable snapshot of why an operation was cancelled, for later analysis.

    Recorded for both manual and automatic cancellations. The composition snapshots
    are denormalised (and the FK is SET_NULL) so the record stays meaningful even if
    the operation is later deleted.
    """

    class Reason(models.TextChoices):
        INSUFFICIENT = "insufficient_signups", gettext_lazy("Too few sign-ups")
        COMPOSITION = "composition_unmet", gettext_lazy("Doctrine composition not met")
        MANUAL = "manual", gettext_lazy("Cancelled by organiser")

    operation = models.ForeignKey(
        Operation, on_delete=models.SET_NULL, null=True, blank=True, related_name="cancellations"
    )
    operation_pk = models.IntegerField(db_index=True)
    operation_type = models.CharField(max_length=20)
    organiser_name = models.CharField(max_length=200, blank=True)
    scheduled_start = models.DateTimeField(null=True, blank=True)
    rsvp_deadline = models.DateTimeField(null=True, blank=True)
    min_pilots = models.PositiveIntegerField(default=0)
    confirmed_at_deadline = models.PositiveIntegerField(default=0)
    required_composition = models.JSONField(default=dict, blank=True)
    actual_composition = models.JSONField(default=dict, blank=True)
    reason = models.CharField(max_length=24, choices=Reason.choices)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"cancel op#{self.operation_pk} ({self.reason})"


class OperationDoctrine(models.Model):
    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="doctrines")
    doctrine = models.ForeignKey(Doctrine, on_delete=models.CASCADE, related_name="+")
    target_count = models.IntegerField(default=0, help_text=gettext_lazy("Pilots wanted on this doctrine (0 = any)."))

    class Meta:
        unique_together = ("operation", "doctrine")


class OperationAttendance(TimeStampedModel):
    """A pilot's participation (PAP) in an operation.

    Self-reported by the pilot ("I was there") and optionally confirmed by an FC /
    officer. One row per pilot per operation; each also writes a FLEET contribution to
    the recognition ledger (idempotent on operation+pilot).
    """

    operation = models.ForeignKey(
        Operation, on_delete=models.CASCADE, related_name="attendance"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="op_attendance"
    )
    character_id = models.BigIntegerField(null=True, blank=True)
    character_name = models.CharField(max_length=200, blank=True)
    confirmed = models.BooleanField(default=False, help_text=gettext_lazy("Verified by an FC / officer."))
    added_by_officer = models.BooleanField(default=False)

    class Meta:
        unique_together = ("operation", "user")
        ordering = ["-confirmed", "character_name"]

    def __str__(self) -> str:
        return f"{self.character_name or self.user_id} @ {self.operation_id}"


class OperationRsvp(TimeStampedModel):
    """A pilot's intended availability for an upcoming operation.

    Distinct from ``OperationAttendance`` (PAP, recorded *after* the fleet): this
    is the *before* signal — will you make it? — so FCs can size the fleet and see
    who's committed versus tentative ahead of time. One row per pilot per op.
    """

    class Response(models.TextChoices):
        YES = "yes", gettext_lazy("Coming")
        MAYBE = "maybe", gettext_lazy("Maybe")
        NO = "no", gettext_lazy("Can't make it")

    operation = models.ForeignKey(Operation, on_delete=models.CASCADE, related_name="rsvps")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="op_rsvps"
    )
    character_name = models.CharField(max_length=200, blank=True)
    response = models.CharField(max_length=5, choices=Response.choices, default=Response.YES)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = ("operation", "user")
        ordering = ["response", "character_name"]

    def __str__(self) -> str:
        return f"{self.character_name or self.user_id}: {self.response} @ {self.operation_id}"


class StructureTimer(TimeStampedModel):
    """An upcoming structure reinforcement / sov timer the corp cares about.

    A lightweight, manually-maintained timer board (the Alliance Auth "Structure
    Timers" equivalent): when a structure's armor/hull timer comes out, or a sov
    objective is contestable, with a live countdown for fleet planning.
    """

    class TimerType(models.TextChoices):
        ARMOR = "armor", gettext_lazy("Armor")
        HULL = "hull", gettext_lazy("Hull / Final")
        ANCHORING = "anchoring", gettext_lazy("Anchoring")
        UNANCHORING = "unanchoring", gettext_lazy("Unanchoring")
        # "Sov · IHub" / "Sov · TCU" — sov jargon + EVE structure abbreviations; kept English.
        IHUB = "ihub", "Sov · IHub"
        TCU = "tcu", "Sov · TCU"
        OTHER = "other", gettext_lazy("Other")

    class Side(models.TextChoices):
        FRIENDLY = "friendly", gettext_lazy("Friendly (defend)")
        HOSTILE = "hostile", gettext_lazy("Hostile (attack)")
        NEUTRAL = "neutral", gettext_lazy("Neutral")

    # Marker put on rows created by the structure-monitoring ESI bridge
    # (apps.corporation.structures_esi.import_reinforcement_timers). Those already
    # reach the Pingboard calendar via the CorpStructure sync, so the timer-board
    # calendar sync (apps.pingboard.calendar._sync_timers) excludes them by this note
    # to avoid publishing the same reinforcement timer twice.
    AUTO_IMPORT_NOTE = "Auto-imported from structure monitoring."

    name = models.CharField(max_length=200, help_text=gettext_lazy("Structure name or label."))
    system_name = models.CharField(max_length=120, blank=True)
    system_id = models.BigIntegerField(null=True, blank=True)
    # help_text lists EVE structure type names (game data) — kept English verbatim.
    structure_type = models.CharField(max_length=80, blank=True, help_text="Astrahus, Fortizar…")
    timer_type = models.CharField(max_length=12, choices=TimerType.choices, default=TimerType.ARMOR)
    side = models.CharField(max_length=10, choices=Side.choices, default=Side.HOSTILE)
    exits_at = models.DateTimeField(db_index=True, help_text=gettext_lazy("When the timer comes out (EVE/UTC)."))
    notes = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["exits_at"]

    def __str__(self) -> str:
        return f"{self.name} · {self.get_timer_type_display()} @ {self.exits_at:%Y-%m-%d %H:%M}"

    @property
    def is_auto_imported(self) -> bool:
        """True when the structure-monitoring ESI bridge created this row.

        The ``notes`` column then holds the stable :data:`AUTO_IMPORT_NOTE` dedup
        marker (a code identifier, kept English so the calendar sync can match on
        it) rather than officer free-text — so the timer board renders a translated
        label for it instead of the frozen English marker.
        """
        return self.notes == self.AUTO_IMPORT_NOTE


class SovStructure(TimeStampedModel):
    """A sovereignty structure (TCU / IHub) held by our alliance, from public ESI.

    Tracks the Activity Defense Multiplier (ADM, the ``vulnerability_occupancy_level``)
    and the vulnerability window so leadership can see at a glance which systems are
    soft (low ADM) or about to be contestable. Only relevant if the corp's alliance
    holds sov; empty otherwise. Snapshot-keyed by the ESI structure id.
    """

    IHUB_TYPE = 32458  # Infrastructure Hub (carries the ADM)
    TCU_TYPE = 32226   # Territorial Claim Unit

    structure_id = models.BigIntegerField(primary_key=True)
    alliance_id = models.BigIntegerField(db_index=True)
    solar_system_id = models.IntegerField(db_index=True)
    system_name = models.CharField(max_length=120, blank=True)
    structure_type_id = models.IntegerField(default=0)
    adm = models.FloatField(default=1.0, help_text=gettext_lazy("Activity Defense Multiplier (1.0–6.0)."))
    vulnerable_start = models.DateTimeField(null=True, blank=True)
    vulnerable_end = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["adm", "system_name"]

    def __str__(self) -> str:
        return f"{self.system_name or self.solar_system_id} ADM {self.adm:.1f}"

    @property
    def structure_label(self) -> str:
        return {self.IHUB_TYPE: "IHub", self.TCU_TYPE: "TCU"}.get(self.structure_type_id, "Sov")

    @property
    def is_soft(self) -> bool:
        """ADM below the leadership-configured floor (default 3.0) means a noticeably
        weaker defensive timer — worth shoring up."""
        from apps.corporation.models import StructureAlertConfig

        _, adm_floor = StructureAlertConfig.thresholds()
        return self.adm < adm_floor


class OperationTemplate(TimeStampedModel):
    """OPS-4 (3.12): a reusable strat-op blueprint that a beat materialises into real
    ``Operation`` instances on a weekly cadence — so officers stop re-entering the same
    composition, comms and SRP config for every recurring fleet."""

    name = models.CharField(max_length=200)
    type = models.CharField(max_length=20, choices=Operation.Type.choices,
                            default=Operation.Type.PVP)
    # Op config copied onto each instance.
    duration_minutes = models.PositiveIntegerField(default=60)
    formup = models.CharField(max_length=200, blank=True)
    destination = models.CharField(max_length=200, blank=True)
    comms = models.CharField(max_length=200, blank=True)
    link = models.CharField(max_length=500, blank=True)
    notes = models.TextField(blank=True)
    min_pilots = models.PositiveIntegerField(default=0)
    srp = models.CharField(max_length=10, choices=Operation.Srp.choices, blank=True)
    rsvp_offset_minutes = models.PositiveIntegerField(
        default=0, help_text=gettext_lazy("Instance RSVP deadline = this many minutes before form-up (0 = none)."))

    # Weekly cadence, in UTC.
    weekday = models.PositiveSmallIntegerField(
        default=5, help_text=gettext_lazy("0 = Monday … 6 = Sunday (UTC)."))
    hour = models.PositiveSmallIntegerField(default=20, help_text=gettext_lazy("Form-up hour, 0–23 (UTC)."))
    minute = models.PositiveSmallIntegerField(default=0, help_text=gettext_lazy("Form-up minute, 0–59 (UTC)."))
    lead_days = models.PositiveSmallIntegerField(
        default=10, help_text=gettext_lazy("Materialise instances this many days ahead."))

    active = models.BooleanField(default=True)
    fc = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", help_text=gettext_lazy("Default fleet commander for spawned ops."))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["-active", "name"]

    def __str__(self) -> str:
        return f"OperationTemplate<{self.name}>"


class OperationTemplateSlot(models.Model):
    """A ship-composition row on a template, copied into each materialised op's ship slots."""

    template = models.ForeignKey(
        OperationTemplate, on_delete=models.CASCADE, related_name="slots")
    ship_name = models.CharField(max_length=200)
    ship_type_id = models.BigIntegerField(null=True, blank=True)
    role = models.CharField(max_length=10, choices=OperationShipSlot.Role.choices,
                            default=OperationShipSlot.Role.DPS)
    min_pilots = models.PositiveIntegerField(default=1)
    max_pilots = models.PositiveIntegerField(default=0, help_text=gettext_lazy("0 = no cap."))
    priority = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["priority", "id"]

    def __str__(self) -> str:
        return f"{self.ship_name} ×{self.min_pilots}"
