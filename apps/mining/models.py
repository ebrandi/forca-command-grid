"""Mining ledger: participation tracking, mining tax, and profit-split payouts.

The ledger comes from ESI corporation mining observers (refineries record who mined
what ore). We value it at Jita, apply a corp-set mining tax, and let leadership split
operation proceeds among participants proportional to what they mined.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel


class MiningObserver(models.Model):
    """A structure (refinery) that records a mining ledger."""

    observer_id = models.BigIntegerField(primary_key=True)
    observer_type = models.CharField(max_length=32, blank=True)
    name = models.CharField(max_length=200, blank=True)
    last_updated = models.DateField(null=True, blank=True)

    def __str__(self) -> str:
        return self.name or gettext("Observer %(observer_id)s") % {
            "observer_id": self.observer_id
        }


class MiningLedgerEntry(models.Model):
    """One day's mined quantity of one ore type by one pilot at one observer."""

    observer = models.ForeignKey(MiningObserver, on_delete=models.CASCADE, related_name="entries")
    character_id = models.BigIntegerField(db_index=True)
    character_name = models.CharField(max_length=200, blank=True)
    type_id = models.IntegerField()
    quantity = models.BigIntegerField(default=0)
    day = models.DateField(db_index=True)

    class Meta:
        unique_together = ("observer", "character_id", "type_id", "day")
        ordering = ["-day"]


class MiningTaxConfig(TimeStampedModel):
    """The corp's mining tax rate (a fraction of mined value, 0..1). One active row."""

    name = models.CharField(max_length=80, default="Standard")
    rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.1000"))
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-is_active", "-updated_at"]

    def __str__(self) -> str:
        return f"Mining tax {self.rate:.2%}"


class MiningPayout(TimeStampedModel):
    """A leadership-created profit split of operation proceeds over a date window."""

    class Method(models.TextChoices):
        BY_VALUE = "by_value", _("By ISK value mined")
        BY_VOLUME = "by_volume", _("By quantity mined")
        EQUAL = "equal", _("Split equally")

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        FINAL = "final", _("Finalised")

    name = models.CharField(max_length=200)
    period_start = models.DateField()
    period_end = models.DateField()
    pool_isk = models.DecimalField(max_digits=24, decimal_places=2, default=0)
    method = models.CharField(max_length=12, choices=Method.choices, default=Method.BY_VALUE)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=4, default=Decimal("0.1000"))
    total_value = models.DecimalField(max_digits=24, decimal_places=2, default=0)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name


class MiningPayoutLine(models.Model):
    """One participant's share of a payout."""

    payout = models.ForeignKey(MiningPayout, on_delete=models.CASCADE, related_name="lines")
    # Indexed: the member "My mining" page filters lines by the pilot's character ids.
    character_id = models.BigIntegerField(db_index=True)
    character_name = models.CharField(max_length=200, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    value_mined = models.DecimalField(max_digits=24, decimal_places=2, default=0)
    share_pct = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    gross = models.DecimalField(max_digits=24, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=24, decimal_places=2, default=0)
    net = models.DecimalField(max_digits=24, decimal_places=2, default=0)
    paid = models.BooleanField(default=False)

    class Meta:
        ordering = ["-value_mined"]


class MiningMilestone(TimeStampedModel):
    """MIN-4 (3.10): a cumulative-m³ mining milestone a pilot has reached — one row per
    ``(user, threshold_m3)``.

    ``credited`` marks whether reaching it awarded recognition. The FIRST scan snapshots each
    pilot's already-reached milestones as an un-credited baseline (future-only), so only
    crossings AFTER the baseline earn a ContributionEvent.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mining_milestones"
    )
    threshold_m3 = models.BigIntegerField()
    reached_at = models.DateTimeField()
    credited = models.BooleanField(default=False)

    class Meta:
        unique_together = ("user", "threshold_m3")
        ordering = ["user", "threshold_m3"]

    def __str__(self) -> str:
        return f"{self.user_id} → {self.threshold_m3:,} m³"
