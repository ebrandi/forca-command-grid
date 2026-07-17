"""P4 / WS5: receipt posting through the P1/fit ledgered paths + auto-receipt."""
from __future__ import annotations

import pytest

from apps.doctrines.models import Doctrine, DoctrineFit
from apps.market.models import MarketLocation
from apps.procurement import receipts
from apps.procurement.models import (
    PoReceipt,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
)
from apps.procurement.services import ProcurementError
from apps.stockpile.models import StockpileItem
from apps.store.models import FitStock, FitStockEntry

pytestmark = pytest.mark.django_db

S = PurchaseOrder.Status


def _supplier():
    return Supplier.objects.create(kind=Supplier.Kind.PILOT, display_name="S")


def _type_po(qty=10, status=S.ACCEPTED, **po_kw):
    s = _supplier()
    po = PurchaseOrder.objects.create(supplier=s, status=status, **po_kw)
    line = PurchaseOrderLine.objects.create(po=po, type_id=587, quantity_ordered=qty)
    return po, line


def test_type_level_receipt_posts_to_stockpile_and_tracks_received():
    po, line = _type_po(qty=10)
    receipts.receive_po_delivery(po, line, 4, actor=None)
    line.refresh_from_db()
    po.refresh_from_db()
    assert line.quantity_received == 4
    assert po.status == S.PARTIAL
    item = StockpileItem.objects.get(type_id=587)
    assert item.quantity_current == 4
    r = PoReceipt.objects.get()
    assert r.quantity == 4 and r.stockpile_id == item.pk and r.kind == PoReceipt.Kind.MANUAL


def test_receipt_over_orders_is_clamped_and_completes():
    po, line = _type_po(qty=10)
    receipts.receive_po_delivery(po, line, 4, actor=None)
    # Ask for 10 more; only 6 remain — clamp, then the PO is DELIVERED.
    receipts.receive_po_delivery(po, line, 10, actor=None)
    line.refresh_from_db()
    po.refresh_from_db()
    assert line.quantity_received == 10
    assert po.status == S.DELIVERED
    assert StockpileItem.objects.get(type_id=587).quantity_current == 10


def test_receipt_blocked_outside_receivable_states():
    po, line = _type_po(qty=5, status=S.APPROVED)
    with pytest.raises(ProcurementError):
        receipts.receive_po_delivery(po, line, 1, actor=None)


def test_fully_received_line_rejects_further_receipts():
    po, line = _type_po(qty=2)
    receipts.receive_po_delivery(po, line, 2, actor=None)
    with pytest.raises(ProcurementError):
        receipts.receive_po_delivery(po, line, 1, actor=None)


def test_fit_level_receipt_uses_the_fit_ledger():
    s = _supplier()
    loc = MarketLocation.objects.create(name="Home", location_type=MarketLocation.LocationType.STATION)
    doc = Doctrine.objects.create(name="D")
    fit = DoctrineFit.objects.create(doctrine=doc, name="Rifter", ship_type_id=587, modules=[])
    po = PurchaseOrder.objects.create(supplier=s, location=loc, status=S.ACCEPTED)
    line = PurchaseOrderLine.objects.create(po=po, type_id=587, doctrine_fit=fit, quantity_ordered=3)

    receipts.receive_po_delivery(po, line, 3, actor=None)
    line.refresh_from_db()
    po.refresh_from_db()
    # Fit path raises quantity_received (so the MRP lot shrinks) AND writes the fit ledger.
    assert line.quantity_received == 3
    assert po.status == S.DELIVERED
    stock = FitStock.objects.get(doctrine_fit=fit, location=loc)
    assert stock.quantity_on_hand == 3
    entry = FitStockEntry.objects.get(stock=stock)
    assert entry.kind == FitStockEntry.Kind.PO_RECEIPT and entry.delta == 3
    # No corp StockpileItem was written for a fit-level receipt.
    assert not StockpileItem.objects.filter(type_id=587).exists()
    # The receipt evidence row has no stockpile (fit-level).
    assert PoReceipt.objects.get().stockpile_id is None


def test_auto_receipt_posts_landed_quantities_and_is_idempotent():
    po, line = _type_po(qty=10, contract_id=42,
                        contract_items=[{"type_id": 587, "quantity": 7}])
    posted = receipts.auto_receive_from_contract(po, actor=None)
    assert len(posted) == 1 and posted[0].kind == PoReceipt.Kind.CONTRACT_AUTO
    line.refresh_from_db()
    po.refresh_from_db()
    assert line.quantity_received == 7
    assert po.status == S.PARTIAL
    # Re-run: cumulative landed already accounted → nothing more posts.
    again = receipts.auto_receive_from_contract(po, actor=None)
    assert again == []
    line.refresh_from_db()
    assert line.quantity_received == 7


def test_auto_receipt_skips_fit_level_lines():
    s = _supplier()
    doc = Doctrine.objects.create(name="D")
    fit = DoctrineFit.objects.create(doctrine=doc, name="Rifter", ship_type_id=587, modules=[])
    po = PurchaseOrder.objects.create(supplier=s, status=S.ACCEPTED, contract_id=99,
                                      contract_items=[{"type_id": 587, "quantity": 5}])
    line = PurchaseOrderLine.objects.create(po=po, type_id=587, doctrine_fit=fit, quantity_ordered=5)
    posted = receipts.auto_receive_from_contract(po, actor=None)
    assert posted == []
    line.refresh_from_db()
    assert line.quantity_received == 0  # assembly is a human act
