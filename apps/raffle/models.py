"""Raffle Contest subsystem — data model.

A **raffle contest** is a time-boxed engagement campaign: during the accrual
window enrolled pilots earn *tickets* from configured activity sources (PVP kills,
manual leadership grants, mining, fleet attendance, …); at the draw time the
system draws winners with a cryptographically-secure, commit-reveal, auditable
process and assigns prizes.

Two invariants run through the whole model and must never be softened silently:

* **Only enrolled pilots with a valid ESI token earn tickets or win prizes.**
  Activity from a pilot who has no FORCA account / no live token is recorded as
  :class:`RaffleIneligibleActivity` (for adoption analytics + outreach) — never as
  a drawable :class:`RaffleTicketLedgerEntry`. See :mod:`apps.raffle.eligibility`.
* **The ticket ledger is append-only.** Corrections are represented as new
  reversal/adjustment rows, not destructive edits; after a contest closes the
  ledger is frozen and only an audited correction workflow may touch it.

Nothing here moves ISK — prizes are *recorded and fulfilled manually*, exactly
like SRP and mentorship rewards.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel

# The base scope every SSO login stores; "has a valid ESI token" means at least a
# non-revoked token exists — no extra scope is required for the PVP source. A
# contest may demand more via ``RaffleContest.required_scopes``.
DEFAULT_ALGORITHM_VERSION = "1"


# --------------------------------------------------------------------------- #
#  Contest
# --------------------------------------------------------------------------- #
class RaffleContest(TimeStampedModel):
    """A single time-boxed raffle campaign and all of its configuration.

    The lifecycle is a guarded state machine (see :mod:`apps.raffle.services`):
    ``draft → scheduled → active → closed → completed → archived`` with
    ``cancelled`` reachable from any pre-draw state. Ticket accrual happens only
    while ``active`` and inside ``[start_at, end_at]``; the ledger freezes at
    ``closed``; the draw runs at ``draw_at`` and moves the contest to
    ``completed``.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SCHEDULED = "scheduled", _("Scheduled")
        ACTIVE = "active", _("Active")
        CLOSED = "closed", _("Closed (awaiting draw)")
        COMPLETED = "completed", _("Completed")
        ARCHIVED = "archived", _("Archived")
        CANCELLED = "cancelled", _("Cancelled")

    # Statuses whose pilot-facing pages the public/archive may show.
    VISIBLE_STATUSES = (Status.ACTIVE, Status.CLOSED, Status.COMPLETED, Status.ARCHIVED)
    # Statuses where configuration may still be freely edited.
    EDITABLE_STATUSES = (Status.DRAFT, Status.SCHEDULED)

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    description = models.TextField(blank=True, help_text=_("Short pitch shown in the hero."))
    objective = models.CharField(
        max_length=300, blank=True,
        help_text=_("Why the contest exists — the engagement goal in one line."),
    )
    public_rules = models.TextField(
        blank=True, help_text=_("Leadership-authored rules shown to pilots.")
    )
    admin_notes = models.TextField(blank=True, help_text=_("Internal — never shown to pilots."))

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True
    )

    start_at = models.DateTimeField(help_text=_("Ticket accrual opens."))
    end_at = models.DateTimeField(help_text=_("Ticket accrual closes."))
    draw_at = models.DateTimeField(help_text=_("Winners are drawn."))

    # --- Eligibility / ESI-adoption policy --------------------------------- #
    require_enrolled = models.BooleanField(
        default=True,
        help_text=_("Only pilots enrolled in FORCA earn tickets (strongly recommended)."),
    )
    require_valid_token = models.BooleanField(
        default=True,
        help_text=_("Require a live, non-revoked ESI token to earn tickets (recommended)."),
    )
    required_scopes = models.JSONField(
        default=list, blank=True,
        help_text=_("Extra ESI scopes a pilot's token must carry (usually empty for PVP)."),
    )
    include_alliance = models.BooleanField(
        default=False,
        help_text=_("Also admit registered alliance / friendly-corp pilots (default: corp only)."),
    )
    retroactive_enabled = models.BooleanField(
        default=False,
        help_text=_("Award tickets for eligible activity back to the start date once a "
                    "pilot enrols (still must be enrolled + valid at draw to win)."),
    )

    # --- Draw policy ------------------------------------------------------- #
    one_prize_per_pilot = models.BooleanField(
        default=True, help_text=_("A pilot can win at most one prize (spreads rewards).")
    )
    algorithm_version = models.CharField(max_length=8, default=DEFAULT_ALGORITHM_VERSION)
    auto_draw = models.BooleanField(
        default=True, help_text=_("Draw automatically at the draw time (else officer runs it).")
    )

    # --- Display / transparency ------------------------------------------- #
    leaderboard_visible = models.BooleanField(default=True)
    leaderboard_size = models.PositiveIntegerField(default=25)
    show_odds = models.BooleanField(
        default=False, help_text=_("Show each pilot their estimated chance of winning.")
    )
    show_recent_events = models.BooleanField(
        default=True, help_text=_("Show a recent-ticket-events feed on the dashboard.")
    )
    show_ineligible_to_pilots = models.BooleanField(
        default=False,
        help_text=_("Show a pilot the tickets they *would* have earned before enrolling."),
    )
    archive_public = models.BooleanField(
        default=True, help_text=_("Keep this contest visible in the archive after completion.")
    )

    # --- Booster window (double-ticket weekends, strategic pushes) --------- #
    booster_multiplier = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("1"),
        help_text=_("Ticket multiplier applied inside the booster window (1 = off)."),
    )
    booster_start_at = models.DateTimeField(null=True, blank=True)
    booster_end_at = models.DateTimeField(null=True, blank=True)

    # --- Activity safeguard: a minimum level of activity for a VALID draw --- #
    # Protects the corp's ISK: if pilots don't engage, the automatic draw is held.
    # Leadership can still force a manual draw. Blank metric = no minimum.
    min_activity_metric = models.CharField(
        max_length=32, blank=True,
        help_text=_("Activity metric that gates a valid draw (blank = always valid)."),
    )
    min_activity_threshold = models.DecimalField(
        max_digits=24, decimal_places=2, default=0,
        help_text=_("The contest is valid for an automatic draw only once this metric reaches this value."),
    )

    # --- Prize-value booster: hit a goal → ISK/PLEX prizes are worth more --- #
    prize_booster_metric = models.CharField(
        max_length=32, blank=True,
        help_text=_("Activity metric whose goal unlocks a prize-value boost (blank = off)."),
    )
    prize_booster_goal = models.DecimalField(max_digits=24, decimal_places=2, default=0)
    prize_booster_percent = models.DecimalField(
        max_digits=6, decimal_places=2, default=0,
        help_text=_("Boost ISK/PLEX prize values by this percent when the goal is reached (e.g. 10)."),
    )

    # --- Goals / milestones (JSON: [{label, metric, target}]) -------------- #
    goals = models.JSONField(default=list, blank=True)

    # --- Anti-abuse thresholds (JSON, see apps.raffle.integrity) ----------- #
    anti_abuse = models.JSONField(default=dict, blank=True)

    template_key = models.CharField(max_length=40, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-start_at", "-created_at"]
        indexes = [
            models.Index(fields=["status", "draw_at"], name="raffle_status_draw_idx"),
            models.Index(fields=["status", "-start_at"], name="raffle_status_start_idx"),
        ]

    def __str__(self) -> str:
        return f"RaffleContest<{self.name}:{self.status}>"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:120] or "contest"
            slug, i = base, 2
            while RaffleContest.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{i}"
                i += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("raffle:detail", args=[self.slug])

    # --- Derived state ----------------------------------------------------- #
    @property
    def is_accruing(self) -> bool:
        """Tickets may be earned right now."""
        now = timezone.now()
        return self.status == self.Status.ACTIVE and self.start_at <= now < self.end_at

    @property
    def is_frozen(self) -> bool:
        """Ledger is immutable (closed / completed / archived)."""
        return self.status in (self.Status.CLOSED, self.Status.COMPLETED, self.Status.ARCHIVED)

    @property
    def is_editable(self) -> bool:
        """Config (schedule / eligibility / draw rules) may still be edited — true
        until ticket accrual actually begins, NOT merely based on status.

        Any draft or scheduled contest is editable; so is an ACTIVE contest whose
        start time is still in the future (e.g. it was activated early to announce
        it). No tickets can accrue before ``start_at`` (the engine's window is
        empty until then), so its time box and rules are still safe to change.
        Once accrual has started — or the contest is closed/completed/archived/
        cancelled — the schedule and rules lock.
        """
        if self.status in (self.Status.DRAFT, self.Status.SCHEDULED):
            return True
        if self.status == self.Status.ACTIVE:
            return timezone.now() < self.start_at
        return False

    @property
    def is_boosted_now(self) -> bool:
        if self.booster_multiplier <= 1 or not (self.booster_start_at and self.booster_end_at):
            return False
        return self.booster_start_at <= timezone.now() < self.booster_end_at

    def booster_for(self, when) -> Decimal:
        """The ticket multiplier in effect at ``when`` (Decimal('1') outside the window)."""
        if self.booster_multiplier <= 1 or not (self.booster_start_at and self.booster_end_at):
            return Decimal("1")
        if self.booster_start_at <= when < self.booster_end_at:
            return Decimal(self.booster_multiplier)
        return Decimal("1")


# --------------------------------------------------------------------------- #
#  Prizes
# --------------------------------------------------------------------------- #
class RafflePrize(TimeStampedModel):
    """One prize slot in a contest (rank 1 = top prize)."""

    class PrizeType(models.TextChoices):
        ISK = "isk", "ISK"
        PLEX = "plex", "PLEX"
        ITEM = "item", _("In-game item")
        DOCTRINE_SHIP = "doctrine_ship", _("Doctrine ship")
        CAPITAL = "capital", _("Capital ship")
        SUPERCAPITAL = "supercapital", _("Supercapital ship")
        CUSTOM = "custom", _("Custom reward")

    contest = models.ForeignKey(
        RaffleContest, on_delete=models.CASCADE, related_name="prizes"
    )
    rank = models.PositiveIntegerField(default=1, help_text=_("1 = first prize."))
    name = models.CharField(max_length=140)
    prize_type = models.CharField(
        max_length=14, choices=PrizeType.choices, default=PrizeType.ISK
    )
    icon_type_id = models.IntegerField(
        null=True, blank=True, help_text=_("EVE type id for the prize icon (ships/items).")
    )
    quantity = models.PositiveIntegerField(default=1)
    estimated_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    description = models.TextField(blank=True)
    delivery_instructions = models.TextField(
        blank=True, help_text=_("Public: how the winner receives the prize.")
    )
    internal_notes = models.TextField(blank=True, help_text=_("Internal — leaders only."))

    class Meta:
        ordering = ["contest", "rank"]
        constraints = [
            models.UniqueConstraint(fields=["contest", "rank"], name="uniq_raffle_prize_rank")
        ]

    def __str__(self) -> str:
        return f"#{self.rank} {self.name}"


# --------------------------------------------------------------------------- #
#  Ticket-source configuration
# --------------------------------------------------------------------------- #
class RaffleTicketSourceConfig(TimeStampedModel):
    """Per-contest configuration for one ticket source (see :mod:`apps.raffle.sources`)."""

    class Mode(models.TextChoices):
        AUTO = "auto", _("Automatic")
        OFFICER_APPROVED = "officer_approved", _("Officer-approved")
        MANUAL = "manual", _("Manual only")

    class CapScope(models.TextChoices):
        NONE = "none", _("No cap")
        DAILY = "daily", _("Per day")
        WEEKLY = "weekly", _("Per week")
        CONTEST = "contest", _("Per contest")

    contest = models.ForeignKey(
        RaffleContest, on_delete=models.CASCADE, related_name="source_configs"
    )
    source_key = models.CharField(max_length=40, db_index=True)
    enabled = models.BooleanField(default=False)
    mode = models.CharField(max_length=16, choices=Mode.choices, default=Mode.AUTO)

    # Source-specific rate/rules (e.g. {"per_kill":1,"final_blow":10,"solo":100}).
    config = models.JSONField(default=dict, blank=True)
    # Source-specific eligibility filters (e.g. PVP region/system/min value).
    filters = models.JSONField(default=dict, blank=True)

    min_threshold = models.DecimalField(
        max_digits=20, decimal_places=2, default=0,
        help_text=_("Minimum event magnitude (e.g. min kill ISK, min m³) to earn a ticket."),
    )
    max_per_event = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("Cap tickets from a single event (blank = no cap).")
    )
    cap_scope = models.CharField(max_length=8, choices=CapScope.choices, default=CapScope.NONE)
    cap_amount = models.PositiveIntegerField(null=True, blank=True)

    require_esi = models.BooleanField(
        default=True,
        help_text=_("Require enrolment + a valid ESI token (leave on — not recommended to disable)."),
    )
    retroactive = models.BooleanField(
        default=False, help_text=_("Recompute from the start date once a pilot enrols.")
    )
    visible_to_pilots = models.BooleanField(default=True)
    show_calculation = models.BooleanField(
        default=True, help_text=_("Show pilots the detailed ticket maths for this source.")
    )
    last_processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["contest", "source_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["contest", "source_key"], name="uniq_raffle_source_per_contest"
            )
        ]

    def __str__(self) -> str:
        return f"{self.source_key}@{self.contest_id}"


# --------------------------------------------------------------------------- #
#  Ticket ledger (append-only)
# --------------------------------------------------------------------------- #
class RaffleTicketLedgerEntry(TimeStampedModel):
    """One immutable ticket award. The unit of the draw.

    Uniqueness on ``(contest, source_key, source_ref, character_id)`` makes every
    ticket source idempotent — reprocessing the same killmail / mining day / grant
    never double-awards. Corrections are new rows (``status=reversed`` / a negative
    adjustment), never edits to ``amount``.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending approval")
        APPROVED = "approved", _("Approved")
        EXCLUDED = "excluded", _("Excluded")
        REVERSED = "reversed", _("Reversed")
        DISQUALIFIED = "disqualified", _("Disqualified")

    class EsiStatus(models.TextChoices):
        VALID = "valid", _("Valid")
        EXPIRED = "expired", _("Expired")
        REVOKED = "revoked", _("Revoked")
        NONE = "none", _("No token")

    contest = models.ForeignKey(
        RaffleContest, on_delete=models.CASCADE, related_name="ledger_entries"
    )
    # The enrolled account that owns the tickets (null only for an audited
    # emergency-override grant to a not-yet-enrolled character).
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="raffle_tickets",
    )
    character_id = models.BigIntegerField(db_index=True)
    character_name = models.CharField(max_length=200, blank=True)

    source_key = models.CharField(max_length=40, db_index=True)
    source_ref = models.CharField(
        max_length=120, help_text=_("Stable event id, e.g. killmail:123 / manual:45.")
    )
    amount = models.IntegerField(default=0, help_text=_("Tickets (negative for reversals)."))
    # When the underlying ACTIVITY happened (distinct from created_at, when the row
    # was written) — drives period caps, the accrual-by-day curve, and the
    # non-retroactive pre-enrolment gate.
    occurred_at = models.DateTimeField(default=timezone.now)
    reason = models.CharField(max_length=300, blank=True)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.APPROVED, db_index=True
    )
    # Frozen eligibility context at award time (transparency + audit).
    eligibility_snapshot = models.JSONField(default=dict, blank=True)
    esi_status = models.CharField(
        max_length=8, choices=EsiStatus.choices, default=EsiStatus.VALID
    )

    created_by_system = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["contest", "source_key", "source_ref", "character_id"],
                name="uniq_raffle_ticket_event",
            )
        ]
        indexes = [
            models.Index(fields=["contest", "user"], name="raffle_ledger_user_idx"),
            models.Index(fields=["contest", "source_key"], name="raffle_ledger_source_idx"),
            models.Index(fields=["contest", "status"], name="raffle_ledger_status_idx"),
            models.Index(fields=["contest", "source_ref"], name="raffle_ledger_ref_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.amount}t {self.source_key} → {self.character_id}"

    @property
    def counts_for_draw(self) -> bool:
        return self.status == self.Status.APPROVED and self.amount > 0


# --------------------------------------------------------------------------- #
#  Manual grants
# --------------------------------------------------------------------------- #
class RaffleManualGrant(TimeStampedModel):
    """A leadership recognition grant of tickets, with reason + audit trail.

    Respects the enrolment rule by default; ``override_used`` records the rare,
    Director-only, explicitly-enabled emergency grant to a not-yet-enrolled pilot.
    """

    contest = models.ForeignKey(
        RaffleContest, on_delete=models.CASCADE, related_name="manual_grants"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="raffle_manual_grants",
    )
    character_id = models.BigIntegerField(null=True, blank=True)
    character_name = models.CharField(max_length=200, blank=True)

    amount = models.PositiveIntegerField(default=1)
    reason = models.CharField(max_length=300)
    category = models.CharField(max_length=60, blank=True)
    internal_notes = models.TextField(blank=True, help_text=_("Leaders only — never shown to the pilot."))
    override_used = models.BooleanField(default=False)

    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    ledger_entry = models.OneToOneField(
        RaffleTicketLedgerEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="manual_grant",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"grant {self.amount}t → {self.character_name or self.character_id}"


# --------------------------------------------------------------------------- #
#  Precomputed participant summary (leaderboard / dashboard)
# --------------------------------------------------------------------------- #
class RaffleParticipantSummary(TimeStampedModel):
    """A pilot's rolled-up standing in a contest, recomputed by a beat task.

    Read side of leaderboards + the personal progress card, so pages never scan the
    raw ledger. Keyed by the *account* (``user``) — the eligibility + prize unit.
    """

    contest = models.ForeignKey(
        RaffleContest, on_delete=models.CASCADE, related_name="summaries"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="raffle_summaries"
    )
    character_id = models.BigIntegerField(null=True, blank=True)
    character_name = models.CharField(max_length=200, blank=True)

    total_tickets = models.IntegerField(default=0)
    tickets_by_source = models.JSONField(default=dict, blank=True)

    pvp_kills = models.IntegerField(default=0)
    pvp_participation = models.IntegerField(default=0)
    pvp_final_blows = models.IntegerField(default=0)
    pvp_solo = models.IntegerField(default=0)
    manual_tickets = models.IntegerField(default=0)

    eligible = models.BooleanField(default=True)
    esi_status = models.CharField(max_length=8, default="valid")
    exclusion_reason = models.CharField(max_length=120, blank=True)
    rank = models.PositiveIntegerField(default=0)
    last_recalc_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["contest", "rank", "-total_tickets"]
        constraints = [
            models.UniqueConstraint(
                fields=["contest", "user"], name="uniq_raffle_summary_per_user"
            )
        ]
        indexes = [
            models.Index(fields=["contest", "-total_tickets"], name="raffle_summary_tix_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.character_name or self.user_id}: {self.total_tickets}t"


# --------------------------------------------------------------------------- #
#  Ineligible activity (adoption analytics + outreach)
# --------------------------------------------------------------------------- #
class RaffleIneligibleActivity(TimeStampedModel):
    """Eligible-*looking* activity by a pilot who could not be awarded tickets.

    Recorded for analytics + leadership outreach ("you flew with us — enrol to
    claim these tickets"). NEVER drawable. Idempotent per event+character.
    """

    class Reason(models.TextChoices):
        NOT_ENROLLED = "not_enrolled", _("No FORCA account")
        NO_TOKEN = "no_token", _("No valid ESI token")
        TOKEN_EXPIRED = "token_expired", _("ESI token expired/revoked")
        MISSING_SCOPE = "missing_scope", _("Missing required scope")
        NOT_CORP = "not_corp", _("Not a recognised corp pilot")
        EXCLUDED = "excluded", _("Manually excluded")

    contest = models.ForeignKey(
        RaffleContest, on_delete=models.CASCADE, related_name="ineligible_activity"
    )
    character_id = models.BigIntegerField(db_index=True)
    character_name = models.CharField(max_length=200, blank=True)
    source_key = models.CharField(max_length=40, db_index=True)
    source_ref = models.CharField(max_length=120)
    reason = models.CharField(max_length=16, choices=Reason.choices, db_index=True)
    would_be_tickets = models.IntegerField(default=0)
    detected_at = models.DateTimeField(default=timezone.now)
    later_enrolled = models.BooleanField(default=False)
    retroactive_applied = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-detected_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["contest", "source_key", "source_ref", "character_id"],
                name="uniq_raffle_ineligible_event",
            )
        ]
        indexes = [
            models.Index(fields=["contest", "reason"], name="raffle_inelig_reason_idx"),
            models.Index(fields=["contest", "character_id"], name="raffle_inelig_char_idx"),
        ]

    def __str__(self) -> str:
        return f"ineligible {self.character_id} ({self.reason})"


# --------------------------------------------------------------------------- #
#  Draw + results + eligibility snapshot
# --------------------------------------------------------------------------- #
class RaffleDraw(TimeStampedModel):
    """One execution of the winner draw for a contest (commit-reveal + manifest).

    A redraw creates a NEW row referencing the superseded one — historical draws
    are never mutated.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        COMMITTED = "committed", _("Seed committed")
        RUNNING = "running", _("Running")
        COMPLETED = "completed", _("Completed")
        FAILED = "failed", _("Failed")

    contest = models.ForeignKey(RaffleContest, on_delete=models.CASCADE, related_name="draws")
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    algorithm_version = models.CharField(max_length=8, default=DEFAULT_ALGORITHM_VERSION)
    code_version = models.CharField(max_length=40, blank=True, help_text=_("git commit at draw time."))

    # Commit-reveal fairness: the sha256 of the secret seed is stored (and shown)
    # BEFORE the draw; the seed itself is revealed after, so anyone can recompute.
    seed_commitment = models.CharField(max_length=64, blank=True)
    seed = models.CharField(max_length=128, blank=True)
    external_entropy = models.CharField(
        max_length=200, blank=True, help_text=_("Optional public seed folded into the entropy.")
    )
    committed_at = models.DateTimeField(null=True, blank=True)
    revealed_at = models.DateTimeField(null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    total_eligible_tickets = models.IntegerField(default=0)
    total_excluded_tickets = models.IntegerField(default=0)
    eligible_pilots = models.IntegerField(default=0)
    excluded_pilots = models.IntegerField(default=0)

    # Activity-safeguard + prize-booster outcome frozen at draw time.
    min_activity_met = models.BooleanField(default=True)
    forced_below_minimum = models.BooleanField(
        default=False, help_text=_("Drawn by leadership override despite unmet minimum activity.")
    )
    prize_booster_applied = models.BooleanField(default=False)
    prize_booster_percent = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    manifest = models.JSONField(default=dict, blank=True)
    random_values = models.JSONField(default=list, blank=True)
    skipped_draws = models.JSONField(default=list, blank=True)

    executed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", help_text=_("Null = automatic draw."),
    )
    superseded_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="supersedes"
    )
    notes = models.TextField(blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["contest", "status"], name="raffle_draw_status_idx"),
        ]

    def __str__(self) -> str:
        return f"draw<{self.contest_id}:{self.status}>"

    @property
    def is_current(self) -> bool:
        return self.superseded_by_id is None


class RaffleDrawResult(TimeStampedModel):
    """A winning draw → prize assignment (and its fulfilment lifecycle)."""

    class Status(models.TextChoices):
        WON = "won", _("Won")
        FORFEITED = "forfeited", _("Forfeited")
        REDRAWN = "redrawn", _("Redrawn")
        VOID = "void", _("Void")

    class FulfilStatus(models.TextChoices):
        PENDING = "pending", _("Pending")
        CONTACTED = "contacted", _("Contacted")
        DELIVERED = "delivered", _("Delivered")
        CANCELLED = "cancelled", _("Cancelled")

    draw = models.ForeignKey(RaffleDraw, on_delete=models.CASCADE, related_name="results")
    prize = models.ForeignKey(RafflePrize, on_delete=models.CASCADE, related_name="results")
    winner_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="raffle_wins",
    )
    winner_character_id = models.BigIntegerField(null=True, blank=True)
    winner_character_name = models.CharField(max_length=200, blank=True)

    draw_order = models.PositiveIntegerField(default=1, help_text=_("1 = first prize drawn."))
    winning_ticket_index = models.IntegerField(default=0)
    winning_ticket_ref = models.CharField(max_length=120, blank=True)
    # Effective value actually awarded = prize value × the prize booster when it was
    # achieved (ISK/PLEX prizes only); frozen so fulfilment pays the boosted amount.
    awarded_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.WON)

    fulfil_status = models.CharField(
        max_length=10, choices=FulfilStatus.choices, default=FulfilStatus.PENDING, db_index=True
    )
    fulfilled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    fulfilment_notes = models.TextField(blank=True, help_text=_("Internal fulfilment notes."))

    class Meta:
        ordering = ["draw", "draw_order"]
        constraints = [
            models.UniqueConstraint(fields=["draw", "prize"], name="uniq_raffle_result_per_prize")
        ]

    def __str__(self) -> str:
        return f"result #{self.draw_order} {self.winner_character_name or self.winner_user_id}"


class RaffleParticipantEligibilitySnapshot(TimeStampedModel):
    """Per-pilot eligibility as frozen at draw time — the audit backbone of a draw."""

    draw = models.ForeignKey(
        RaffleDraw, on_delete=models.CASCADE, related_name="eligibility_snapshots"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    character_id = models.BigIntegerField(null=True, blank=True)
    character_name = models.CharField(max_length=200, blank=True)

    enrolled = models.BooleanField(default=False)
    has_valid_token = models.BooleanField(default=False)
    is_corp_member = models.BooleanField(default=False)
    scopes_ok = models.BooleanField(default=True)
    manually_excluded = models.BooleanField(default=False)
    eligible = models.BooleanField(default=False)
    exclusion_reason = models.CharField(max_length=120, blank=True)

    tickets_counted = models.IntegerField(default=0)
    tickets_excluded = models.IntegerField(default=0)

    class Meta:
        ordering = ["draw", "-tickets_counted"]
        indexes = [
            models.Index(fields=["draw", "eligible"], name="raffle_elig_snap_idx"),
        ]

    def __str__(self) -> str:
        return f"snap {self.character_id} elig={self.eligible}"


# --------------------------------------------------------------------------- #
#  Integrity / suspicious activity
# --------------------------------------------------------------------------- #
class RaffleSuspiciousActivityFlag(TimeStampedModel):
    """A flagged (not deleted) ticket event pending officer review."""

    class FlagType(models.TextChoices):
        REPEATED_VICTIM = "repeated_victim", _("Same victim repeated")
        LOW_VALUE = "low_value", _("Very low value kill")
        SELF_KILL = "self_kill", _("Possible self-kill / awox")
        RAPID_REPEAT = "rapid_repeat", _("Rapid repeated kills")
        BLUE_KILL = "blue_kill", _("Blue / friendly kill")
        OTHER = "other", _("Other")

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        DISMISSED = "dismissed", _("Dismissed (kept)")
        UPHELD = "upheld", _("Upheld (disqualified)")

    contest = models.ForeignKey(
        RaffleContest, on_delete=models.CASCADE, related_name="suspicious_flags"
    )
    character_id = models.BigIntegerField(null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    ledger_entry = models.ForeignKey(
        RaffleTicketLedgerEntry, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="flags",
    )
    flag_type = models.CharField(max_length=16, choices=FlagType.choices)
    detail = models.CharField(max_length=300, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    resolution = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"flag {self.flag_type} ({self.status})"


# --------------------------------------------------------------------------- #
#  Templates + global config
# --------------------------------------------------------------------------- #
class RaffleExclusion(TimeStampedModel):
    """A pilot leadership has manually barred from a contest (audited).

    Excluded pilots earn no new tickets and are removed from the winner pool at
    draw time. Keyed by the *account* when known, else the character id.
    """

    contest = models.ForeignKey(
        RaffleContest, on_delete=models.CASCADE, related_name="exclusions"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="raffle_exclusions",
    )
    character_id = models.BigIntegerField(null=True, blank=True)
    character_name = models.CharField(max_length=200, blank=True)
    reason = models.CharField(max_length=300)
    active = models.BooleanField(default=True)
    excluded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["contest", "user"], name="uniq_raffle_exclusion_user",
                condition=models.Q(user__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["contest", "character_id"], name="uniq_raffle_exclusion_char",
                condition=models.Q(character_id__isnull=False),
            ),
        ]

    def __str__(self) -> str:
        return f"exclude {self.character_name or self.user_id or self.character_id}"


class RaffleContestTemplate(TimeStampedModel):
    """A reusable contest blueprint (contest defaults + sources + prizes)."""

    key = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    config = models.JSONField(default=dict, blank=True)
    built_in = models.BooleanField(default=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"template<{self.key}>"


class RaffleConfig(TimeStampedModel):
    """Raffle-wide leadership settings (a singleton), separate from per-contest config."""

    name = models.CharField(max_length=80, default="Default")
    is_active = models.BooleanField(default=True)

    # The emergency, off-by-default, Director-only power to grant tickets to a
    # not-yet-enrolled pilot. Kept behind this flag AND the Director role AND an
    # explicit per-grant confirmation, and always audited.
    allow_manual_override = models.BooleanField(
        default=False,
        help_text=_("DANGER: allow Director grants to non-enrolled pilots (off by default)."),
    )
    intro_text = models.TextField(
        blank=True,
        default=(
            "Fly with the corp, earn raffle tickets, win prizes. Connect your ESI "
            "token and enrol in FORCA Command Grid to take part — only enrolled "
            "pilots with a live ESI connection can earn tickets and win."
        ),
    )

    # RAF-5 (3.14): a monthly prize-spend ceiling (like SRP / Command Intelligence budgets).
    # 0 = off. Prize value is counted in the month a contest draws.
    monthly_prize_budget = models.DecimalField(
        max_digits=20, decimal_places=2, default=0,
        help_text=_("Monthly raffle prize ISK ceiling (0 = no limit). Warns near it and "
                    "holds new contests once a month is committed past it."),
    )
    budget_warn_pct = models.PositiveSmallIntegerField(
        default=80,
        help_text=_("Warn leadership once a month's committed prize value reaches this "
                    "percent of the ceiling."),
    )

    class Meta:
        ordering = ["-is_active", "-updated_at"]

    def __str__(self) -> str:
        return f"RaffleConfig<{self.name}>"


class RaffleEnrolmentOutreach(TimeStampedModel):
    """RAF-3 (3.9): a record that we nudged an active-but-unenrolled pilot to enrol for a
    contest, so the same pilot is never nudged twice for it (rate-limit / no-spam)."""

    contest = models.ForeignKey(
        RaffleContest, on_delete=models.CASCADE, related_name="enrolment_outreach")
    character_id = models.BigIntegerField(db_index=True)
    character_name = models.CharField(max_length=200, blank=True)
    would_be_tickets = models.IntegerField(default=0)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+")
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("contest", "character_id")
        ordering = ["-sent_at"]

    def __str__(self) -> str:
        return f"Outreach<{self.contest_id}:{self.character_id}>"


class RaffleOutreachOptOut(TimeStampedModel):
    """RAF-3 (3.9): a pilot who asked not to be nudged about enrolling again — a global
    (cross-contest) decline we honour permanently."""

    character_id = models.BigIntegerField(unique=True)
    character_name = models.CharField(max_length=200, blank=True)
    opted_out_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"OutreachOptOut<{self.character_id}>"
