"""Stockpile & Logistics: stockpiles, reservations, hauling, contracts."""
from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.industry.models import IndustryProject
from apps.market.models import MarketLocation
from core.mixins import ProvenanceMixin, TimeStampedModel


class Stockpile(ProvenanceMixin):
    class Kind(models.TextChoices):
        CORP = "corp", _("Corp")
        PERSONAL = "personal", _("Personal")

    name = models.CharField(max_length=200)
    location = models.ForeignKey(
        MarketLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="stockpiles"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.CORP)
    owner_character_id = models.BigIntegerField(null=True, blank=True)

    def __str__(self) -> str:
        return self.name


class StockpileItem(ProvenanceMixin):
    class Provenance(models.TextChoices):
        ESI = "esi", "ESI"
        MANUAL = "manual", _("Manual")
        ESTIMATED = "estimated", _("Estimated")

    stockpile = models.ForeignKey(Stockpile, on_delete=models.CASCADE, related_name="items")
    type_id = models.IntegerField()
    quantity_current = models.BigIntegerField(default=0)
    quantity_target = models.BigIntegerField(null=True, blank=True)
    provenance = models.CharField(
        max_length=10, choices=Provenance.choices, default=Provenance.MANUAL
    )

    class Meta:
        unique_together = ("stockpile", "type_id")

    @property
    def quantity_reserved(self) -> int:
        return sum(
            r.quantity_reserved
            for r in self.reservations.filter(status=StockReservation.Status.ACTIVE)
        )

    @property
    def quantity_available(self) -> int:
        return self.quantity_current - self.quantity_reserved


class StockReservation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        CONSUMED = "consumed", _("Consumed")
        RELEASED = "released", _("Released")

    stockpile_item = models.ForeignKey(
        StockpileItem, on_delete=models.CASCADE, related_name="reservations"
    )
    project = models.ForeignKey(
        IndustryProject, on_delete=models.CASCADE, related_name="reservations"
    )
    quantity_reserved = models.BigIntegerField(default=0)
    reserved_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        ordering = ["reserved_at"]  # FIFO


class HaulingTask(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        CLAIMED = "claimed", _("Claimed")
        IN_PROGRESS = "in_progress", _("In progress")
        DONE = "done", _("Done")

    type_id = models.IntegerField(null=True, blank=True)
    manifest = models.JSONField(default=list, blank=True)
    quantity = models.BigIntegerField(null=True, blank=True)
    volume_m3 = models.FloatField(default=0.0)
    source_location = models.ForeignKey(
        MarketLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="haul_sources"
    )
    dest_location = models.ForeignKey(
        MarketLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="haul_dests"
    )
    route_jumps = models.IntegerField(null=True, blank=True)
    project = models.ForeignKey(
        IndustryProject, on_delete=models.SET_NULL, null=True, blank=True, related_name="haul_tasks"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    claimed_by_character_id = models.BigIntegerField(null=True, blank=True)


class AssetLocation(models.Model):
    """A resolved EVE location where assets sit (station / system / structure).

    Cached so we resolve each ``location_id`` once. Structures we can't read
    (no docking/scope) are still recorded by id so assets group correctly.
    """

    class Kind(models.TextChoices):
        STATION = "station", _("Station")
        SOLAR_SYSTEM = "solar_system", _("Solar system")
        STRUCTURE = "structure", _("Structure")
        OTHER = "other", _("Other")

    location_id = models.BigIntegerField(primary_key=True)
    name = models.CharField(max_length=200, blank=True)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.OTHER)
    system_id = models.IntegerField(null=True, blank=True)
    region_id = models.IntegerField(null=True, blank=True)

    def __str__(self) -> str:
        if self.name:
            return self.name
        label = "Structure" if self.kind == self.Kind.STRUCTURE else "Location"
        return f"{label} {self.location_id}"


class Asset(ProvenanceMixin):
    """A live snapshot of assets owned by the corp or a character, by location.

    Read-only mirror of ESI (separate from the manual planning Stockpiles):
    aggregated quantity per (owner, location, type). Corp assets come from a
    Director token; personal assets from each pilot's own token.
    """

    class Owner(models.TextChoices):
        CORPORATION = "corporation", _("Corporation")
        CHARACTER = "character", _("Character")

    owner_type = models.CharField(max_length=12, choices=Owner.choices, db_index=True)
    owner_id = models.BigIntegerField(db_index=True)
    location = models.ForeignKey(
        AssetLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="assets"
    )
    type_id = models.IntegerField(db_index=True)
    quantity = models.BigIntegerField(default=0)

    class Meta:
        unique_together = ("owner_type", "owner_id", "location", "type_id")
        indexes = [models.Index(fields=["owner_type", "owner_id"])]


