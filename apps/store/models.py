"""Corp Store: a built-to-suit ship ordering service.

Alliance pilots order ships — ready-to-fly doctrine fits (priced off the live
Jita sell price plus a markup) or made-to-order hulls including capitals and
supercapitals (built only when ordered, secured by an upfront deposit). Orders
land on a corp-only board where members claim and fulfil them via in-game
contract. ``StoreConfig.audience`` controls who may shop (same model as the
freight and buyback services).
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.doctrines.models import DoctrineFit
from core.mixins import TimeStampedModel


class Audience(models.TextChoices):
    PUBLIC = "public", "Public — anyone can shop"
    ALLIANCE = "alliance", "Corp & alliance members only"
    CORP = "corp", "Corp members only"
    DISABLED = "disabled", "Disabled"


class HullClass(models.TextChoices):
    SUBCAP = "subcap", "Sub-capital"
    CAPITAL = "capital", "Capital"
    SUPERCAPITAL = "supercapital", "Supercapital"


class StoreConfig(TimeStampedModel):
    """Markups, deposit, and audience for the store. One active row is used."""

    name = models.CharField(max_length=80, default="Standard")
    is_active = models.BooleanField(default=True)
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.ALLIANCE)

    # Multipliers on the Jita sell price.
    doctrine_markup = models.DecimalField(max_digits=5, decimal_places=3, default=Decimal("1.100"))
    hull_markup = models.DecimalField(max_digits=5, decimal_places=3, default=Decimal("1.100"))
    # Upfront deposit on a made-to-order build, as a fraction of the total price.
    deposit_pct = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal("0.250"))

    class Meta:
        ordering = ["-is_active", "-updated_at"]

    def __str__(self) -> str:
        return f"StoreConfig<{self.name}{' active' if self.is_active else ''}>"


class StoreOrder(TimeStampedModel):
    """A buyer's order for a ship, fulfilled by a corp member.

    Prices and the manifest are frozen at order time, so a later market move or
    fit edit never changes a live order.
    """

    class Kind(models.TextChoices):
        DOCTRINE_FIT = "doctrine_fit", "Ready-to-fly doctrine ship"
        HULL = "hull", "Made-to-order hull"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLAIMED = "claimed", "Claimed"
        DEPOSIT_PAID = "deposit_paid", "Deposit paid"
        IN_PRODUCTION = "in_production", "In production"
        READY = "ready", "Ready"
        DELIVERED = "delivered", "Delivered"
        CANCELLED = "cancelled", "Cancelled"

    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="store_orders",
    )
    buyer_character_id = models.BigIntegerField(null=True, blank=True)

    kind = models.CharField(max_length=12, choices=Kind.choices)
    doctrine_fit = models.ForeignKey(
        DoctrineFit, on_delete=models.SET_NULL, null=True, blank=True, related_name="store_orders"
    )
    fit_name = models.CharField(max_length=200, blank=True)
    ship_type_id = models.IntegerField()
    ship_name = models.CharField(max_length=200, blank=True)
    hull_class = models.CharField(max_length=12, choices=HullClass.choices, default=HullClass.SUBCAP)

    manifest = models.JSONField(default=list, blank=True)  # [{type_id,name,quantity,unit_jita}]
    quantity = models.IntegerField(default=1)

    unit_jita = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    unit_price = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    total_price = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    markup_pct = models.DecimalField(max_digits=5, decimal_places=3, default=Decimal("1.100"))
    deposit_pct = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal("0.000"))
    deposit_amount = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    requires_build = models.BooleanField(default=False)

    status = models.CharField(max_length=14, choices=Status.choices, default=Status.OPEN)
    location_name = models.CharField(max_length=200, blank=True)
    notes = models.CharField(max_length=300, blank=True)

    claimed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="store_fulfilments",
    )
    claimed_by_character_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Order #{self.pk} · {self.ship_name} ({self.get_status_display()})"

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.OPEN

    @property
    def is_capital(self) -> bool:
        return self.hull_class in (HullClass.CAPITAL, HullClass.SUPERCAPITAL)
