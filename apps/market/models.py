"""Market Intelligence: locations, prices, order snapshots."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.mixins import ProvenanceMixin


class MarketLocation(models.Model):
    class LocationType(models.TextChoices):
        STATION = "station", _("Station")
        STRUCTURE = "structure", _("Structure")
        SYSTEM = "system", _("System")
        REGION = "region", _("Region")

    name = models.CharField(max_length=200)
    location_type = models.CharField(max_length=12, choices=LocationType.choices)
    region_id = models.IntegerField(null=True, blank=True)
    system_id = models.IntegerField(null=True, blank=True)
    structure_id = models.BigIntegerField(null=True, blank=True)
    is_price_reference = models.BooleanField(default=False)
    is_staging = models.BooleanField(default=False)
    requires_auth = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    def __str__(self) -> str:
        return self.name


class MarketWatch(models.Model):
    """A pilot's personal watchlist entry — an item type they want to keep an eye on."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="market_watches"
    )
    type_id = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "type_id"], name="uniq_market_watch")
        ]
        indexes = [models.Index(fields=["user"])]

    def __str__(self) -> str:
        return f"watch<{self.user_id}:{self.type_id}>"


class MarketPrice(ProvenanceMixin):
    class Profile(models.TextChoices):
        JITA_SELL = "jita_sell", _("Jita sell")
        JITA_BUY = "jita_buy", _("Jita buy")
        ADJUSTED = "adjusted", _("CCP adjusted")

    # Not independently indexed: the unique_together (type_id, location, profile) below has
    # type_id as its leading column, so it already serves every type_id lookup (R4).
    type_id = models.IntegerField()
    location = models.ForeignKey(
        MarketLocation, on_delete=models.CASCADE, related_name="prices", null=True, blank=True
    )
    buy_max = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    sell_min = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    weighted_avg = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    adjusted_price = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    average_price = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    volume = models.BigIntegerField(null=True, blank=True)
    profile = models.CharField(max_length=12, choices=Profile.choices, default=Profile.JITA_SELL)

    class Meta:
        unique_together = ("type_id", "location", "profile")


class MarketOrderSnapshot(ProvenanceMixin):
    type_id = models.IntegerField(db_index=True)
    location = models.ForeignKey(MarketLocation, on_delete=models.CASCADE, related_name="orders")
    is_buy = models.BooleanField(default=False)
    price = models.DecimalField(max_digits=20, decimal_places=2)
    volume_remain = models.BigIntegerField(default=0)
    min_volume = models.IntegerField(null=True, blank=True)
    duration = models.IntegerField(null=True, blank=True)
    issued = models.DateTimeField(null=True, blank=True)


class MarketHistory(ProvenanceMixin):
    """One day of regional market history for a type (public ESI history)."""

    # type_id not independently indexed: the unique_together (type_id, region_id, date) has
    # it as its leading column (R4). region_id keeps its own index (2nd composite column, used
    # by region-scoped scans).
    type_id = models.IntegerField()
    region_id = models.IntegerField(db_index=True)
    date = models.DateField()
    average = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    highest = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    lowest = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    volume = models.BigIntegerField(default=0)
    order_count = models.BigIntegerField(default=0)

    class Meta:
        unique_together = ("type_id", "region_id", "date")
        ordering = ["-date"]
