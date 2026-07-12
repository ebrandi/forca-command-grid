"""Pilot engagement spine: personal preferences and the contribution ledger.

These two models are the cross-cutting backbone of the pilot-facing experience
(PRD Part II §II.9). ``PilotPreference`` holds each member's personal settings;
``ContributionEvent`` is the append-only ledger that records *what a pilot did
for the corp* — built a hull, hauled a load, completed a task — so the pilot can
see their own impact and (opt-out) the corp can celebrate it.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel


class PilotPreference(TimeStampedModel):
    """Per-member settings. Created lazily the first time a member needs one."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pilot_preference"
    )
    # Recognition is opt-out: contributions are celebrated corp-wide unless the
    # member turns this off. (Product decision; see PRD §II.6.5.)
    public_recognition = models.BooleanField(default=True)
    # Free-form IANA-ish tz label the member sets; used to frame "help this week".
    timezone = models.CharField(max_length=64, blank=True)
    # Which linked character the member considers their fleet "main".
    primary_character_id = models.BigIntegerField(null=True, blank=True)
    # Reserved for future dashboard personalisation.
    dashboard_layout = models.JSONField(default=dict, blank=True)
    # Opt-in: DM the member one reminder when a character's skill queue runs dry, so
    # they stop bleeding SP. Off by default — a nudge is only sent to pilots who ask.
    notify_idle_queue = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"prefs:{self.user_id}"


class ContributionEvent(TimeStampedModel):
    """One thing a pilot did that helped the corp.

    Append-only and idempotent per source action (``kind`` + ``ref_type`` +
    ``ref_id``), so the system that completes an action can record credit
    without fear of double-counting on retries. Magnitudes are kept in their
    *native unit* (ISK, ships, m³, count) — there is deliberately no single
    composite "score" (PRD §II.6.2).
    """

    class Kind(models.TextChoices):
        # Each kind maps to a real completion event (see apps.*.record_contribution
        # call sites). SEED/DELIVERY were removed in 0003 (never recorded); TRAIN
        # was re-added in 0005 with a real recorder (skill-progression detection)
        # alongside DOCTRINE (newly-flyable doctrine ships).
        BUILD = "build", _("Built")
        HAUL = "haul", _("Hauled")
        TASK = "task", _("Completed task")
        SRP = "srp", _("Ship replacement")
        MINING = "mining", _("Mined")
        FLEET = "fleet", _("Flew in fleet")
        TRAIN = "train", _("Trained skill")
        DOCTRINE = "doctrine", _("Unlocked doctrine")
        DIRECTIVE = "directive", _("Completed directive")  # CMD-2 (3.6)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="contributions"
    )
    kind = models.CharField(max_length=12, choices=Kind.choices, db_index=True)
    # Quantity in the native unit named by ``unit`` (e.g. 5 ships, 120000 m³).
    magnitude = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    unit = models.CharField(max_length=16, default="count")
    # Leader-weighted points for this event (computed from ContributionWeights at
    # record time). The native magnitude stays the source of truth; points are the
    # comparable cross-kind score leaders tune.
    points = models.IntegerField(default=0, db_index=True)
    # What this credit was for, and (optionally) which corp gap it closed.
    description = models.CharField(max_length=200, blank=True)
    ref_type = models.CharField(max_length=32, blank=True)
    ref_id = models.CharField(max_length=64, blank=True)
    gap_ref = models.CharField(max_length=120, blank=True)
    occurred_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["user", "occurred_at"]),
            models.Index(fields=["kind", "ref_type", "ref_id"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "ref_type", "ref_id"],
                condition=models.Q(ref_id__gt=""),
                name="uniq_contribution_per_source_action",
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.kind}:{self.magnitude}{self.unit}"


class ContributionWeights(TimeStampedModel):
    """Leadership-tunable point values for the contribution score (a singleton).

    The contribution ledger keeps native units (ISK, ships, m³…); these weights
    turn each event into comparable **points** so leaders get a single corp
    scoreboard and can steer what the corp values. ``active_weights()`` returns
    the live row, seeding sensible defaults on first use.
    """

    name = models.CharField(max_length=80, default="Standard")
    is_active = models.BooleanField(default=True)
    # Master switch: when off, every event scores 0 points (native units stay).
    enabled = models.BooleanField(default=True)

    # Flat per-event kinds.
    task_points = models.IntegerField(default=1, help_text=_("Points per completed task."))
    fleet_points = models.IntegerField(default=2, help_text=_("Points per fleet attended."))
    haul_points = models.IntegerField(default=3, help_text=_("Points per delivered haul."))
    haul_requires_verification = models.BooleanField(
        default=True,
        help_text=_("Only award haul points once the delivery is ESI-verified in-game."),
    )

    # Per-unit kinds.
    build_points_per_ship = models.IntegerField(
        default=1, help_text=_("Points per ship built and delivered.")
    )
    mining_points_per_mil = models.DecimalField(
        max_digits=8, decimal_places=3, default=Decimal("0.100"),
        help_text=_("Points per 1,000,000 ISK of mining payout."),
    )
    srp_points_per_mil = models.DecimalField(
        max_digits=8, decimal_places=3, default=Decimal("0.000"),
        help_text=_("Points per 1,000,000 ISK of SRP (0 = SRP earns no points)."),
    )

    # Skill training: points per recommended skill level trained.
    train_points_per_level = models.IntegerField(
        default=1, help_text=_("Points per recommended skill level trained.")
    )

    # Doctrine unlock: variable — base + corp-priority + effort (required SP).
    doctrine_base = models.IntegerField(
        default=5, help_text=_("Base points for unlocking any doctrine ship.")
    )
    doctrine_priority_coef = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("0.10"),
        help_text=_("Extra points × the doctrine's corp priority (0–100)."),
    )
    doctrine_effort_per_mil_sp = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("1.00"),
        help_text=_("Extra points per 1,000,000 SP the doctrine fit requires."),
    )

    # PVP: points for kills the pilot was an attacker on (Hall of Fame).
    pvp_points_per_kill = models.IntegerField(
        default=1, help_text=_("Points per enemy kill the pilot was involved in.")
    )
    pvp_final_blow_bonus = models.IntegerField(
        default=0, help_text=_("Extra points when the pilot landed the final blow.")
    )

    # PVE: points for the ratting/bounty income the corp receives from a member.
    pve_points_per_mil = models.DecimalField(
        max_digits=8, decimal_places=3, default=Decimal("0.050"),
        help_text=_("Points per 1,000,000 ISK of corp PVE (ratting) income from a member."),
    )
    pve_ref_types = models.CharField(
        max_length=255, default="bounty_prizes,ess_escrow_transfer",
        help_text=_("Corp wallet ref_types that count as members' PVE income "
                    "(comma-separated). The Corp Finance page shows your real ref_types."),
    )

    def __str__(self) -> str:
        return f"weights:{self.name}"

    def pve_ref_type_list(self) -> list[str]:
        return [r.strip() for r in (self.pve_ref_types or "").split(",") if r.strip()]


class MonthlyWeightSnapshot(TimeStampedModel):
    """Frozen contribution-scoring weights for a completed Hall-of-Fame month (4.15).

    The Hall of Fame scores on read with the live weights, so retuning them silently
    reshuffled *past* months' boards. Once a month closes, its board is scored with the
    weights snapshotted here — stable historical recognition. Future-only: months that
    completed before this landed freeze at the weights active when first captured (which
    is exactly what pilots already see), so nothing shifts retroactively on capture.
    ``weights`` is a JSON copy of the scoring fields (see apps.pilots.weights)."""

    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    weights = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["year", "month"], name="uniq_hof_weight_month"),
        ]

    def __str__(self) -> str:
        return f"hof-weights:{self.year:04d}-{self.month:02d}"
