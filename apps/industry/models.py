"""Industry Planning: projects, items, BOM, blueprints, PI plans."""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.doctrines.models import Doctrine
from apps.market.models import MarketLocation
from apps.sso.models import EveCharacter
from core.mixins import ProvenanceMixin, TimeStampedModel


class IndustryEconomyConfig(TimeStampedModel):
    """Leadership-tunable defaults for the Industry & Economy module (singleton).

    Follows the ``.active()`` convention used by DoctrineDisplayConfig / SrpProgram:
    one active row, created on first read. Per-project overrides fall back to these.
    """

    class Visibility(models.TextChoices):
        PRIVATE = "private", _("Private (owner + leadership)")
        LEADERSHIP = "leadership", _("Leadership only")
        CORP = "corp", _("Whole corporation")

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
        BUILD = "build", _("Build")
        STOCK = "stock", _("Stock")
        AMMO = "ammo", _("Ammo")
        CONTRACTS = "contracts", _("Contracts")
        CUSTOM = "custom", _("Custom")

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        ACTIVE = "active", _("Active")
        BLOCKED = "blocked", _("Blocked")
        DONE = "done", _("Done")
        CANCELLED = "cancelled", _("Cancelled")

    class Visibility(models.TextChoices):
        PRIVATE = "private", _("Private (owner + leadership)")
        LEADERSHIP = "leadership", _("Leadership only")
        CORP = "corp", _("Whole corporation")

    class Source(models.TextChoices):
        MANUAL = "manual", _("Manual")
        DOCTRINE_SUPPLY = "doctrine_supply", _("Doctrine supply gap")
        STORE_ORDER = "store_order", _("Corp Store order")
        STORE_GAP = "store_gap", _("Corp Store stock gap")
        ESI_JOB = "esi_job", _("Imported ESI job")
        MRP = "mrp", _("MRP planning run")

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
        BUILD = "build", _("Build")
        BUY = "buy", _("Buy")
        UNDECIDED = "undecided", _("Undecided")

    class Strategy(models.TextChoices):
        BUILD_VS_BUY = "build_vs_buy", _("Build when cheaper")
        BUILD_TO_MINERALS = "build_to_minerals", _("Build all the way down")

    class BlueprintSource(models.TextChoices):
        OWN_BPO = "own_bpo", _("I own a BPO")
        OWN_BPC = "own_bpc", _("I own a BPC")
        BUY = "buy", _("Buy the blueprint")
        INVENT = "invent", _("Invent a T2 BPC")
        CORP = "corp", _("Corporation blueprint")
        UNKNOWN = "unknown", _("Not sure — guide me")

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

    # Human labels for the SDE activity code. The stored ``activity`` stays the canonical
    # code every branch compares against; only the rendered half is translated.
    ACTIVITY_LABELS = {
        "manufacturing": _("Manufacturing"),
        "reaction": _("Reaction"),
        "invention": _("Invention"),
    }

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

    @property
    def activity_label(self) -> str:
        return self.ACTIVITY_LABELS.get(self.activity, self.activity)


class MaterialRequirement(models.Model):
    class AcquireMethod(models.TextChoices):
        BUILD = "build", _("Build")
        BUY = "buy", _("Buy")
        REACT = "react", _("React")
        INVENT = "invent", _("Invent")
        PI = "pi", "PI"
        HAUL = "haul", _("Haul")
        CONTRACT = "contract", _("Contract")

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


# --------------------------------------------------------------------------- #
#  MRP v1 (P3) — corp-wide net requirements
# --------------------------------------------------------------------------- #
class MrpConfig(TimeStampedModel):
    """Leadership-tunable MRP knobs (P3). Singleton via ``active()``."""

    is_active = models.BooleanField(default=True, db_default=True)
    consolidation_window_days = models.PositiveIntegerField(
        default=28, db_default=28,
        help_text=_("Dated demand beyond this many days ahead is excluded from the "
                    "plan and listed separately."),
    )
    buy_lead_days = models.PositiveIntegerField(
        default=3, db_default=3,
        help_text=_("Assumed days to buy an item at the price-reference hub."),
    )
    import_lead_days = models.PositiveIntegerField(
        default=5, db_default=5,
        help_text=_("Assumed days to buy at Jita and freight to the destination."),
    )
    include_ready_jobs = models.BooleanField(
        default=True, db_default=True,
        help_text=_("Count finished-but-undelivered ESI jobs (status \"ready\") as incoming supply."),
    )
    auto_run_enabled = models.BooleanField(
        default=False, db_default=False,
        help_text=_("Run the planning job automatically on its nightly schedule. "
                    "Off: officers run it manually from the Material Plan page."),
    )
    # P5 (manufacturing capacity). Ships inert: off, the feasible pass is byte-identical
    # to P3 (unconstrained industry slots). On, build dates are held to committed
    # capacity and the bottleneck is named — never a promise the corp cannot hit.
    capacity_enabled = models.BooleanField(
        default=False, db_default=False,
        help_text=_("Hold build dates to committed manufacturing capacity (slots, "
                    "skills, blueprints, facilities) and name the bottleneck. Off: "
                    "dates assume unlimited industry slots (the P3 behaviour)."),
    )
    capacity_skill_stale_days = models.PositiveSmallIntegerField(
        default=14, db_default=14,
        help_text=_("A pilot's slots become \"unknown\" (excluded from measured "
                    "capacity) when their skills snapshot is older than this. Unknown "
                    "is never treated as zero."),
    )
    default_me = models.PositiveSmallIntegerField(
        default=0, db_default=0,
        help_text=_("Material-efficiency assumed for every build level of the explosion."),
    )
    max_depth = models.PositiveSmallIntegerField(
        default=8, db_default=8,
        help_text=_("Deepest BOM level the explosion will recurse to."),
    )

    class Meta:
        ordering = ["-is_active", "-updated_at"]
        verbose_name = _("MRP config")
        verbose_name_plural = _("MRP configs")

    def __str__(self) -> str:
        return f"MrpConfig #{self.pk}{' active' if self.is_active else ''}"

    @classmethod
    def active(cls) -> MrpConfig:
        cfg = cls.objects.filter(is_active=True).order_by("-updated_at").first()
        if cfg is None:
            cfg = cls.objects.create(is_active=True)
        return cfg


class MrpRun(models.Model):
    """One planning run: the single-flight guard, the stats and the input digest.

    At most one row may be ``running`` (partial unique). A crashed run is
    recovered precisely: a new trigger finding a ``running`` row whose
    ``heartbeat_at`` is stale flips it to ``failed`` and claims its own row in
    the same transaction — the constraint makes exactly one claimant win.
    """

    class Status(models.TextChoices):
        RUNNING = "running", _("Running")
        DONE = "done", _("Done")
        FAILED = "failed", _("Failed")

    status = models.CharField(
        max_length=8, choices=Status.choices, default=Status.RUNNING,
        db_default=Status.RUNNING, db_index=True,
    )
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    stats = models.JSONField(
        blank=True, default=dict, db_default=models.Value({}, models.JSONField())
    )
    inputs_digest = models.CharField(max_length=64, blank=True, default="", db_default="")

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["status"],
                condition=models.Q(status="running"),
                name="uniq_running_mrp_run",
            ),
        ]

    def __str__(self) -> str:
        return f"MrpRun #{self.pk} {self.status}"


class NetRequirement(TimeStampedModel):
    """One live netted material requirement per (type, location) — the
    ``FitSupplyNeed`` pattern generalised to arbitrary types.

    Quantities are FINISHED UNITS, always — blueprint runs never leave the BOM
    layer. ``sources`` is demand provenance (fit_demand/supply_need/parent/
    vehicle refs), ``incoming_refs`` is supply provenance (esi_job/build_job/
    project_item refs; ESI jobs by ``job_id``, never pk — the table is
    snapshot-replaced). Together they make every displayed number decomposable.
    """

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        IN_PROGRESS = "in_progress", _("In progress")
        DONE = "done", _("Done")
        CANCELLED = "cancelled", _("Cancelled")

    type_id = models.IntegerField(db_index=True)
    location = models.ForeignKey(
        MarketLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.OPEN,
        db_default=Status.OPEN, db_index=True,
    )
    net_quantity = models.BigIntegerField(default=0, db_default=0)
    gross_quantity = models.BigIntegerField(default=0, db_default=0)
    available_quantity = models.BigIntegerField(default=0, db_default=0)
    incoming_quantity = models.BigIntegerField(default=0, db_default=0)
    required_by = models.DateTimeField(null=True, blank=True)
    # Codes stay machine-English; labels resolve at render time (SUGGESTION_LABELS).
    suggestion = models.CharField(max_length=8, blank=True, default="", db_default="")
    feasible_at = models.DateTimeField(null=True, blank=True)
    feasible_source = models.CharField(max_length=12, blank=True, default="", db_default="")
    # P5: which constraint governs this row's capacity-armed date — a machine code
    # (slots|skills|blueprint|facility|materials|unmeasured|""), rendered through
    # BOTTLENECK_LABELS. Empty on every pre-capacity row and when nothing binds.
    bottleneck_code = models.CharField(max_length=12, blank=True, default="", db_default="")
    depth = models.PositiveSmallIntegerField(default=0, db_default=0)
    sources = models.JSONField(
        blank=True, default=list, db_default=models.Value([], models.JSONField())
    )
    incoming_refs = models.JSONField(
        blank=True, default=list, db_default=models.Value([], models.JSONField())
    )
    diverged = models.BooleanField(default=False, db_default=False)
    last_run = models.ForeignKey(
        MrpRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    industry_project = models.ForeignKey(
        IndustryProject, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    build_job = models.ForeignKey(
        "erp.BuildJob", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    hauling_task = models.ForeignKey(
        "stockpile.HaulingTask", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    task = models.ForeignKey(
        "tasks.Task", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    purchase_order = models.ForeignKey(
        "procurement.PurchaseOrder", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    # P6: the freight-batch line consolidating this row's import/buy demand — one
    # more vehicle in the fan-out lattice. Joins ``has_vehicle`` and the fan-out guards.
    freight_line = models.ForeignKey(
        "logistics.FreightBatchLine", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["depth", "type_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["type_id", "location"],
                condition=models.Q(status__in=("open", "in_progress")),
                nulls_distinct=False,
                name="uniq_live_netrequirement_per_type_location",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "suggestion"]),
            models.Index(fields=["status", "depth"]),
        ]

    def __str__(self) -> str:
        return f"NetRequirement<{self.type_id}@{self.location_id} net={self.net_quantity} {self.status}>"

    @property
    def has_vehicle(self) -> bool:
        return bool(
            self.industry_project_id or self.build_job_id
            or self.hauling_task_id or self.task_id or self.purchase_order_id
            or self.freight_line_id
        )


class ProductionResource(ProvenanceMixin):
    """One (pilot, activity class) manufacturing-capacity row — the P5 resource ledger.

    Deliberately NOT per location: EVE industry slots are pilot-global (the Mass
    Production line skills), so a per-(pilot, location) pool would double-count a
    pilot running jobs in two systems — location is an attribute of the *load*,
    never of the pool.

    Derived fields (``slots_total``, ``as_of``) come from the pilot's latest skill
    snapshot and are rewritten on each derivation (only when changed). Officer-entered
    fields (override, weekly cap, window, pause) are preserved across derivations —
    they are the human's governor over the measurement.

    Rows have no status lifecycle: a resource is deleted when the pilot stops
    qualifying (grant revoked / left corp), never soft-closed — hence a plain unique
    constraint, no partial-unique one-live-row pattern.
    """

    class ActivityClass(models.TextChoices):
        MANUFACTURING = "manufacturing", _("Manufacturing")
        REACTION = "reaction", _("Reaction")
        SCIENCE = "science", _("Science")

    character = models.ForeignKey(
        EveCharacter, on_delete=models.CASCADE, related_name="production_resources"
    )
    activity_class = models.CharField(max_length=13, choices=ActivityClass.choices)
    # Derived from skills: 1 + base line skill + advanced line skill. NULL means
    # *unknown* (no/stale snapshot), which is never treated as zero (honest-data rule).
    slots_total = models.PositiveSmallIntegerField(null=True, blank=True)
    # Officer overrides — preserved across re-derivation.
    manual_slots_override = models.PositiveSmallIntegerField(null=True, blank=True)
    max_weekly_output = models.PositiveIntegerField(
        null=True, blank=True,
        help_text=_("Cap on finished units/week the scheduler will book onto this "
                    "pilot — a crude cadence governor counted across all types, not a "
                    "per-job ceiling. Blank means uncapped."),
    )
    unavailable_from = models.DateTimeField(null=True, blank=True)
    unavailable_until = models.DateTimeField(null=True, blank=True)
    is_paused = models.BooleanField(
        default=False, db_default=False,
        help_text=_("Don't schedule new work onto this pilot."),
    )

    class Meta:
        ordering = ["character_id", "activity_class"]
        constraints = [
            models.UniqueConstraint(
                fields=["character", "activity_class"],
                name="uniq_resource_per_char_activity",
            ),
        ]
        indexes = [models.Index(fields=["activity_class"])]

    def __str__(self) -> str:
        return f"resource<{self.character_id}:{self.activity_class} slots={self.effective_slots}>"

    @property
    def effective_slots(self) -> int | None:
        """The slot count the scheduler books against: the officer override when set,
        else the derived total. ``None`` = unknown (never bookable, never zero)."""
        if self.manual_slots_override is not None:
            return self.manual_slots_override
        return self.slots_total
