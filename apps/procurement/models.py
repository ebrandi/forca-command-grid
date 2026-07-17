"""Procurement (P4): suppliers, supply agreements, purchase orders.

Imports and third-party builds become tracked commitments with due dates, price
variance and reliability — evidence-driven, never a parallel stock/demand system.
The app *plans and evidences*; it never moves ISK and never creates in-game
contracts or jobs (no ESI write API exists). Everything right of "approved" is
read-only observation of contract-sync and wallet-journal evidence.

Codes stay machine-English and persisted; every user-visible label is a
``gettext`` translation resolved at render time (the ``SUGGESTION_LABELS``
discipline). Officer-typed prose (``display_name``/``contact``/``notes``/
``reason``) is verbatim and never machine-translated.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import JSONField, Q, Value
from django.utils.translation import gettext_lazy as _

from core.mixins import TimeStampedModel

# ISK money columns follow the store/pricing norm (max_digits=20, 2 dp) — up to
# ~1e18 ISK, comfortably past a super-cap fleet. Rates are fractions (0..1);
# variance is a signed fraction.
_ISK = dict(max_digits=20, decimal_places=2)
_RATE = dict(max_digits=10, decimal_places=4)


def _default_reconcile_ref_types() -> list[str]:
    """The wallet ref_type the payment reconcile trusts out of the box.

    A module-level callable (not ``list``) so a config created through the ORM
    ``active()`` path is seeded with the real ref_type, not an empty list.
    """
    return ["contract_price_payment_corp"]


class ProcurementConfig(TimeStampedModel):
    """Leadership-tunable procurement knobs. Singleton via ``active()`` — the
    ``MrpConfig``/``DemandConfig`` shape; tunables live here, never scattered
    ``AppSetting`` keys. Every evidence beat is inert until its own flag arms."""

    is_active = models.BooleanField(default=True, db_default=True)
    agreement_approval_threshold_isk = models.DecimalField(
        default=Decimal("5000000000"), db_default=Decimal("5000000000"), **_ISK,
        help_text=_("Estimated cycle value at or above which a supply agreement needs "
                    "a second Director's approval."),
    )
    po_director_threshold_isk = models.DecimalField(
        default=Decimal("2000000000"), db_default=Decimal("2000000000"), **_ISK,
        help_text=_("Total value at or above which a standalone purchase order (one "
                    "not covered by an active agreement) needs Director approval."),
    )
    overdue_grace_days = models.PositiveSmallIntegerField(
        default=2, db_default=2,
        help_text=_("Days past the promised date before a purchase order is flagged overdue."),
    )
    reliability_window_weeks = models.PositiveSmallIntegerField(
        default=8, db_default=8,
        help_text=_("Complete weeks of closed purchase orders the reliability rollup looks back over."),
    )
    match_enabled = models.BooleanField(
        default=False, db_default=False,
        help_text=_("Arm the contract matcher (runs after the corp-contracts sync). "
                    "Off: officers confirm contract matches by hand."),
    )
    reconcile_enabled = models.BooleanField(
        default=False, db_default=False,
        help_text=_("Arm the wallet-journal payment reconcile. Off: officers confirm payments by hand."),
    )
    overdue_sweep_enabled = models.BooleanField(
        default=False, db_default=False,
        help_text=_("Arm the daily overdue sweep (also expires lapsed agreements)."),
    )
    auto_receipt_enabled = models.BooleanField(
        default=False, db_default=False,
        help_text=_("Let a finished matched contract post its landed quantities as receipts "
                    "automatically. Off: officers post receipts by hand."),
    )
    reliability_rollup_enabled = models.BooleanField(
        default=False, db_default=False,
        help_text=_("Arm the nightly supplier-reliability rollup."),
    )
    reconcile_ref_types = models.JSONField(
        default=_default_reconcile_ref_types,
        db_default=Value(["contract_price_payment_corp"], JSONField()),
        help_text=_("Wallet-journal ref_types the payment reconcile treats as supplier "
                    "payments. Enumerate from the real corp journal before arming."),
    )

    class Meta:
        ordering = ["-is_active", "-updated_at"]
        verbose_name = _("procurement config")
        verbose_name_plural = _("procurement configs")

    def __str__(self) -> str:
        return f"ProcurementConfig #{self.pk}{' active' if self.is_active else ''}"

    @classmethod
    def active(cls) -> ProcurementConfig:
        cfg = cls.objects.filter(is_active=True).order_by("-updated_at").first()
        if cfg is None:
            cfg = cls.objects.create(is_active=True)
        return cfg


class Supplier(TimeStampedModel):
    """A source of imports or third-party builds — a pilot, a corp, or a hub
    seller. Business data only: the ``EveName`` and what officers type; never a
    pilot's skills, assets or personal jobs (the consent model is untouched)."""

    class Kind(models.TextChoices):
        PILOT = "pilot", _("Pilot")
        CORP = "corp", _("Corporation")
        HUB = "hub", _("Market hub")

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        PROBATION = "probation", _("Probation")
        SUSPENDED = "suspended", _("Suspended")
        RETIRED = "retired", _("Retired")

    kind = models.CharField(max_length=5, choices=Kind.choices)
    # Names render via EveName; entity_id may be null for an informal/hub source.
    entity_id = models.BigIntegerField(null=True, blank=True)
    display_name = models.CharField(max_length=200, blank=True, default="", db_default="")
    contact = models.CharField(max_length=200, blank=True, default="", db_default="")
    # Machine codes (manufacturing/reactions/hauling/trading); labels render-time.
    activities = models.JSONField(
        blank=True, default=list, db_default=Value([], JSONField())
    )
    locations = models.ManyToManyField("market.MarketLocation", blank=True, related_name="+")
    default_location = models.ForeignKey(
        "market.MarketLocation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    lead_time_days = models.PositiveSmallIntegerField(default=5, db_default=5)
    weekly_capacity_units = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.ACTIVE,
        db_default=Status.ACTIVE, db_index=True,
    )
    notes = models.TextField(blank=True, default="", db_default="")
    # Reliability — written by the nightly rollup (metrics.py), never live-computed.
    on_time_rate = models.DecimalField(null=True, blank=True, **_RATE)
    fill_rate = models.DecimalField(null=True, blank=True, **_RATE)
    price_variance_pct = models.DecimalField(null=True, blank=True, **_RATE)
    reliability_sample = models.PositiveIntegerField(default=0, db_default=0)
    reliability_computed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["display_name", "pk"]
        constraints = [
            # One live supplier per real entity: re-adding a retired supplier is fine,
            # but two active rows for the same character/corp would split its history.
            models.UniqueConstraint(
                fields=["entity_id"],
                condition=Q(entity_id__isnull=False) & ~Q(status="retired"),
                name="uniq_live_supplier_entity",
            ),
        ]
        indexes = [models.Index(fields=["status", "kind"])]

    def __str__(self) -> str:
        return self.display_name or f"Supplier #{self.pk}"


class SupplierItem(TimeStampedModel):
    """The per-type catalogue for a supplier: what they sell/build, at what MOQ,
    price model and lead time."""

    class PriceModel(models.TextChoices):
        FIXED = "fixed", _("Fixed price")
        JITA_INDEXED = "jita_indexed", _("Jita-indexed + premium")

    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="items")
    type_id = models.IntegerField(db_index=True)
    moq = models.PositiveIntegerField(default=1, db_default=1)
    price_model = models.CharField(
        max_length=12, choices=PriceModel.choices, default=PriceModel.JITA_INDEXED,
        db_default=PriceModel.JITA_INDEXED,
    )
    fixed_price_isk = models.DecimalField(null=True, blank=True, **_ISK)
    premium_pct = models.DecimalField(default=Decimal("0"), db_default=Decimal("0"), **_RATE)
    lead_time_days = models.PositiveSmallIntegerField(null=True, blank=True)
    weekly_capacity_units = models.BigIntegerField(null=True, blank=True)
    active = models.BooleanField(default=True, db_default=True)

    class Meta:
        ordering = ["supplier", "type_id"]
        constraints = [
            models.UniqueConstraint(fields=["supplier", "type_id"], name="uniq_supplieritem_type"),
        ]

    def __str__(self) -> str:
        return f"SupplierItem<{self.supplier_id}:{self.type_id}>"


class SupplyAgreement(TimeStampedModel):
    """A standing commitment with a supplier — term, lines, cadence, payment
    terms. Above the Director threshold it needs a second Director's approval
    before it goes ACTIVE and can pre-authorise purchase orders."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PENDING_APPROVAL = "pending_approval", _("Pending approval")
        ACTIVE = "active", _("Active")
        REJECTED = "rejected", _("Rejected")
        EXPIRED = "expired", _("Expired")
        CANCELLED = "cancelled", _("Cancelled")

    class Cadence(models.TextChoices):
        ONE_OFF = "one_off", _("One-off")
        WEEKLY = "weekly", _("Weekly")
        MONTHLY = "monthly", _("Monthly")

    class PaymentTerms(models.TextChoices):
        PREPAID = "prepaid", _("Prepaid")
        ON_DELIVERY = "on_delivery", _("On delivery")
        NET_7 = "net_7", _("Net 7 days")
        NET_30 = "net_30", _("Net 30 days")

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="agreements")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFT,
        db_default=Status.DRAFT, db_index=True,
    )
    term_start = models.DateField(null=True, blank=True)
    term_end = models.DateField(null=True, blank=True)
    cadence = models.CharField(
        max_length=8, choices=Cadence.choices, default=Cadence.ONE_OFF, db_default=Cadence.ONE_OFF,
    )
    location = models.ForeignKey(
        "market.MarketLocation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    payment_terms = models.CharField(
        max_length=12, choices=PaymentTerms.choices, default=PaymentTerms.ON_DELIVERY,
        db_default=PaymentTerms.ON_DELIVERY,
    )
    collateral_isk = models.DecimalField(default=Decimal("0"), db_default=Decimal("0"), **_ISK)
    # Frozen at submit from one price snapshot — the threshold basis. A renewal is
    # a NEW agreement and re-freezes.
    estimated_cycle_value_isk = models.DecimalField(
        default=Decimal("0"), db_default=Decimal("0"), **_ISK,
    )
    notes = models.TextField(blank=True, default="", db_default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "supplier"])]

    def __str__(self) -> str:
        return f"SupplyAgreement #{self.pk} ({self.status})"


class SupplyAgreementLine(models.Model):
    """One concrete type on an agreement. A "family" (e.g. all T1 BC hulls) is
    enumerated as one line per ``type_id`` (§11 narrowing)."""

    agreement = models.ForeignKey(SupplyAgreement, on_delete=models.CASCADE, related_name="lines")
    type_id = models.IntegerField(db_index=True)
    quantity_per_cycle = models.BigIntegerField(default=0, db_default=0)
    min_qty = models.BigIntegerField(null=True, blank=True)
    max_qty = models.BigIntegerField(null=True, blank=True)
    # Agreement lines override the catalogue price trio.
    price_model = models.CharField(
        max_length=12, choices=SupplierItem.PriceModel.choices,
        default=SupplierItem.PriceModel.JITA_INDEXED, db_default=SupplierItem.PriceModel.JITA_INDEXED,
    )
    fixed_price_isk = models.DecimalField(null=True, blank=True, **_ISK)
    premium_pct = models.DecimalField(default=Decimal("0"), db_default=Decimal("0"), **_RATE)

    class Meta:
        ordering = ["agreement", "type_id"]
        constraints = [
            models.UniqueConstraint(fields=["agreement", "type_id"], name="uniq_agreementline_type"),
        ]

    def __str__(self) -> str:
        return f"AgreementLine<{self.agreement_id}:{self.type_id}>"


class AgreementApproval(TimeStampedModel):
    """Dual-control for above-threshold agreements — the ``RoleChangeRequest``
    shape cloned (not generalised in place): a requester submits, a *different*
    Director approves. Superuser is NOT exempt (the stricter buyback posture)."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")

    agreement = models.ForeignKey(SupplyAgreement, on_delete=models.CASCADE, related_name="approvals")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agreement_approvals_made",
    )
    # Copied at submit — what the decider saw, immutable.
    estimated_value_isk = models.DecimalField(default=Decimal("0"), db_default=Decimal("0"), **_ISK)
    reason = models.CharField(max_length=200, blank=True, default="", db_default="")
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING,
        db_default=Status.PENDING, db_index=True,
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agreement_approvals_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # At most one open approval per agreement (the uniq_open_role_request pattern).
            models.UniqueConstraint(
                fields=["agreement"], condition=Q(status="pending"),
                name="uniq_open_agreement_approval",
            ),
        ]

    def __str__(self) -> str:
        return f"AgreementApproval<agreement={self.agreement_id} {self.status}>"


class PurchaseOrder(TimeStampedModel):
    """One order to a supplier. Lifecycle is evidence-driven (contract sync,
    wallet journal, receipts) or an audited officer action; everything right of
    APPROVED is read-only observation. Contract and payment evidence is *copied*
    onto the row (never an FK — the ``CorpContract`` snapshot is delete-all
    rebuilt hourly)."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SUBMITTED = "submitted", _("Submitted")
        APPROVED = "approved", _("Approved")
        CONTRACT_EXPECTED = "contract_expected", _("Contract expected")
        CONTRACT_AVAILABLE = "contract_available", _("Contract available")
        ACCEPTED = "accepted", _("Accepted")
        PARTIAL = "partial", _("Partially delivered")
        DELIVERED = "delivered", _("Delivered")
        RECONCILED = "reconciled", _("Reconciled")
        CANCELLED = "cancelled", _("Cancelled")
        DISPUTED = "disputed", _("Disputed")
        OVERDUE = "overdue", _("Overdue")

    class DeliveryMode(models.TextChoices):
        SUPPLIER_DELIVERS = "supplier_delivers", _("Supplier delivers")
        HUB_PICKUP = "hub_pickup", _("Hub pickup (corp hauls)")

    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_orders")
    agreement = models.ForeignKey(
        SupplyAgreement, on_delete=models.SET_NULL, null=True, blank=True, related_name="purchase_orders",
    )
    location = models.ForeignKey(
        "market.MarketLocation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    delivery_mode = models.CharField(
        max_length=18, choices=DeliveryMode.choices, default=DeliveryMode.SUPPLIER_DELIVERS,
        db_default=DeliveryMode.SUPPLIER_DELIVERS,
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT,
        db_default=Status.DRAFT, db_index=True,
    )
    promised_by = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    # Kept when the PO later progresses — reliability evidence. Never cleared.
    overdue_since = models.DateTimeField(null=True, blank=True)
    expected_total_isk = models.DecimalField(default=Decimal("0"), db_default=Decimal("0"), **_ISK)
    notes = models.TextField(blank=True, default="", db_default="")
    # Seam-B: a system-generated note stores its scaffold key + JSON params plus a
    # pinned-English fallback (the BuildJob trio), so it re-renders in the reader's locale.
    note_key = models.CharField(max_length=60, blank=True, default="", db_default="")
    note_params = models.JSONField(blank=True, default=dict, db_default=Value({}, JSONField()))
    system_note = models.CharField(max_length=300, blank=True, default="", db_default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    # Contract evidence, copied at match time (bare id soft-link, never an FK).
    contract_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    contract_status = models.CharField(max_length=24, blank=True, default="", db_default="")
    contract_price = models.DecimalField(null=True, blank=True, **_ISK)
    contract_date_issued = models.DateTimeField(null=True, blank=True)
    contract_date_completed = models.DateTimeField(null=True, blank=True)
    contract_matched_at = models.DateTimeField(null=True, blank=True)
    contract_items = models.JSONField(
        blank=True, default=list, db_default=Value([], JSONField())
    )
    # Payment evidence (the buyback settlement_ref analogue).
    paid_entry_id = models.BigIntegerField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_amount_isk = models.DecimalField(null=True, blank=True, **_ISK)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One PO per real contract — two links would double-count one delivery.
            models.UniqueConstraint(
                fields=["contract_id"], condition=Q(contract_id__isnull=False),
                name="uniq_po_contract_id",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "promised_by"]),
            models.Index(fields=["supplier", "status"]),
        ]

    def __str__(self) -> str:
        return f"PurchaseOrder #{self.pk} ({self.status})"


class PurchaseOrderLine(models.Model):
    """One type on a purchase order. ``doctrine_fit`` set ⇒ receipts post to fit
    inventory via ``store.inventory.receive_stock``; null ⇒ they post to the corp
    stockpile via the ``erp.deliver`` path."""

    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    type_id = models.IntegerField(db_index=True)
    doctrine_fit = models.ForeignKey(
        "doctrines.DoctrineFit", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    quantity_ordered = models.BigIntegerField(default=0, db_default=0)
    quantity_received = models.BigIntegerField(default=0, db_default=0)
    # Frozen at approval: fixed price, or price_for snapshot + premium.
    unit_price_isk = models.DecimalField(default=Decimal("0"), db_default=Decimal("0"), **_ISK)
    # Variance baseline, frozen at order time.
    unit_jita_at_order = models.DecimalField(default=Decimal("0"), db_default=Decimal("0"), **_ISK)

    class Meta:
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity_ordered__gte=1), name="poline_ordered_gte_1"),
            models.CheckConstraint(condition=Q(quantity_received__gte=0), name="poline_received_gte_0"),
        ]

    def __str__(self) -> str:
        return f"POLine<po={self.po_id}:{self.type_id} {self.quantity_received}/{self.quantity_ordered}>"


class PoReceipt(models.Model):
    """One immutable evidence row per receipt event (the ``Delivery`` analogue).
    Never updated or deleted; corrections are stockpile adjustments + a Disputed
    note, not receipt edits."""

    class Kind(models.TextChoices):
        CONTRACT_AUTO = "contract_auto", _("Auto (contract landed)")
        MANUAL = "manual", _("Manual (officer attested)")

    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="receipts")
    line = models.ForeignKey(PurchaseOrderLine, on_delete=models.CASCADE, related_name="receipts")
    quantity = models.BigIntegerField(default=0, db_default=0)
    kind = models.CharField(max_length=14, choices=Kind.choices, default=Kind.MANUAL, db_default=Kind.MANUAL)
    contract_id = models.BigIntegerField(null=True, blank=True)
    # Where a type-level receipt posted (null on fit-level receipts).
    stockpile = models.ForeignKey(
        "stockpile.StockpileItem", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    # Frozen variance evidence — the reliability rollup reads this, never a live price.
    unit_jita_at_receipt = models.DecimalField(default=Decimal("0"), db_default=Decimal("0"), **_ISK)
    note = models.CharField(max_length=300, blank=True, default="", db_default="")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity__gte=1), name="poreceipt_qty_gte_1"),
        ]
        indexes = [models.Index(fields=["po", "-created_at"])]

    def __str__(self) -> str:
        return f"PoReceipt<po={self.po_id} line={self.line_id} n={self.quantity} {self.kind}>"
