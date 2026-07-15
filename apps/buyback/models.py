"""Buyback & appraisal service: a tunable config plus member-posted offers.

A pilot pastes items from the game, gets an instant appraisal priced off the
Jita sell price with a location-based haircut, and can post the lot as an offer.
Other corp/alliance members buy the lot from their corpmate. Leadership control
who may use the service via ``audience`` (same model as the freight service).
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel


class Audience(models.TextChoices):
    PUBLIC = "public", _("Public — anyone can use it")
    ALLIANCE = "alliance", _("Corp & alliance members only")
    CORP = "corp", _("Corp members only")
    DISABLED = "disabled", _("Disabled")


class SecBand(models.TextChoices):
    # Security-band community jargon — kept spelled English inside the msgid.
    HIGHSEC = "highsec", _("Highsec")
    LOWSEC = "lowsec", _("Lowsec")
    NULLSEC = "nullsec", _("Nullsec / wormhole")


class BuybackConfig(TimeStampedModel):
    """Service settings: who may use it and the per-location payout rates.

    Rates are the fraction of the Jita sell price paid, by where the items sit:
    highsec 0.90 (−10%), lowsec 0.85 (−15%), nullsec 0.80 (−20%). Hauling risk
    rises the further out the goods are, so the payout drops to match.
    """

    name = models.CharField(max_length=80, default="Standard")
    is_active = models.BooleanField(default=True)
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.ALLIANCE)

    highsec_pct = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal("0.900"))
    lowsec_pct = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal("0.850"))
    nullsec_pct = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal("0.800"))

    # 4.9: ore/mineral buyback mode. When enabled, sellers can value ore/ice by its refined
    # mineral output (not the ore's own sell price) at ``reprocessing_pct`` effective yield —
    # what the corp actually realises reprocessing it. Off by default (opt-in per config).
    ore_mode_enabled = models.BooleanField(default=False)
    reprocessing_pct = models.DecimalField(
        max_digits=4, decimal_places=3, default=Decimal("0.906"),
        help_text=_("Effective refine yield (skills + structure + rig) used to value ore."),
    )

    class Meta:
        ordering = ["-is_active", "-updated_at"]

    def __str__(self) -> str:
        return f"BuybackConfig<{self.name}{' active' if self.is_active else ''}>"

    def rate_for(self, sec_band: str) -> Decimal:
        return {
            SecBand.HIGHSEC: self.highsec_pct,
            SecBand.LOWSEC: self.lowsec_pct,
            SecBand.NULLSEC: self.nullsec_pct,
        }.get(sec_band, self.nullsec_pct)


class BuybackOffer(TimeStampedModel):
    """A lot a member has put up for buyback at the appraised price.

    ``items`` is the frozen manifest; ``offer_total`` and ``jita_total`` are
    locked at submission so a later price move never changes a live offer. Another
    member buys the whole lot — they pay the seller and take the items in-game.
    """

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        PURCHASED = "purchased", _("Purchased")
        PAID = "paid", _("Paid")
        CANCELLED = "cancelled", _("Cancelled")

    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="buyback_offers",
    )
    seller_character_id = models.BigIntegerField(null=True, blank=True)

    location_name = models.CharField(max_length=200, blank=True)
    sec_band = models.CharField(max_length=10, choices=SecBand.choices, default=SecBand.HIGHSEC)
    rate_pct = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal("0.900"))

    items = models.JSONField(default=list, blank=True)
    item_count = models.IntegerField(default=0)
    volume_m3 = models.FloatField(default=0.0)
    jita_total = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    offer_total = models.DecimalField(max_digits=20, decimal_places=2, default=0)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    notes = models.CharField(max_length=300, blank=True)

    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="buyback_purchases",
    )
    buyer_character_id = models.BigIntegerField(null=True, blank=True)
    purchased_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Buyback lot #{self.pk} · {self.offer_total} ISK ({self.get_status_display()})"

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.OPEN

    @property
    def savings_vs_jita(self) -> Decimal:
        return (self.jita_total or Decimal("0")) - (self.offer_total or Decimal("0"))


class GuaranteedBuybackConfig(TimeStampedModel):
    """Corp-funded GUARANTEED buyback (4.20) — the corp itself commits to buy a member's lot
    at the quoted price, settled by an in-game ISK transfer the app RECONCILES read-only
    against the corp wallet journal. It is the highest-risk, financial feature, so it ships
    INERT and stays that way until leadership deliberately arms it:

      * ``enabled`` is False by default AND ``audience`` starts DISABLED (double-off);
      * every request/approve entry point is gated on both;
      * approvals are budget-capped (per-lot + a rolling 24h ceiling), officer-approved with
        separation of duties, and audited;
      * the app NEVER moves ISK — a treasurer pays in-game and settlement is either an ESI
        wallet-journal match (read-only) or an explicit officer confirmation.
    A singleton (one active row).
    """

    enabled = models.BooleanField(default=False)
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.DISABLED)
    # A single guaranteed lot may not exceed this; a member can't commit the corp to a whale.
    per_lot_cap = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal("100000000"))
    # Rolling 24h ceiling on newly-approved commitments — the staged-rollout throttle.
    daily_budget = models.DecimalField(max_digits=20, decimal_places=2, default=Decimal("1000000000"))
    # When True, a lot only settles on a matching corp-wallet-journal donation (read-only ESI);
    # when False, an officer may confirm settlement manually with a wallet reference.
    require_esi_reconcile = models.BooleanField(default=True)
    intro_text = models.TextField(
        blank=True,
        default=(
            "The corp guarantees to buy your lot at the quoted price. Submit it, an officer "
            "approves, then the corp pays you in-game. No ISK moves through this app."
        ),
    )

    @property
    def intro_text_i18n(self) -> str:
        """Member-facing intro: the translated seed while unedited, else the officer's text
        verbatim. Keyed on the singleton's stable key — see :mod:`apps.buyback.templates_i18n`.
        """
        from . import templates_i18n
        return templates_i18n.intro_text_for(templates_i18n.GUARANTEED_CONFIG_KEY, self.intro_text)

    @classmethod
    def get_solo(cls) -> GuaranteedBuybackConfig:
        obj = cls.objects.order_by("pk").first()
        return obj or cls.objects.create()


class GuaranteedBuyout(TimeStampedModel):
    """One member's request for the corp to guarantee-buy a lot, and its lifecycle. The
    ``quoted_value`` is the corp's committed payout, frozen at request. Nothing here moves
    ISK — ``status`` tracks a real-world (in-game) payment the corp makes and the app then
    reconciles."""

    class Status(models.TextChoices):
        REQUESTED = "requested", _("Requested")
        APPROVED = "approved", _("Approved — awaiting corp payment")
        SETTLED = "settled", _("Settled")
        REJECTED = "rejected", _("Rejected")
        CANCELLED = "cancelled", _("Cancelled")

    class SettlementKind(models.TextChoices):
        ESI = "esi", _("ESI wallet match")
        MANUAL = "manual", _("Officer-confirmed")

    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="guaranteed_buyouts",
    )
    seller_character_id = models.BigIntegerField(null=True, blank=True)
    items = models.JSONField(default=list, blank=True)
    item_count = models.IntegerField(default=0)
    volume_m3 = models.FloatField(default=0.0)
    jita_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    quoted_value = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    location_name = models.CharField(max_length=200, blank=True)
    notes = models.CharField(max_length=300, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.REQUESTED, db_index=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="guaranteed_buyout_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.CharField(max_length=200, blank=True)

    settled_at = models.DateTimeField(null=True, blank=True)
    settlement_kind = models.CharField(max_length=6, choices=SettlementKind.choices, blank=True)
    # The corp-wallet-journal entry_id (ESI) or an officer-entered wallet reference.
    settlement_ref = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self) -> str:
        return f"Guaranteed buyout #{self.pk} · {self.quoted_value} ISK ({self.get_status_display()})"

    @property
    def payment_token(self) -> str:
        """The reference a treasurer puts in the in-game transfer 'reason' so the ESI
        reconciler matches THIS buyout precisely (never a coincidental donation). The
        surrounding dashes make it collision-proof: ``GB-5-`` can't be a substring of
        ``GB-50-``, so a longer buyout's token never falsely matches a shorter one."""
        return f"GB-{self.pk}-"
