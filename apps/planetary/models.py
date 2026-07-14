"""Planetary Industry domain models.

Two layers:

* **Static reference** (``PiMaterial``, ``PiPlanetType``, ``PiPlanetResource``,
  ``PiSchematic``, ``PiSchematicInput``) — the PI "rulebook" derived from the SDE.
  Populated by ``manage.py load_pi_static`` (see management/commands), refreshable
  when CCP changes the game. Never hand-edited.
* **Pilot data** (``PiPlan``, ``PiPlanPlanet``, ``PiColony``) and the leadership
  singleton (``PlanetaryConfig``).

Nothing here moves ISK or writes to the game — plans are estimates and colony rows
are read-only mirrors of ESI, both clearly marked with provenance/assumptions.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from core.mixins import ProvenanceMixin, TimeStampedModel

from . import constants, static_data


class PiTier(models.TextChoices):
    P0 = "P0", _("P0 · Raw resource")
    P1 = "P1", _("P1 · Processed material")
    P2 = "P2", _("P2 · Refined commodity")
    P3 = "P3", _("P3 · Specialised commodity")
    P4 = "P4", _("P4 · Advanced commodity")


# --------------------------------------------------------------------------- #
# Static reference (the PI rulebook, from the SDE)
# --------------------------------------------------------------------------- #
class PiMaterial(models.Model):
    """A PI material (P0–P4). ``type_id`` mirrors the SDE type."""

    type_id = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=200, db_index=True)
    tier = models.CharField(max_length=2, choices=PiTier.choices, db_index=True)
    volume = models.FloatField(default=0.0, help_text=_("m³ per unit — used for hauling sizing."))

    class Meta:
        ordering = ["tier", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.tier})"

    @property
    def schematic(self) -> PiSchematic | None:
        """The single recipe that produces this material (None for P0)."""
        return PiSchematic.objects.filter(output=self).first()


class PiPlanetType(models.Model):
    """One of the 8 planet types, with didactic metadata."""

    type_id = models.IntegerField(primary_key=True)
    slug = models.SlugField(max_length=20, unique=True)
    name = models.CharField(max_length=40)
    best_for = models.CharField(max_length=200, blank=True)
    blurb = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "name"]

    def __str__(self) -> str:
        return self.name

    # --- Render-time i18n seam (Seam A) ------------------------------------ #
    # ``name`` is CCP game data and is NEVER translated. ``best_for``/``blurb`` are our
    # own prose, seeded as English by ``load_pi_static``; they translate here, at render
    # time, keyed on the stable ``slug``. See apps.planetary.static_data.planet_text.
    @property
    def best_for_i18n(self) -> str:
        return static_data.planet_text(self.slug, "best_for", self.best_for)

    @property
    def blurb_i18n(self) -> str:
        return static_data.planet_text(self.slug, "blurb", self.blurb)

    @property
    def resource_materials(self) -> list[PiMaterial]:
        return [r.material for r in self.resources.select_related("material")]


class PiPlanetResource(models.Model):
    """A raw P0 resource extractable on a given planet type (the 8×5 matrix)."""

    planet_type = models.ForeignKey(PiPlanetType, on_delete=models.CASCADE, related_name="resources")
    material = models.ForeignKey(PiMaterial, on_delete=models.CASCADE, related_name="planet_sources")

    class Meta:
        unique_together = ("planet_type", "material")
        ordering = ["planet_type", "material"]

    def __str__(self) -> str:
        return f"{self.planet_type.name} → {self.material.name}"


class PiSchematic(models.Model):
    """A production recipe: N inputs → ``output_quantity`` of ``output`` per cycle."""

    schematic_id = models.IntegerField(primary_key=True)
    name = models.CharField(max_length=200)
    output = models.ForeignKey(PiMaterial, on_delete=models.CASCADE, related_name="produced_by")
    output_quantity = models.IntegerField(default=1)
    cycle_seconds = models.IntegerField(default=3600)
    tier = models.CharField(max_length=2, choices=PiTier.choices, db_index=True)

    class Meta:
        ordering = ["tier", "name"]

    def __str__(self) -> str:
        return self.name

    def runs_per_day(self) -> float:
        return constants.SECONDS_PER_DAY / self.cycle_seconds if self.cycle_seconds else 0.0


class PiSchematicInput(models.Model):
    schematic = models.ForeignKey(PiSchematic, on_delete=models.CASCADE, related_name="inputs")
    material = models.ForeignKey(PiMaterial, on_delete=models.CASCADE, related_name="feeds_into")
    quantity = models.IntegerField()

    class Meta:
        unique_together = ("schematic", "material")

    def __str__(self) -> str:
        return f"{self.quantity}× {self.material.name}"


# --------------------------------------------------------------------------- #
# Leadership config (singleton, mirrors SrpProgram/MentorshipProgram)
# --------------------------------------------------------------------------- #
class PlanetaryConfig(TimeStampedModel):
    """Corp-wide defaults for the PI planner. One active row (see services.active_config)."""

    is_active = models.BooleanField(default=True)
    enabled = models.BooleanField(
        default=True, help_text=_("Master switch shown to pilots. Turning this off hides the "
        "planner even if the feature flag is on."))
    name = models.CharField(max_length=80, default="Standard")

    default_market_region_id = models.IntegerField(default=constants.THE_FORGE)
    default_market_region_name = models.CharField(max_length=80, default="The Forge (Jita)")

    default_customs_export_tax = models.DecimalField(
        max_digits=5, decimal_places=2, default=constants.DEFAULT_CUSTOMS_EXPORT_TAX,
        help_text=_("Customs office (POCO) export tax %, applied to item base value."))
    default_customs_import_tax = models.DecimalField(
        max_digits=5, decimal_places=2, default=constants.DEFAULT_CUSTOMS_IMPORT_TAX)
    default_sales_tax = models.DecimalField(
        max_digits=5, decimal_places=2, default=constants.DEFAULT_SALES_TAX,
        help_text=_("Market sales tax %."))
    default_broker_fee = models.DecimalField(
        max_digits=5, decimal_places=2, default=constants.DEFAULT_BROKER_FEE)
    default_hauling_cost_per_m3 = models.DecimalField(
        max_digits=12, decimal_places=2, default=constants.DEFAULT_HAULING_COST_PER_M3,
        help_text=_("ISK per m³ to move goods to the hub (0 = you self-haul)."))
    corp_buyback_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=constants.DEFAULT_CORP_BUYBACK_RATE,
        help_text=_("Percent of Jita sell the corp buyback pays."))
    default_extraction_rate_per_hour = models.PositiveIntegerField(
        default=constants.DEFAULT_EXTRACTION_RATE_PER_HOUR,
        help_text=_("Planning assumption: P0 units/hour on one extraction planet."))

    recommended_products = models.JSONField(
        default=list, blank=True, help_text=_("Type ids the corp wants pilots to produce."))
    priority_note = models.TextField(blank=True)
    recommended_regions = models.CharField(max_length=200, blank=True)
    default_visibility = models.CharField(max_length=12, default="private")

    class Meta:
        ordering = ["-is_active", "-updated_at"]

    def __str__(self) -> str:
        return f"PlanetaryConfig #{self.pk} ({self.name})"


# --------------------------------------------------------------------------- #
# Pilot plans
# --------------------------------------------------------------------------- #
class PiGoal(models.TextChoices):
    BEGINNER = "beginner", _("Beginner passive income")
    LOW_EFFORT = "low_effort", _("Low-effort extraction")
    P0_P1 = "p0_p1", _("P0 → P1 processing")
    P0_P2 = "p0_p2", _("P0 → P2 refined")
    FACTORY = "factory", _("Factory planet")
    P3_P4 = "p3_p4", _("P3 / P4 advanced production")
    CORP_SUPPLY = "corp_supply", _("Corporation supply chain")
    MAX_PROFIT = "max_profit", _("Maximum ISK profit")


class PiStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    READY = "ready", _("Ready to build")
    ACTIVE = "active", _("Active")
    NEEDS_REVIEW = "needs_review", _("Needs review")
    ARCHIVED = "archived", _("Archived")


class PiVisibility(models.TextChoices):
    PRIVATE = "private", _("Private (only me)")
    LEADERSHIP = "leadership", _("Shared with leadership")
    CORP = "corp", _("Shared with corporation")


class PiEffort(models.TextChoices):
    LOW = "low", _("Low effort")
    DAILY = "daily", _("Daily reset")
    FEW_DAYS = "few_days", _("Every 2–3 days")
    WEEKLY = "weekly", _("Weekly")


class PiRisk(models.TextChoices):
    HIGHSEC = "highsec", _("High-sec convenience")
    LOW_NULL = "low_null", _("Low-sec / null-sec yield")
    WORMHOLE = "wormhole", _("Wormhole production")
    CORP_SPACE = "corp_space", _("Corp-controlled space")


class PiExportStrategy(models.TextChoices):
    SELL_LOCAL = "sell_local", _("Sell locally")
    HAUL_HUB = "haul_hub", _("Haul to market hub")
    CORP_BUYBACK = "corp_buyback", _("Use corp buyback")
    FEED_CHAIN = "feed_chain", _("Feed another chain")


class PiPlanetRole(models.TextChoices):
    EXTRACT = "extract", _("Extraction")
    FACTORY = "factory", _("Factory")
    STORAGE = "storage", _("Storage / staging")


class PiPlan(TimeStampedModel):
    """A pilot's PI plan — a costed, didactic setup for one or more planets."""

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pi_plans")
    character_id = models.BigIntegerField(null=True, blank=True)
    character_name = models.CharField(max_length=200, blank=True)

    name = models.CharField(max_length=200)
    goal = models.CharField(max_length=16, choices=PiGoal.choices, default=PiGoal.BEGINNER)
    status = models.CharField(max_length=16, choices=PiStatus.choices, default=PiStatus.DRAFT)
    visibility = models.CharField(
        max_length=12, choices=PiVisibility.choices, default=PiVisibility.PRIVATE)

    # Location / security context
    system_id = models.IntegerField(null=True, blank=True)
    system_name = models.CharField(max_length=120, blank=True)
    region_id = models.IntegerField(null=True, blank=True)
    planet_count = models.PositiveSmallIntegerField(default=1)

    # Market + economic assumptions (copied from config at creation → self-contained)
    market_region_id = models.IntegerField(default=constants.THE_FORGE)
    market_region_name = models.CharField(max_length=80, default="The Forge (Jita)")
    customs_export_tax = models.DecimalField(
        max_digits=5, decimal_places=2, default=constants.DEFAULT_CUSTOMS_EXPORT_TAX)
    customs_import_tax = models.DecimalField(
        max_digits=5, decimal_places=2, default=constants.DEFAULT_CUSTOMS_IMPORT_TAX)
    sales_tax = models.DecimalField(
        max_digits=5, decimal_places=2, default=constants.DEFAULT_SALES_TAX)
    broker_fee = models.DecimalField(
        max_digits=5, decimal_places=2, default=constants.DEFAULT_BROKER_FEE)
    hauling_cost_per_m3 = models.DecimalField(
        max_digits=12, decimal_places=2, default=constants.DEFAULT_HAULING_COST_PER_M3)
    corp_buyback_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=constants.DEFAULT_CORP_BUYBACK_RATE)
    extraction_rate_per_hour = models.PositiveIntegerField(
        default=constants.DEFAULT_EXTRACTION_RATE_PER_HOUR)

    effort = models.CharField(max_length=10, choices=PiEffort.choices, default=PiEffort.DAILY)
    risk = models.CharField(max_length=12, choices=PiRisk.choices, default=PiRisk.HIGHSEC)
    export_strategy = models.CharField(
        max_length=12, choices=PiExportStrategy.choices, default=PiExportStrategy.HAUL_HUB)

    notes = models.TextField(blank=True)

    # Last computed economics (see calc.plan_economics). Never trusted as input.
    snapshot = models.JSONField(default=dict, blank=True)
    last_priced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["owner", "status"])]

    def __str__(self) -> str:
        return self.name

    @property
    def is_archived(self) -> bool:
        return self.status == PiStatus.ARCHIVED


class PiPlanPlanet(models.Model):
    """One planet inside a plan: its role and what it produces/exports."""

    plan = models.ForeignKey(PiPlan, on_delete=models.CASCADE, related_name="planets")
    planet_type = models.ForeignKey(PiPlanetType, on_delete=models.PROTECT, related_name="+")
    role = models.CharField(max_length=10, choices=PiPlanetRole.choices, default=PiPlanetRole.EXTRACT)
    # What this planet ultimately outputs (a P0/P1 for extraction, a P2+ for a factory).
    primary_material = models.ForeignKey(
        PiMaterial, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # Pilot's measured units/day — overrides the planning estimate when set.
    output_override = models.PositiveIntegerField(null=True, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return f"{self.planet_type.name} ({self.role})"


# --------------------------------------------------------------------------- #
# ESI colony import (read-only mirror; see esi.py)
# --------------------------------------------------------------------------- #
class PiColony(TimeStampedModel, ProvenanceMixin):
    """A snapshot of a real in-game colony from ESI.

    ESI PI layout data only updates when the pilot opens the colony in the EVE
    client, so ``last_update`` (from ESI) can lag reality. The UI must say so.
    """

    character = models.ForeignKey(
        "sso.EveCharacter", on_delete=models.CASCADE, related_name="pi_colonies")
    planet_id = models.BigIntegerField()
    planet_type_id = models.IntegerField(null=True, blank=True)
    planet_type_name = models.CharField(max_length=40, blank=True)
    solar_system_id = models.IntegerField(null=True, blank=True)
    solar_system_name = models.CharField(max_length=120, blank=True)
    upgrade_level = models.PositiveSmallIntegerField(default=0)
    num_pins = models.PositiveSmallIntegerField(default=0)
    # ESI's own "last_update" — when the colony was last touched in the client.
    last_update = models.DateTimeField(null=True, blank=True)
    # Normalised layout: extracted resources, schematics in use, issues, est. output.
    summary = models.JSONField(default=dict, blank=True)
    # PI-2 (3.5): signature of the last issue-set we alerted the pilot about, so a persistent
    # issue nudges at most once (and a re-occurrence after a fix nudges again).
    alerted_sig = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        ordering = ["character", "planet_id"]
        unique_together = ("character", "planet_id")

    def __str__(self) -> str:
        return f"{self.planet_type_name or 'Planet'} colony ({self.character_id})"
