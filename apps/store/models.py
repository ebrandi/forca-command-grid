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
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.doctrines.models import DoctrineFit
from apps.market.models import MarketLocation
from core.mixins import TimeStampedModel


class Audience(models.TextChoices):
    PUBLIC = "public", _("Public — anyone can shop")
    ALLIANCE = "alliance", _("Corp & alliance members only")
    CORP = "corp", _("Corp members only")
    DISABLED = "disabled", _("Disabled")


class HullClass(models.TextChoices):
    # Capital-ship-class community jargon (sub-cap / capital / super) — kept English.
    SUBCAP = "subcap", "Sub-capital"
    CAPITAL = "capital", "Capital"
    SUPERCAPITAL = "supercapital", "Supercapital"


class PriceBasis(models.TextChoices):
    """What a store price was computed from — frozen on the order."""

    JITA = "jita", _("Jita sell price")
    BUILD = "build", _("Estimated build cost")


class OfferState(models.TextChoices):
    """The customer-facing availability of a doctrine-fit offer on the Shipyard.

    Derived — never stored on the fit — by the one authoritative service in
    :mod:`apps.store.availability`. Orders freeze the state they were placed under.
    """

    READY = "ready", _("Ready for immediate delivery")
    LIMITED = "limited", _("Limited stock")
    BACKORDER = "backorder", _("Available on backorder")
    UNAVAILABLE = "unavailable", _("Temporarily unavailable")
    NOT_OFFERED = "not_offered", _("Not offered for sale")


class OrderAvailability(models.TextChoices):
    """How an order's quantity was covered at order time — frozen for audit.

    Blank on orders that predate availability control; those are never
    reinterpreted as stock-backed.
    """

    READY = "ready", _("Reserved from stock")
    PARTIAL = "partial", _("Partially reserved, remainder backordered")
    BACKORDER = "backorder", _("Backordered")


class FulfilmentMethod(models.TextChoices):
    """Leadership's preferred way to source a doctrine fit when stock runs out."""

    STOCK = "stock", _("Use stocked ships")
    BUILD = "build", _("Build locally")
    IMPORT = "import", _("Import from Jita")
    SUPPLIER = "supplier", _("Buy from an approved supplier")
    AUTO = "auto", _("Choose build or buy automatically")


class StoreConfig(TimeStampedModel):
    """Markups, deposit, and audience for the store. One active row is used."""

    name = models.CharField(max_length=80, default="Standard")
    is_active = models.BooleanField(default=True)
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.ALLIANCE)

    # Multipliers on the Jita sell price (doctrine fits and sub-capital hulls).
    doctrine_markup = models.DecimalField(max_digits=5, decimal_places=3, default=Decimal("1.100"))
    hull_markup = models.DecimalField(max_digits=5, decimal_places=3, default=Decimal("1.100"))
    # Multipliers on the estimated production cost. Capital-class hulls aren't bought
    # off the Jita market — they're manufactured to order — so leaders set the profit
    # margin over build cost per class instead of a Jita markup.
    # ``db_default`` is load-bearing for rollback safety: it keeps the column DEFAULT in
    # the database, so pre-migration code INSERTing without these columns still works.
    capital_markup = models.DecimalField(
        max_digits=5, decimal_places=3, default=Decimal("1.100"), db_default=Decimal("1.100")
    )
    supercap_markup = models.DecimalField(
        max_digits=5, decimal_places=3, default=Decimal("1.100"), db_default=Decimal("1.100")
    )
    # Upfront deposit on a made-to-order build, as a fraction of the total price.
    deposit_pct = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal("0.250"))

    def markup_for_hull(self, hull_class: str) -> Decimal:
        """The configured multiplier for a made-to-order hull of ``hull_class``."""
        return {
            HullClass.CAPITAL: self.capital_markup,
            HullClass.SUPERCAPITAL: self.supercap_markup,
        }.get(hull_class, self.hull_markup)

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
        DOCTRINE_FIT = "doctrine_fit", _("Ready-to-fly doctrine ship")
        HULL = "hull", _("Made-to-order hull")

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        CLAIMED = "claimed", _("Claimed")
        DEPOSIT_PAID = "deposit_paid", _("Deposit paid")
        IN_PRODUCTION = "in_production", _("In production")
        READY = "ready", _("Ready")
        DELIVERED = "delivered", _("Delivered")
        CANCELLED = "cancelled", _("Cancelled")

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
    # What unit_price was computed from: Jita sell (subcaps, doctrine fits) or the
    # estimated build cost (capital-class hulls). ``unit_cost`` freezes that estimate.
    # ``db_default`` keeps the DB column DEFAULT so pre-migration code can still INSERT.
    price_basis = models.CharField(
        max_length=5, choices=PriceBasis.choices, default=PriceBasis.JITA,
        db_default=PriceBasis.JITA,
    )
    unit_cost = models.DecimalField(
        max_digits=20, decimal_places=2, default=0, db_default=Decimal("0")
    )
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

    # --- Availability freeze (SHIP-1) ------------------------------------- #
    # What the buyer was shown and promised at order time. Frozen like price and
    # manifest: later stock moves, fit edits or policy changes never rewrite a
    # live order. All columns carry db_default so a code-only rollback stays
    # INSERT-safe (see deploy runbook), and blank/zero means "legacy order,
    # placed before availability control existed".
    availability_state = models.CharField(
        max_length=10, choices=OrderAvailability.choices, blank=True, default="", db_default="",
    )
    quantity_reserved = models.IntegerField(default=0, db_default=0)
    quantity_backordered = models.IntegerField(default=0, db_default=0)
    delivery_location = models.ForeignKey(
        MarketLocation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="store_orders",
    )
    # The exact fit revision the order was placed against (see
    # ``apps.store.availability.manifest_hash``); guards stocked ships built for an
    # older revision from silently satisfying a newer one.
    manifest_hash = models.CharField(max_length=64, blank=True, default="", db_default="")
    backorder_acknowledged = models.BooleanField(default=False, db_default=False)
    lead_days_assumed = models.PositiveIntegerField(null=True, blank=True)

    # The order-time promise (immutable once set) vs the living estimate. Estimates
    # are never guarantees — the UI must say "Estimated delivery".
    promised_date = models.DateTimeField(null=True, blank=True)
    current_eta = models.DateTimeField(null=True, blank=True)
    eta_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    eta_changed_at = models.DateTimeField(null=True, blank=True)
    # Officer/claimer-typed free text; rendered verbatim (never machine-translated).
    delay_reason = models.CharField(max_length=300, blank=True, default="", db_default="")
    actual_ready_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["current_eta"]),
        ]

    def __str__(self) -> str:
        return f"Order #{self.pk} · {self.ship_name} ({self.get_status_display()})"

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.OPEN

    @property
    def is_capital(self) -> bool:
        return self.hull_class in (HullClass.CAPITAL, HullClass.SUPERCAPITAL)

    @property
    def is_split(self) -> bool:
        """Part reserved from stock, part backordered (split disclosed at order time)."""
        return self.quantity_reserved > 0 and self.quantity_backordered > 0

    @property
    def has_backorder(self) -> bool:
        return self.quantity_backordered > 0

    @property
    def is_overdue(self) -> bool:
        """Past its current estimate and still undelivered. Computed, never a status."""
        from django.utils import timezone

        if self.current_eta is None or self.status in (
            self.Status.READY, self.Status.DELIVERED, self.Status.CANCELLED
        ):
            return False
        return self.current_eta < timezone.now()


# --------------------------------------------------------------------------- #
#  Shipyard availability control (SHIP-1): policy, per-fit offers, fitted-ship
#  inventory, reservations, supply needs. A complete doctrine ship is a bundle
#  (hull + exact modules), not an EVE type — so it gets its own small inventory
#  model instead of overloading the per-type StockpileItem. Balances are
#  ledger-backed: every change writes an immutable FitStockEntry.
# --------------------------------------------------------------------------- #


class ShipyardPolicy(TimeStampedModel):
    """Corp-wide Shipyard fulfilment policy (officer-set; one active row).

    Per-fit :class:`FitOffer` rows override these; a NULL override inherits.
    Follows the ``.active()`` singleton convention (StoreConfig, DoctrineDisplayConfig).
    """

    is_active = models.BooleanField(default=True)

    # Backorders default ON so enabling availability control doesn't flip the whole
    # Shipyard to "unavailable" before any stock is recorded — every fit simply
    # becomes an honest backorder instead of an implied ready ship.
    backorders_enabled = models.BooleanField(
        default=True, db_default=True,
        help_text=_("Accept orders for doctrine ships that are not in stock."),
    )
    default_lead_days = models.PositiveIntegerField(
        default=7, db_default=7,
        help_text=_("Default estimated days to fulfil a backorder."),
    )
    allow_partial_fulfilment = models.BooleanField(
        default=True, db_default=True,
        help_text=_("Allow one order to combine reserved stock with a backordered remainder."),
    )
    reservation_expiry_days = models.PositiveIntegerField(
        default=0, db_default=0,
        help_text=_("Release reservations of orders nobody has claimed after this many days (0 = never)."),
    )
    default_location = models.ForeignKey(
        MarketLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        help_text=_("Where doctrine ships are stocked and delivered unless a fit overrides it."),
    )
    max_order_quantity = models.PositiveIntegerField(
        default=10, db_default=10,
        help_text=_("Most ships one order may request unless a fit overrides it."),
    )
    limited_stock_threshold = models.PositiveIntegerField(
        default=2, db_default=2,
        help_text=_("Show 'Limited stock' when this many or fewer ships remain."),
    )
    show_unavailable = models.BooleanField(
        default=True, db_default=True,
        help_text=_("Keep out-of-stock ships visible on the Shipyard (greyed out) instead of hiding them."),
    )
    available_only_default = models.BooleanField(
        default=False, db_default=False,
        help_text=_("Open the Shipyard filtered to immediately available ships."),
    )
    waitlist_enabled = models.BooleanField(
        default=False, db_default=False,
        help_text=_("Let pilots join a waitlist for ships that can't be ordered."),
    )
    auto_allocate_receipts = models.BooleanField(
        default=True, db_default=True,
        help_text=_("When stock arrives, reserve it for waiting backorders automatically (oldest first)."),
    )

    class Meta:
        ordering = ["-is_active", "-updated_at"]
        verbose_name = _("shipyard policy")
        verbose_name_plural = _("shipyard policies")

    def __str__(self) -> str:
        return f"ShipyardPolicy #{self.pk}{' active' if self.is_active else ''}"

    @classmethod
    def active(cls) -> ShipyardPolicy:
        policy = cls.objects.filter(is_active=True).order_by("-updated_at").first()
        if policy is None:
            policy = cls.objects.create(is_active=True)
        return policy


class FitOffer(TimeStampedModel):
    """Per-doctrine-fit sales and stocking policy. NULL fields inherit ShipyardPolicy."""

    fit = models.OneToOneField(DoctrineFit, on_delete=models.CASCADE, related_name="offer")
    is_offered = models.BooleanField(
        default=True, db_default=True,
        help_text=_("Offer this fit for sale on the Shipyard."),
    )
    backorders_allowed = models.BooleanField(
        null=True, blank=True, help_text=_("Override the corp-wide backorder policy for this fit."),
    )
    lead_days = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("Override the default backorder lead time (days)."),
    )
    delivery_location = models.ForeignKey(
        MarketLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        help_text=_("Override the default delivery location for this fit."),
    )
    max_backorder_quantity = models.PositiveIntegerField(
        null=True, blank=True,
        help_text=_("Most units allowed on backorder per order for this fit."),
    )
    max_per_order = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("Override the maximum quantity per order."),
    )
    safety_stock = models.PositiveIntegerField(
        default=0, db_default=0,
        help_text=_("Units held back from planning as a strategic reserve."),
    )
    reorder_point = models.PositiveIntegerField(
        null=True, blank=True,
        help_text=_("Flag this fit for restocking when available stock falls to this level."),
    )
    target_stock = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("How many complete ships the corp aims to keep stocked."),
    )
    preferred_fulfilment = models.CharField(
        max_length=10, choices=FulfilmentMethod.choices, default=FulfilmentMethod.AUTO,
        db_default=FulfilmentMethod.AUTO,
    )
    # Officer-typed prose, shown verbatim (never machine-translated).
    buyer_notes = models.CharField(
        max_length=300, blank=True, default="", db_default="",
        help_text=_("Shown to buyers on the Shipyard card."),
    )
    internal_notes = models.CharField(
        max_length=300, blank=True, default="", db_default="",
        help_text=_("Fulfilment notes visible to members and officers only."),
    )
    priority = models.IntegerField(
        default=0, db_default=0, help_text=_("Higher = restock sooner."),
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["fit_id"]

    def __str__(self) -> str:
        return f"FitOffer<{self.fit_id}>"


class FitStock(TimeStampedModel):
    """Complete, deliverable ships of one doctrine fit at one location.

    ``quantity_on_hand`` is the transactional balance (guarded by row locks and a
    non-negative check); its full history is the :class:`FitStockEntry` ledger.
    ``manifest_hash`` records which revision of the fit these ships were assembled
    to — stock stranded by a fit edit stops counting toward availability until an
    officer revalidates it.
    """

    doctrine_fit = models.ForeignKey(DoctrineFit, on_delete=models.CASCADE, related_name="stocks")
    location = models.ForeignKey(MarketLocation, on_delete=models.PROTECT, related_name="fit_stocks")
    quantity_on_hand = models.BigIntegerField(default=0, db_default=0)
    manifest_hash = models.CharField(max_length=64, blank=True, default="", db_default="")
    last_reconciled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # One row per fit revision at a location: stock stranded by a fit edit
            # keeps its old hash (and stops counting) while fresh builds of the new
            # revision accrue in their own row.
            models.UniqueConstraint(
                fields=["doctrine_fit", "location", "manifest_hash"],
                name="uniq_fitstock_fit_location_hash",
            ),
            models.CheckConstraint(
                condition=Q(quantity_on_hand__gte=0), name="fitstock_on_hand_gte_0"
            ),
        ]

    def __str__(self) -> str:
        return f"FitStock<fit={self.doctrine_fit_id} loc={self.location_id} n={self.quantity_on_hand}>"


class FitStockEntry(models.Model):
    """One immutable ledger line explaining a FitStock balance change.

    ``kind`` is a code (label translated at render time); ``reason`` is the
    officer's own words, kept verbatim. Rows are never updated or deleted.
    """

    class Kind(models.TextChoices):
        RECEIPT = "receipt", _("Stock received")
        ADJUSTMENT = "adjustment", _("Manual adjustment")
        CONSUMED = "consumed", _("Delivered to buyer")
        RECONCILIATION = "reconciliation", _("Reconciliation")
        REVALIDATION = "revalidation", _("Fit revision revalidated")

    stock = models.ForeignKey(FitStock, on_delete=models.CASCADE, related_name="entries")
    kind = models.CharField(max_length=14, choices=Kind.choices)
    delta = models.BigIntegerField(default=0)
    balance_after = models.BigIntegerField(default=0)
    reason = models.CharField(max_length=300, blank=True, default="")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    order = models.ForeignKey(
        StoreOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="stock_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["stock", "-created_at"])]
        verbose_name_plural = _("fit stock entries")

    def __str__(self) -> str:
        return f"FitStockEntry<{self.stock_id} {self.kind} {self.delta:+d}>"


class FitReservation(models.Model):
    """Stock held for one order, against one FitStock row.

    Created race-safely (the FitStock rows are locked while availability is
    re-derived), released on cancellation/expiry, consumed exactly once on
    delivery — every transition is a status-guarded UPDATE, so a double
    release/consume is a no-op. An order may hold several reservations: the one
    taken at order time plus later allocations when restocks arrive for its
    backordered remainder. Idempotency lives in the service layer, which recomputes
    an order's unfilled quantity under the same row lock before reserving more.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        RELEASED = "released", _("Released")
        CONSUMED = "consumed", _("Consumed")
        EXPIRED = "expired", _("Expired")

    order = models.ForeignKey(StoreOrder, on_delete=models.CASCADE, related_name="fit_reservations")
    stock = models.ForeignKey(FitStock, on_delete=models.PROTECT, related_name="reservations")
    quantity = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gte=1), name="fitreservation_qty_gte_1"),
        ]
        indexes = [
            models.Index(fields=["stock", "status"]),
            models.Index(fields=["order", "status"]),
        ]

    def __str__(self) -> str:
        return f"FitReservation<order={self.order_id} n={self.quantity} {self.status}>"


class FitSupplyNeed(TimeStampedModel):
    """The consolidated restocking requirement for one fit at one location.

    One live row per (fit, location) — every accepted backorder and reorder-point
    breach folds into it instead of spawning duplicates — linking customer demand
    to whichever supply vehicle leadership chooses (Industry Project, ERP build
    job, or claimable task). Linked orders are derived (open orders for the same
    fit+location with a backordered quantity), never denormalised.
    """

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        IN_PROGRESS = "in_progress", _("In progress")
        DONE = "done", _("Done")
        CANCELLED = "cancelled", _("Cancelled")

    doctrine_fit = models.ForeignKey(DoctrineFit, on_delete=models.CASCADE, related_name="supply_needs")
    location = models.ForeignKey(
        MarketLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    quantity_required = models.BigIntegerField(default=0)
    required_by = models.DateTimeField(null=True, blank=True)
    industry_project = models.ForeignKey(
        "industry.IndustryProject", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    build_job = models.ForeignKey(
        "erp.BuildJob", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    task = models.ForeignKey(
        "tasks.Task", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["doctrine_fit", "location"],
                condition=Q(status__in=("open", "in_progress")),
                nulls_distinct=False,
                name="uniq_live_supplyneed_per_fit_location",
            ),
        ]

    def __str__(self) -> str:
        return f"FitSupplyNeed<fit={self.doctrine_fit_id} need={self.quantity_required} {self.status}>"


class FitWaitlistEntry(models.Model):
    """A pilot's request to be notified when an unorderable fit becomes available."""

    fit = models.ForeignKey(DoctrineFit, on_delete=models.CASCADE, related_name="waitlist_entries")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fit_waitlist_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["fit", "user"], name="uniq_fit_waitlist_entry"),
        ]
        verbose_name_plural = _("fit waitlist entries")

    def __str__(self) -> str:
        return f"FitWaitlistEntry<fit={self.fit_id} user={self.user_id}>"
