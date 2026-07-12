"""Ship Replacement Program: program settings, rules, claims, budget (PRD §II.5.7).

Planning/record only — nothing here ever moves ISK. Leadership tunes a single
``SrpProgram`` (how losses are valued and how pilots are compensated); an eligible
loss lets a pilot submit a claim; an SRP manager approves/denies (optionally
adjusting the payout) with a reason; the payout is then recorded as made. Open +
approved-unpaid payouts are the corp's SRP exposure.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.doctrines.models import Doctrine
from core.mixins import TimeStampedModel

# Capsule (pod) hull type ids — losses of these are only claimable when the
# program explicitly opts in (most corps SRP ships, not pods).
POD_TYPE_IDS = (670, 33328)


class SrpProgram(TimeStampedModel):
    """Leadership-tunable settings for the whole SRP programme (a singleton).

    This is the answer to "how does our SRP actually work": what we pay (a
    replacement hull, ISK up to value, or just the gap the in-game insurance
    leaves), how we value the loss, and who/what is covered. ``active_program()``
    returns the live one, seeding a sensible default on first use.
    """

    class PayoutMode(models.TextChoices):
        # We hand the pilot a replacement ship + fit (logistics, no ISK transfer).
        REPLACEMENT = "replacement", _("Replacement ship & fit")
        # We pay ISK up to the loss value (acts like full-market insurance).
        ISK_FULL = "isk_full", _("ISK — full loss value")
        # We pay only the gap the official in-game insurance leaves (a top-up).
        ISK_INSURANCE_TOPUP = "isk_topup", _("ISK — top up official insurance")

    class Valuation(models.TextChoices):
        # What the pilot actually lost: hull + destroyed modules, market-priced.
        ACTUAL_LOSS = "actual", _("Actual loss (hull + destroyed modules)")
        # The matching doctrine fit's value (hull + the doctrine's modules).
        DOCTRINE_FIT = "doctrine", _("Doctrine fit value")
        # Hull only — modules are on the pilot.
        HULL_ONLY = "hull", _("Hull only")

    name = models.CharField(max_length=80, default="Standard")
    is_active = models.BooleanField(default=True)
    # Master switch: when off, pilots see the programme is paused and can't claim.
    enabled = models.BooleanField(default=True)

    payout_mode = models.CharField(
        max_length=12, choices=PayoutMode.choices, default=PayoutMode.ISK_FULL
    )
    valuation = models.CharField(
        max_length=10, choices=Valuation.choices, default=Valuation.DOCTRINE_FIT
    )
    # Optional ISK ceiling on any single payout (0 = no cap). A rule's own cap,
    # when set, takes precedence over this default.
    default_cap = models.DecimalField(max_digits=20, decimal_places=2, default=0)

    # Eligibility knobs.
    require_doctrine = models.BooleanField(
        default=True,
        help_text=_("Only losses flying an active doctrine hull are eligible."),
    )
    cover_pod = models.BooleanField(
        default=False, help_text=_("Also cover capsule (pod) losses.")
    )

    # SRP-1 (2.8): fleet-op eligibility gate. When on, a loss only qualifies if it
    # happened during a sanctioned (SRP-covered, non-cancelled) operation's window — so a
    # solo roam / gate camp / rat loss in a doctrine hull no longer drains the budget.
    # Ships OFF (future-only: existing claims are untouched; this only affects new checks).
    require_fleet_op = models.BooleanField(
        default=False,
        help_text=_("Only cover losses during a sanctioned fleet op's window."),
    )
    fleet_op_grace_minutes = models.PositiveIntegerField(
        default=30,
        help_text=_("Minutes of grace added before and after an op window (form-up / travel)."),
    )
    fleet_op_default_duration_minutes = models.PositiveIntegerField(
        default=120,
        help_text=_("Assumed op length when an operation has no explicit duration."),
    )
    fleet_op_require_attendance = models.BooleanField(
        default=False,
        help_text=_("Also require the pilot's recorded attendance (PAP) on that op, not just the window."),
    )

    # 4.6: auto-draft a SUBMITTED claim for an eligible loss so the pilot doesn't have to
    # file it. Off by default; NEVER auto-pays (a draft still needs officer approval). When
    # armed, ``auto_draft_since`` is stamped so only losses AFTER that moment are drafted
    # (future-only — arming never back-drafts historical losses).
    auto_draft_enabled = models.BooleanField(default=False)
    auto_draft_since = models.DateTimeField(null=True, blank=True)

    # Top-up mode: the assumed official-insurance payout as a fraction of the
    # hull's market value (EVE's platinum insurance pays a fixed sum well under
    # market). The SRP then covers the remainder. Tune to match what your pilots
    # actually carry; 0.40 ≈ a typical platinum payout vs Jita hull price.
    insurance_fraction = models.DecimalField(
        max_digits=4, decimal_places=3, default=Decimal("0.400")
    )

    # Leadership-authored explainer shown to pilots on the SRP page.
    intro_text = models.TextField(
        blank=True,
        default=(
            "Lose a doctrine ship on a fleet op and the corp helps you replace it. "
            "Submit a claim from your eligible losses below and an officer reviews it."
        ),
    )

    class Meta:
        ordering = ["-is_active", "-updated_at"]

    def __str__(self) -> str:
        return f"SrpProgram<{self.name}{' active' if self.is_active else ''}>"

    @property
    def is_replacement(self) -> bool:
        return self.payout_mode == self.PayoutMode.REPLACEMENT

    @property
    def is_topup(self) -> bool:
        return self.payout_mode == self.PayoutMode.ISK_INSURANCE_TOPUP


class SrpRule(TimeStampedModel):
    class Basis(models.TextChoices):
        HULL = "hull", _("Hull only")
        FIT = "fit", _("Full fit")
        FIXED = "fixed", _("Fixed cap")

    # A rule with no doctrine applies to any doctrine-tagged loss.
    doctrine = models.ForeignKey(
        Doctrine, on_delete=models.CASCADE, null=True, blank=True, related_name="srp_rules"
    )
    basis = models.CharField(max_length=8, choices=Basis.choices, default=Basis.FIT)
    # For FIXED basis this is the payout; for HULL/FIT it's an optional cap (0 = none).
    max_payout = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    active = models.BooleanField(default=True)

    def __str__(self) -> str:
        scope = self.doctrine.name if self.doctrine else "any doctrine"
        return f"SRP {self.get_basis_display()} · {scope}"


class SrpClaim(TimeStampedModel):
    class Status(models.TextChoices):
        ELIGIBLE = "eligible", _("Eligible")
        SUBMITTED = "submitted", _("Submitted")
        APPROVED = "approved", _("Approved")
        DENIED = "denied", _("Denied")
        PAID = "paid", _("Paid")

    killmail = models.ForeignKey(
        "killboard.Killmail", on_delete=models.CASCADE, related_name="srp_claims"
    )
    claimant = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="srp_claims"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.SUBMITTED, db_index=True
    )
    # 4.6: True when the system auto-drafted this claim from an attended-op loss (still
    # SUBMITTED — an officer must approve it). Shown to the pilot + officer for transparency.
    auto_drafted = models.BooleanField(default=False)
    basis = models.CharField(max_length=8, choices=SrpRule.Basis.choices, default=SrpRule.Basis.FIT)
    # How the corp will compensate this loss, snapshot at claim time so changing
    # the programme later doesn't rewrite history.
    payout_mode = models.CharField(
        max_length=12, choices=SrpProgram.PayoutMode.choices,
        default=SrpProgram.PayoutMode.ISK_FULL,
    )
    # Gross loss value before any insurance offset / cap (what was lost).
    loss_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    # Assumed official-insurance payout deducted in top-up mode (0 otherwise).
    insurance_estimate = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    # The system-computed SRP payout (net of insurance, capped).
    computed_payout = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    # An officer's adjusted payout, set on approval; null = use computed_payout.
    approved_payout = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    # Free-text reference recorded when the payout/replacement is made (e.g. a
    # wallet journal note or "Hawk delivered to Jita IV-4").
    payment_reference = models.CharField(max_length=200, blank=True, default="")
    doctrine = models.ForeignKey(
        Doctrine, on_delete=models.SET_NULL, null=True, blank=True, related_name="srp_claims"
    )
    explanation = models.CharField(max_length=300, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reason = models.CharField(max_length=300, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        # One claim per killmail (a pilot can't double-claim a loss).
        constraints = [
            models.UniqueConstraint(fields=["killmail"], name="uniq_srp_claim_per_killmail")
        ]

    def __str__(self) -> str:
        return f"SRP claim {self.killmail_id} · {self.status}"

    @property
    def payout(self) -> Decimal:
        """The amount that counts as exposure / gets paid: the officer's adjusted
        figure when set, otherwise the system-computed payout."""
        return self.approved_payout if self.approved_payout is not None else self.computed_payout

    @property
    def is_replacement(self) -> bool:
        return self.payout_mode == SrpProgram.PayoutMode.REPLACEMENT


class SrpBudget(TimeStampedModel):
    """A period's SRP allocation, for tracking solvency against exposure."""

    period = models.CharField(max_length=20, unique=True, help_text=_("e.g. 2026-06"))
    allocated = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    # Spend is not stored here — it is derived live from PAID claims by
    # ``services.spent_for_period`` (a stored column would drift out of sync).

    def __str__(self) -> str:
        return f"SRP budget {self.period}"
