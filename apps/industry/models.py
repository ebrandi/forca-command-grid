"""Industry Planning: projects, items, BOM, blueprints, PI plans."""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.doctrines.models import Doctrine
from apps.market.models import MarketLocation
from core.mixins import ProvenanceMixin, TimeStampedModel


class IndustryEconomyConfig(TimeStampedModel):
    """Leadership-tunable defaults for the Industry & Economy module (singleton).

    Follows the ``.active()`` convention used by DoctrineDisplayConfig / SrpProgram:
    one active row, created on first read. Per-project overrides fall back to these.
    """

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private (owner + leadership)"
        LEADERSHIP = "leadership", "Leadership only"
        CORP = "corp", "Whole corporation"

    is_active = models.BooleanField(default=True)
    # /erp/ backwards-compat: when true, /erp/ redirects into the unified Job Tracker.
    erp_redirects = models.BooleanField(default=True)
    # Market + tax + facility assumptions (fraction, e.g. 0.045 == 4.5%).
    default_market_hub_system_id = models.BigIntegerField(default=30000142)  # Jita
    default_system_cost_index = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.0500"))
    default_facility_tax = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.0025"))
    default_sales_tax = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.0450"))
    default_broker_fee = models.DecimalField(max_digits=6, decimal_places=4, default=Decimal("0.0150"))
    # Corp economics.
    corp_buyback_modifier = models.DecimalField(max_digits=5, decimal_places=3, default=Decimal("0.900"))
    hauling_cost_per_m3 = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    # Governance / UX.
    default_visibility = models.CharField(max_length=12, choices=Visibility.choices, default=Visibility.CORP)
    allow_pilot_plans = models.BooleanField(default=True)
    stale_price_hours = models.PositiveIntegerField(default=24)

    # IND-2 (3.4): decrement a build job's input materials from corp stock on delivery, so
    # stock/shopping lists reflect real burn-down. Ships OFF — leadership arms it after
    # verifying consumption against a known build (clamped so stock never goes negative).
    consume_materials_on_delivery = models.BooleanField(default=False)

    @classmethod
    def active(cls) -> IndustryEconomyConfig:
        cfg = cls.objects.filter(is_active=True).order_by("-updated_at").first()
        return cfg or cls.objects.create(is_active=True)

    def __str__(self) -> str:
        return "Industry & Economy config"


class IndustryProject(TimeStampedModel):
    class Objective(models.TextChoices):
        BUILD = "build", "Build"
        STOCK = "stock", "Stock"
        AMMO = "ammo", "Ammo"
        CONTRACTS = "contracts", "Contracts"
        CUSTOM = "custom", "Custom"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        BLOCKED = "blocked", "Blocked"
        DONE = "done", "Done"
        CANCELLED = "cancelled", "Cancelled"

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private (owner + leadership)"
        LEADERSHIP = "leadership", "Leadership only"
        CORP = "corp", "Whole corporation"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        DOCTRINE_SUPPLY = "doctrine_supply", "Doctrine supply gap"
        STORE_ORDER = "store_order", "Corp Store order"
        STORE_GAP = "store_gap", "Corp Store stock gap"
        ESI_JOB = "esi_job", "Imported ESI job"

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    objective_type = models.CharField(max_length=12, choices=Objective.choices, default=Objective.BUILD)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    visibility = models.CharField(max_length=12, choices=Visibility.choices, default=Visibility.CORP)
    target_location = models.ForeignKey(
        MarketLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="projects"
    )
    linked_doctrine = models.ForeignKey(
        Doctrine, on_delete=models.SET_NULL, null=True, blank=True, related_name="projects"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_projects",
    )
    estimated_cost = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    estimated_value = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)

    # Provenance of the plan (where the demand came from) + integration links.
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.MANUAL)
    store_order_id = models.BigIntegerField(null=True, blank=True, db_index=True)

    # Soft delete / archive (never hard-delete pilot data by default).
    is_archived = models.BooleanField(default=False, db_index=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    # Optional per-plan overrides of IndustryEconomyConfig assumptions (null = inherit).
    sales_tax = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    broker_fee = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    system_cost_index = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    facility_tax = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)

    def __str__(self) -> str:
        return self.name


class IndustryProjectItem(models.Model):
    class BuildOrBuy(models.TextChoices):
        BUILD = "build", "Build"
        BUY = "buy", "Buy"
        UNDECIDED = "undecided", "Undecided"

    class Strategy(models.TextChoices):
        BUILD_VS_BUY = "build_vs_buy", "Build when cheaper"
        BUILD_TO_MINERALS = "build_to_minerals", "Build all the way down"

    class BlueprintSource(models.TextChoices):
        OWN_BPO = "own_bpo", "I own a BPO"
        OWN_BPC = "own_bpc", "I own a BPC"
        BUY = "buy", "Buy the blueprint"
        INVENT = "invent", "Invent a T2 BPC"
        CORP = "corp", "Corporation blueprint"
        UNKNOWN = "unknown", "Not sure — guide me"

    project = models.ForeignKey(IndustryProject, on_delete=models.CASCADE, related_name="items")
    type_id = models.IntegerField()
    product_name = models.CharField(max_length=200, blank=True)  # snapshot for display/history
    quantity = models.BigIntegerField(default=1)
    build_or_buy = models.CharField(
        max_length=10, choices=BuildOrBuy.choices, default=BuildOrBuy.UNDECIDED
    )
    strategy = models.CharField(
        max_length=20, choices=Strategy.choices, default=Strategy.BUILD_VS_BUY
    )
    blueprint_source = models.CharField(
        max_length=10, choices=BlueprintSource.choices, default=BlueprintSource.UNKNOWN
    )
    max_depth = models.PositiveSmallIntegerField(default=8)
    runs = models.IntegerField(null=True, blank=True)
    me = models.PositiveSmallIntegerField(default=0)
    te = models.PositiveSmallIntegerField(default=0)

    # Invention assumptions (used when blueprint_source == invent / item is T2).
    invent_decryptor_type_id = models.IntegerField(null=True, blank=True)
    invent_science_1 = models.PositiveSmallIntegerField(default=0)
    invent_science_2 = models.PositiveSmallIntegerField(default=0)
    invent_encryption = models.PositiveSmallIntegerField(default=0)

    @property
    def invention_inputs(self):
        """Datacores this item needs via invention (T2/T3), if any — informational."""
        from apps.sde.models import SdeBlueprintMaterial

        return SdeBlueprintMaterial.objects.filter(
            product_type_id=self.type_id, activity=SdeBlueprintMaterial.INVENTION
        )


class ProductionStep(models.Model):
    """An intermediate build/react job produced by recursive BOM expansion.

    Ordered deepest-first so the list reads as a build order: make the leaf
    intermediates before the things that consume them.
    """

    project_item = models.ForeignKey(
        IndustryProjectItem, on_delete=models.CASCADE, related_name="production_steps"
    )
    type_id = models.IntegerField()
    activity = models.CharField(max_length=32, default="manufacturing")
    runs = models.IntegerField(default=1)
    output_quantity = models.BigIntegerField(default=1)
    produced_quantity = models.BigIntegerField(default=0)
    required_quantity = models.BigIntegerField(default=0)
    depth = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-depth", "type_id"]


class Blueprint(ProvenanceMixin):
    type_id = models.IntegerField(db_index=True)
    owner_character_id = models.BigIntegerField(null=True, blank=True)
    is_corp = models.BooleanField(default=False)
    me = models.PositiveSmallIntegerField(default=0)
    te = models.PositiveSmallIntegerField(default=0)
    runs = models.IntegerField(null=True, blank=True)
    is_original = models.BooleanField(default=True)
    quantity = models.IntegerField(default=1)


class MaterialRequirement(models.Model):
    class AcquireMethod(models.TextChoices):
        BUILD = "build", "Build"
        BUY = "buy", "Buy"
        REACT = "react", "React"
        INVENT = "invent", "Invent"
        PI = "pi", "PI"
        HAUL = "haul", "Haul"
        CONTRACT = "contract", "Contract"

    project_item = models.ForeignKey(
        IndustryProjectItem, on_delete=models.CASCADE, related_name="material_requirements"
    )
    type_id = models.IntegerField()
    quantity_required = models.BigIntegerField(default=0)
    quantity_available = models.BigIntegerField(default=0)
    quantity_to_acquire = models.BigIntegerField(default=0)
    acquire_method = models.CharField(
        max_length=10, choices=AcquireMethod.choices, default=AcquireMethod.BUY
    )
    unit_cost = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    depth = models.PositiveSmallIntegerField(default=0)


class ShoppingList(TimeStampedModel):
    project = models.ForeignKey(
        IndustryProject, on_delete=models.CASCADE, null=True, blank=True, related_name="shopping_lists"
    )
    name = models.CharField(max_length=200)
    location = models.ForeignKey(
        MarketLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    fmt = models.CharField(max_length=12, default="multibuy")


class ShoppingListItem(models.Model):
    shopping_list = models.ForeignKey(ShoppingList, on_delete=models.CASCADE, related_name="items")
    type_id = models.IntegerField()
    quantity = models.BigIntegerField(default=1)
    estimated_unit_price = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
