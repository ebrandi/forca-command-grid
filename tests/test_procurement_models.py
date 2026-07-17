"""P4 / WS1: procurement models, singleton config and DB constraints."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.procurement.models import (
    AgreementApproval,
    PoReceipt,
    ProcurementConfig,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplierItem,
    SupplyAgreement,
)
from apps.store.models import FitStockEntry

pytestmark = pytest.mark.django_db


def _supplier(**kw):
    kw.setdefault("kind", Supplier.Kind.PILOT)
    kw.setdefault("display_name", "Test Supplier")
    return Supplier.objects.create(**kw)


def _po(supplier, **kw):
    return PurchaseOrder.objects.create(supplier=supplier, **kw)


# --- config singleton ---------------------------------------------------------

def test_config_active_is_singleton_with_disarmed_defaults():
    a = ProcurementConfig.active()
    b = ProcurementConfig.active()
    assert a.pk == b.pk
    assert ProcurementConfig.objects.count() == 1
    assert a.agreement_approval_threshold_isk == Decimal("5000000000")
    assert a.po_director_threshold_isk == Decimal("2000000000")
    assert a.overdue_grace_days == 2
    assert a.reliability_window_weeks == 8
    # Every evidence beat ships inert.
    assert a.match_enabled is False
    assert a.reconcile_enabled is False
    assert a.overdue_sweep_enabled is False
    assert a.auto_receipt_enabled is False
    assert a.reliability_rollup_enabled is False
    assert a.reconcile_ref_types == ["contract_price_payment_corp"]


# --- Supplier: one live row per real entity -----------------------------------

def test_supplier_live_entity_unique():
    _supplier(entity_id=100, status=Supplier.Status.ACTIVE)
    with pytest.raises(IntegrityError), transaction.atomic():
        _supplier(entity_id=100, status=Supplier.Status.ACTIVE)


def test_supplier_retired_frees_the_entity():
    a = _supplier(entity_id=100, status=Supplier.Status.ACTIVE)
    a.status = Supplier.Status.RETIRED
    a.save(update_fields=["status"])
    # A fresh active supplier for the same entity is now allowed.
    _supplier(entity_id=100, status=Supplier.Status.ACTIVE)
    assert Supplier.objects.filter(entity_id=100).count() == 2


def test_supplier_null_entity_never_collides():
    _supplier(entity_id=None, kind=Supplier.Kind.HUB)
    _supplier(entity_id=None, kind=Supplier.Kind.HUB)
    assert Supplier.objects.filter(entity_id__isnull=True).count() == 2


def test_supplieritem_unique_per_type():
    s = _supplier()
    SupplierItem.objects.create(supplier=s, type_id=587)
    with pytest.raises(IntegrityError), transaction.atomic():
        SupplierItem.objects.create(supplier=s, type_id=587)


# --- AgreementApproval: one open row per agreement ----------------------------

def test_one_open_approval_per_agreement():
    s = _supplier()
    ag = SupplyAgreement.objects.create(supplier=s)
    AgreementApproval.objects.create(agreement=ag, status=AgreementApproval.Status.PENDING)
    with pytest.raises(IntegrityError), transaction.atomic():
        AgreementApproval.objects.create(agreement=ag, status=AgreementApproval.Status.PENDING)


def test_decided_approval_frees_a_new_request():
    s = _supplier()
    ag = SupplyAgreement.objects.create(supplier=s)
    AgreementApproval.objects.create(agreement=ag, status=AgreementApproval.Status.REJECTED)
    # A new pending approval is fine once the prior one is decided.
    AgreementApproval.objects.create(agreement=ag, status=AgreementApproval.Status.PENDING)
    assert ag.approvals.count() == 2


# --- PurchaseOrder: one PO per real contract ----------------------------------

def test_po_contract_id_unique_when_set():
    s = _supplier()
    _po(s, contract_id=42)
    with pytest.raises(IntegrityError), transaction.atomic():
        _po(s, contract_id=42)


def test_po_null_contract_id_never_collides():
    s = _supplier()
    _po(s, contract_id=None)
    _po(s, contract_id=None)
    assert PurchaseOrder.objects.filter(contract_id__isnull=True).count() == 2


# --- line + receipt quantity guards -------------------------------------------

def test_po_line_ordered_must_be_positive():
    s = _supplier()
    po = _po(s)
    with pytest.raises(IntegrityError), transaction.atomic():
        PurchaseOrderLine.objects.create(po=po, type_id=587, quantity_ordered=0)


def test_po_line_received_may_not_go_negative():
    s = _supplier()
    po = _po(s)
    with pytest.raises(IntegrityError), transaction.atomic():
        PurchaseOrderLine.objects.create(
            po=po, type_id=587, quantity_ordered=1, quantity_received=-1,
        )


def test_po_receipt_quantity_must_be_positive():
    s = _supplier()
    po = _po(s)
    line = PurchaseOrderLine.objects.create(po=po, type_id=587, quantity_ordered=5)
    with pytest.raises(IntegrityError), transaction.atomic():
        PoReceipt.objects.create(po=po, line=line, quantity=0)


# --- the choices-only FitStockEntry.Kind addition -----------------------------

def test_fitstockentry_gains_po_receipt_kind():
    assert FitStockEntry.Kind.PO_RECEIPT == "po_receipt"
    assert ("po_receipt", "Received from supplier") in [
        (v, str(lbl)) for v, lbl in FitStockEntry.Kind.choices
    ]
