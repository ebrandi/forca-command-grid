"""Freight service: an officer-tunable rate card and courier contracts.

The rate card holds the courier pricing constants; a single ``discount`` multiplier
(default 0.80) scales every quote, so leadership tune their margin in one place.
Courier contracts are the jobs pilots run for ISK — quoted, posted, claimed,
flown, delivered.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.market.models import MarketLocation
from core.mixins import TimeStampedModel


class ShipClass(models.TextChoices):
    # EVE ship-group proper nouns (game data) — kept English, not translated.
    DST = "dst", "Blockade Runner / DST"
    FREIGHTER = "freighter", "Freighter"
    JF = "jf", "Jump Freighter"


class Audience(models.TextChoices):
    PUBLIC = "public", _("Public — anyone can get quotes")
    ALLIANCE = "alliance", _("Corp & alliance members only")
    CORP = "corp", _("Corp members only")
    DISABLED = "disabled", _("Disabled")


class RateCard(TimeStampedModel):
    """Pricing constants for the freight calculator. One active row is used.

    Money fields are ISK. Rates are stored at full value; ``discount`` is a price
    multiplier (e.g. 0.80) applied to every quote, so leadership can set their
    margin in one place. ``audience`` controls who may use the service.
    """

    name = models.CharField(max_length=80, default="Standard")
    is_active = models.BooleanField(default=True)

    # Who the service is open to (leadership toggles public vs members-only).
    # Default is members-only: corp and registered alliance pilots, not the public.
    audience = models.CharField(max_length=10, choices=Audience.choices, default=Audience.ALLIANCE)

    # Price multiplier applied to the full rate (0.80 = 20% lower).
    discount = models.DecimalField(max_digits=4, decimal_places=3, default=Decimal("0.800"))
    min_reward = models.BigIntegerField(default=4_500_000)

    # Per-warp rates (a warp = jumps + 1).
    dst_rate_per_warp = models.BigIntegerField(default=1_500_000)
    dst_lowsec_rate_per_warp = models.BigIntegerField(default=3_750_000)
    dst_max_m3 = models.BigIntegerField(default=62_500)
    dst_max_collateral = models.BigIntegerField(default=10_000_000_000)

    freighter_rate_per_warp = models.BigIntegerField(default=2_250_000)
    freighter_rate_per_warp_long = models.BigIntegerField(default=2_500_000)
    freighter_long_threshold = models.IntegerField(default=31)  # warps at/over → long rate
    freighter_max_m3 = models.BigIntegerField(default=950_000)
    freighter_max_collateral = models.BigIntegerField(default=5_000_000_000)

    # Jump freighter: flat base + per cyno jump (range hop), priced off the
    # proximity-graph route (apps/logistics/jumps.py), not gate jumps.
    jf_base = models.BigIntegerField(default=200_000_000)
    jf_per_jump = models.BigIntegerField(default=100_000_000)
    jf_max_m3 = models.BigIntegerField(default=360_000)
    jf_max_collateral = models.BigIntegerField(default=50_000_000_000)
    # Assumed Jump Drive Calibration level for the corp's haulers, setting the
    # range used to compute cyno jumps (JDC V → 10 ly). Officer-tunable.
    jf_assumed_jdc = models.PositiveSmallIntegerField(default=5)

    # Rush surcharges (flat, pre-discount).
    rush_fee_hs = models.BigIntegerField(default=50_000_000)
    rush_fee_jf = models.BigIntegerField(default=200_000_000)

    # Days a hauler has to complete a posted contract.
    contract_days = models.IntegerField(default=7)

    class Meta:
        ordering = ["-is_active", "-updated_at"]

    def __str__(self) -> str:
        return f"RateCard<{self.name}{' active' if self.is_active else ''}>"


class CourierContract(TimeStampedModel):
    """A hauling job: a customer's items moved A→B for a reward.

    Mirrors an in-game courier contract so a pilot can pick it up, fly it, and
    get paid. ``reward`` and ``breakdown`` are computed from the rate card at
    creation time and frozen, so a later rate change never alters live jobs.
    """

    class Status(models.TextChoices):
        QUOTE = "quote", _("Quote")
        OUTSTANDING = "outstanding", _("Outstanding")
        IN_PROGRESS = "in_progress", _("In progress")
        DELIVERED = "delivered", _("Delivered")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    # Customer the haul is for (free text so external corps/alliances work).
    customer = models.CharField(max_length=120, blank=True)
    contact = models.CharField(max_length=120, blank=True)  # in-game name to contract to

    # Endpoints. ``*_name`` is the specific location label shown to the hauler
    # (station / structure / system); ``*_system_id`` is the system used for
    # routing; ``*_location_kind`` records what the label refers to and
    # ``*_location_id`` the station/structure id (null for a system-level point).
    origin_system_id = models.IntegerField(null=True, blank=True)
    origin_name = models.CharField(max_length=200)
    origin_location_kind = models.CharField(max_length=10, default="system")
    origin_location_id = models.BigIntegerField(null=True, blank=True)
    dest_system_id = models.IntegerField(null=True, blank=True)
    dest_name = models.CharField(max_length=200)
    dest_location_kind = models.CharField(max_length=10, default="system")
    dest_location_id = models.BigIntegerField(null=True, blank=True)

    jumps = models.IntegerField(default=0)
    lowsec_jumps = models.IntegerField(default=0)  # low/null systems crossed (JF pricing)
    sec_band = models.CharField(max_length=10, default="highsec")  # highsec / lowsec / nullsec

    ship_class = models.CharField(max_length=12, choices=ShipClass.choices, default=ShipClass.FREIGHTER)
    volume_m3 = models.FloatField(default=0.0)
    collateral = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    rush = models.BooleanField(default=False)

    reward = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    breakdown = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUOTE)
    notes = models.CharField(max_length=300, blank=True)
    deadline = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="courier_contracts_created",
    )
    # Who the job is posted under, so a hauler knows whose in-game contract to
    # look for: the poster's own pilot or the corporation they belong to.
    posted_as_kind = models.CharField(max_length=12, default="character")  # character | corporation
    posted_as_id = models.BigIntegerField(null=True, blank=True)
    posted_as_name = models.CharField(max_length=120, blank=True)

    assigned_hauler_character_id = models.BigIntegerField(null=True, blank=True)
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="courier_contracts_hauled",
    )

    class Verification(models.TextChoices):
        UNVERIFIED = "unverified", _("Not verified")
        VERIFIED = "verified", _("Verified in-game")
        FAILED = "failed", _("Failed in-game")

    # ESI cross-check of the actual in-game contract (see contracts_esi.py). A
    # self-reported delivery only earns full haul credit once verified.
    verification_state = models.CharField(
        max_length=12, choices=Verification.choices, default=Verification.UNVERIFIED,
        db_index=True,
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    esi_contract_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    # LOG-1 (3.2): stamped when a pre-deadline reminder DM has been sent, so the sweep sends
    # it at most once; cleared when the haul is released back to the pool.
    reminder_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.origin_name} → {self.dest_name} ({self.get_status_display()})"

    @property
    def reward_per_jump(self) -> float:
        return float(self.reward) / self.jumps if self.jumps else float(self.reward)

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.OUTSTANDING


class CorpContract(TimeStampedModel):
    """A snapshot of a corporation contract (ESI corp contracts) for oversight.

    The whole contract picture beyond our own courier service — item exchanges,
    auctions, loans and couriers issued to or by the corp — so officers can see
    what's outstanding, in progress or recently finished. Snapshot-keyed by the
    ESI contract id; rebuilt each sync.
    """

    contract_id = models.BigIntegerField(primary_key=True)
    type = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=24, blank=True, db_index=True)
    issuer_id = models.BigIntegerField(null=True, blank=True)
    issuer_corporation_id = models.BigIntegerField(null=True, blank=True)
    issuer_name = models.CharField(max_length=200, blank=True)
    assignee_id = models.BigIntegerField(null=True, blank=True)
    assignee_name = models.CharField(max_length=200, blank=True)
    title = models.CharField(max_length=255, blank=True)
    price = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    reward = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    volume = models.FloatField(default=0)
    date_issued = models.DateTimeField(null=True, blank=True, db_index=True)
    date_expired = models.DateTimeField(null=True, blank=True)
    date_completed = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-date_issued"]

    def __str__(self) -> str:
        return self.title or f"Contract {self.contract_id}"

    @property
    def is_open(self) -> bool:
        return self.status in ("outstanding", "in_progress")


# --------------------------------------------------------------------------- #
#  Freight pipeline & in-transit inventory (P6)
# --------------------------------------------------------------------------- #
class FreightConfig(TimeStampedModel):
    """Leadership-tunable freight-pipeline knobs (P6). Singleton via ``active()``.

    Mirrors the ``MrpConfig.active()`` shape: one active row, created on first read.
    Every defaulted column carries ``default`` + ``db_default`` so the additive
    migration is safe, and each knob has an implemented reader — no speculative
    switches.
    """

    is_active = models.BooleanField(default=True, db_default=True)
    default_ship_class = models.CharField(
        max_length=12, choices=ShipClass.choices, default=ShipClass.JF, db_default=ShipClass.JF,
        help_text=_("Ship class assumed when an officer assigns a batch to the courier flow."),
    )
    default_dispatch_days = models.PositiveSmallIntegerField(
        default=2, db_default=2,
        help_text=_("Assumed days from opening a batch to it departing — the default "
                    "ETD offset when none is typed."),
    )
    default_transit_days = models.PositiveSmallIntegerField(
        default=1, db_default=1,
        help_text=_("Assumed days in transit (depart → arrive) — the default ETA when "
                    "no arrival date is typed."),
    )
    eta_sweep_enabled = models.BooleanField(
        default=False, db_default=False,
        help_text=_("Run the hourly batch sweep: flip batches to arrived from a verified "
                    "courier contract or a completed haul, and flag late ones. Off: the "
                    "beat is a no-op and officers click “arrived” by hand (the v1 "
                    "workflow)."),
    )
    late_grace_hours = models.PositiveSmallIntegerField(
        default=6, db_default=6,
        help_text=_("Hours past a batch's planned ETA before the sweep flags it late."),
    )

    class Meta:
        ordering = ["-is_active", "-updated_at"]
        verbose_name = _("freight config")
        verbose_name_plural = _("freight configs")

    def __str__(self) -> str:
        return f"FreightConfig #{self.pk}{' active' if self.is_active else ''}"

    @classmethod
    def active(cls) -> FreightConfig:
        cfg = cls.objects.filter(is_active=True).order_by("-updated_at").first()
        if cfg is None:
            cfg = cls.objects.create(is_active=True)
        return cfg


class FreightBatch(TimeStampedModel):
    """One consolidation of purchase/import lines for a single lane and trip (P6).

    A batch is (origin, destination) + typed lines; at most **one OPEN batch per
    lane** (partial unique) so "add to the open batch" is deterministic. It links
    to its execution vehicle — a ``CourierContract`` (our own table, safe to FK) or
    a member ``HaulingTask`` — but is a distinct officer-managed object, never an
    overload of the member haul board. The batch renders as "origin → destination
    #pk" at read time; no prose is frozen.
    """

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        ASSIGNED = "assigned", _("Assigned")
        IN_TRANSIT = "in_transit", _("In transit")
        ARRIVED = "arrived", _("Arrived")
        CLOSED = "closed", _("Closed")
        CANCELLED = "cancelled", _("Cancelled")

    origin = models.ForeignKey(
        MarketLocation, on_delete=models.PROTECT, related_name="+",
    )
    destination = models.ForeignKey(
        MarketLocation, on_delete=models.PROTECT, related_name="+",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.OPEN, db_default=Status.OPEN,
        db_index=True,
    )
    ship_class = models.CharField(
        max_length=12, choices=ShipClass.choices, default=ShipClass.JF, db_default=ShipClass.JF,
    )
    # Execution vehicle — one or the other, both nullable. Our own CourierContract is
    # safe to FK (not snapshot-replaced like CorpContract).
    courier_contract = models.ForeignKey(
        CourierContract, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    hauling_task = models.ForeignKey(
        "stockpile.HaulingTask", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    etd_planned = models.DateTimeField(null=True, blank=True)
    eta_planned = models.DateTimeField(null=True, blank=True)
    departed_at = models.DateTimeField(null=True, blank=True)
    arrived_at = models.DateTimeField(null=True, blank=True)
    # Frozen from the quote at assignment (the CourierContract.reward-freeze discipline).
    freight_cost = models.DecimalField(max_digits=20, decimal_places=2, default=0, db_default=0)
    freight_breakdown = models.JSONField(
        blank=True, default=dict, db_default=models.Value({}, models.JSONField())
    )
    # Stamp-once (the reminder_sent_at precedent) so the late sweep flags each ETA once.
    late_flagged_at = models.DateTimeField(null=True, blank=True)
    notes = models.CharField(max_length=300, blank=True, default="", db_default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["origin", "destination"],
                condition=models.Q(status="open"),
                name="uniq_open_freightbatch_per_lane",
            ),
        ]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["destination", "status"]),
        ]

    def __str__(self) -> str:
        return self.label

    @property
    def label(self) -> str:
        """"origin → destination #pk" — derived at read time, never persisted."""
        origin = self.origin.name if self.origin_id else "?"
        dest = self.destination.name if self.destination_id else "?"
        return f"{origin} → {dest} #{self.pk}"

    @property
    def is_terminal(self) -> bool:
        return self.status in (self.Status.CLOSED, self.Status.CANCELLED)


class FreightBatchLine(models.Model):
    """One type on one batch (P6).

    A line has two legitimate writers — officer line ops and the MRP fan-out — so
    ``planned_quantity`` records the MRP-attributed share of ``quantity``: officer-
    typed units are ``quantity − planned_quantity`` and reconciliation may only ever
    move the planned share. ``unit_purchase_cost`` non-null is the freight analogue of
    "claimed" (bought goods sitting at origin), which reconciliation must never
    auto-shrink. ``purchase_ref`` is free evidence text today and the reserved P4 PO
    join point.
    """

    batch = models.ForeignKey(FreightBatch, on_delete=models.CASCADE, related_name="lines")
    type_id = models.IntegerField(db_index=True)
    quantity = models.BigIntegerField()
    planned_quantity = models.BigIntegerField(default=0, db_default=0)
    quantity_received = models.BigIntegerField(default=0, db_default=0)
    unit_purchase_cost = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True,
    )
    # Machine codes typed|snapshot; labels resolve at render (the DECISION_LABELS discipline).
    cost_source = models.CharField(max_length=8, blank=True, default="", db_default="")
    freight_share = models.DecimalField(max_digits=20, decimal_places=2, default=0, db_default=0)
    purchase_ref = models.CharField(max_length=64, blank=True, default="", db_default="")
    received_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["batch_id", "type_id"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "type_id"], name="uniq_freightline_per_type"),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1), name="freightline_qty_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity_received__gte=0)
                & models.Q(quantity_received__lte=models.F("quantity")),
                name="freightline_received_in_range",
            ),
            models.CheckConstraint(
                condition=models.Q(planned_quantity__gte=0)
                & models.Q(planned_quantity__lte=models.F("quantity")),
                name="freightline_planned_in_range",
            ),
        ]

    def __str__(self) -> str:
        return f"line<{self.type_id}×{self.quantity} on batch {self.batch_id}>"

    @property
    def remaining(self) -> int:
        """Unreceipted units — the in-transit remainder of this line."""
        return max(0, int(self.quantity) - int(self.quantity_received))

    @property
    def officer_quantity(self) -> int:
        """Units an officer typed directly (never the MRP-attributed planned share)."""
        return max(0, int(self.quantity) - int(self.planned_quantity))


class FreightReceipt(models.Model):
    """Immutable arrival evidence for a receipted line quantity (P6).

    The ``erp.Delivery`` shape: one row per receipt, never updated or deleted (the
    FitStockEntry ledger discipline). ``unit_landed_cost`` is purchase + freight
    share per unit, or null when no purchase cost was recorded — never a fabricated
    0-as-cost.
    """

    line = models.ForeignKey(FreightBatchLine, on_delete=models.CASCADE, related_name="receipts")
    stockpile = models.ForeignKey(
        "stockpile.Stockpile", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    quantity = models.BigIntegerField()
    unit_landed_cost = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True,
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["line"])]

    def __str__(self) -> str:
        return f"receipt<{self.quantity} of line {self.line_id}>"
