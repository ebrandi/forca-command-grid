"""P4 / WS6: MRP + Shipyard integration — creators, po_line incoming, reconcile,
attribution, availability."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.doctrines.models import Doctrine, DoctrineFit
from apps.industry import mrp
from apps.industry.models import MrpConfig, NetRequirement
from apps.procurement import receipts, services
from apps.procurement.models import PurchaseOrder, PurchaseOrderLine, Supplier, SupplierItem
from apps.store.availability import availability_for_fits
from apps.store.models import FitOffer, FitSupplyNeed

pytestmark = pytest.mark.django_db

RIFTER, COMPONENT = 587, 700
S = PurchaseOrder.Status


def _supplier(**kw):
    kw.setdefault("kind", Supplier.Kind.PILOT)
    kw.setdefault("display_name", "S")
    return Supplier.objects.create(**kw)


def _item(supplier, type_id, *, moq=1, price="1000000"):
    return SupplierItem.objects.create(
        supplier=supplier, type_id=type_id, active=True,
        price_model=SupplierItem.PriceModel.FIXED, fixed_price_isk=Decimal(price), moq=moq,
    )


def _counted_type_po(type_id, ordered, *, status=S.ACCEPTED):
    s = _supplier()
    po = PurchaseOrder.objects.create(supplier=s, status=status,
                                      promised_by=timezone.now() + timezone.timedelta(days=5))
    PurchaseOrderLine.objects.create(po=po, type_id=type_id, quantity_ordered=ordered)
    return po


# --- creators -----------------------------------------------------------------

def test_mrp_po_creator_is_idempotent_and_links(priced_sde):
    req = NetRequirement.objects.create(type_id=RIFTER, net_quantity=10, gross_quantity=10)
    s = _supplier()
    _item(s, RIFTER)
    po = mrp.create_purchase_order_for_requirement(req, actor=None, supplier=s)
    req.refresh_from_db()
    assert req.purchase_order_id == po.pk
    assert req.status == NetRequirement.Status.IN_PROGRESS
    assert po.lines.get().quantity_ordered == 10
    assert po.note_key == "po.mrp_source"
    # Idempotent.
    assert mrp.create_purchase_order_for_requirement(req, actor=None, supplier=s).pk == po.pk


def test_shipyard_po_creator_is_idempotent_and_fit_level():
    doc = Doctrine.objects.create(name="D")
    fit = DoctrineFit.objects.create(doctrine=doc, name="Rifter", ship_type_id=RIFTER, modules=[])
    need = FitSupplyNeed.objects.create(doctrine_fit=fit, quantity_required=5,
                                        status=FitSupplyNeed.Status.IN_PROGRESS)
    s = _supplier()
    from apps.store.supply import create_purchase_order_for_need

    po = create_purchase_order_for_need(need, actor=None, supplier=s)
    need.refresh_from_db()
    assert need.purchase_order_id == po.pk
    line = po.lines.get()
    assert line.doctrine_fit_id == fit.id and line.quantity_ordered == 5
    assert create_purchase_order_for_need(need, actor=None, supplier=s).pk == po.pk


# --- _collect_incoming po_line lots + the cutover -----------------------------

def test_po_line_lot_present_and_shrinks_on_receipt():
    po = _counted_type_po(RIFTER, 10)
    line = po.lines.get()
    pool, _c, _m = mrp._collect_incoming(MrpConfig.active())
    lots = [x for x in pool if x.kind == "po_line"]
    assert len(lots) == 1
    assert lots[0].type_id == RIFTER and lots[0].remaining == 10 and lots[0].po_id == po.pk
    # A receipt raises quantity_received → the lot shrinks by exactly that (the cutover).
    receipts.receive_po_delivery(po, line, 4, actor=None)
    pool2, _c2, _m2 = mrp._collect_incoming(MrpConfig.active())
    assert [x for x in pool2 if x.kind == "po_line"][0].remaining == 6


def test_draft_and_disputed_pos_never_count_as_incoming():
    for status in (S.DRAFT, S.DISPUTED):
        po = _counted_type_po(RIFTER, 10, status=status)
        pool, _c, _m = mrp._collect_incoming(MrpConfig.active())
        assert not [x for x in pool if x.kind == "po_line" and x.po_id == po.pk]
        po.delete()


# --- reconcile_mrp_po (DRAFT refresh, MOQ, diverge, release) -------------------

def test_reconcile_draft_refreshes_to_moq_rounded_target_and_is_stable():
    req = NetRequirement.objects.create(type_id=RIFTER, net_quantity=7, gross_quantity=7)
    s = _supplier()
    _item(s, RIFTER, moq=10)
    po = mrp.create_purchase_order_for_requirement(req, actor=None, supplier=s)
    # Creator already MOQ-rounded 7 → 10.
    assert po.lines.get().quantity_ordered == 10
    # Reconcile with a drifted target 12 → rounds up to 20.
    assert services.reconcile_mrp_po(po.pk, 12) == "refreshed"
    assert po.lines.get().quantity_ordered == 20
    # Re-run with the same target rounds to the SAME 20 → no write (the §12 trap).
    assert services.reconcile_mrp_po(po.pk, 12) == "ok"
    assert po.lines.get().quantity_ordered == 20


def test_reconcile_committed_po_diverges_never_rewrites():
    po = _counted_type_po(RIFTER, 10, status=S.APPROVED)
    # A committed PO whose line (10) differs from the target (25) is flagged, not rewritten.
    assert services.reconcile_mrp_po(po.pk, 25) == "diverged"
    assert po.lines.get().quantity_ordered == 10
    # An in-agreement target that matches is fine.
    assert services.reconcile_mrp_po(po.pk, 10) == "ok"


def test_reconcile_terminal_po_is_released():
    po = _counted_type_po(RIFTER, 10, status=S.CANCELLED)
    assert services.reconcile_mrp_po(po.pk, 10) == "released"


def test_net_requirement_has_vehicle_counts_the_po():
    po = _counted_type_po(RIFTER, 10, status=S.APPROVED)
    req = NetRequirement.objects.create(type_id=RIFTER, purchase_order=po)
    assert req.has_vehicle is True


# --- full run: the PO vehicle keeps a stale row IN_PROGRESS --------------------

def test_po_vehicle_keeps_orphan_row_in_progress(priced_sde):
    FitOffer.objects.create(fit=DoctrineFit.objects.create(
        doctrine=Doctrine.objects.create(name="D"), name="A", ship_type_id=RIFTER,
    ), target_stock=6)
    mrp.run_mrp()
    # A synthetic component row with a counted PO vehicle, no demand this run.
    comp = NetRequirement.objects.create(type_id=COMPONENT, net_quantity=20, gross_quantity=20,
                                         suggestion="buy", depth=1)
    po = _counted_type_po(COMPONENT, 20, status=S.APPROVED)
    comp.purchase_order = po
    comp.save(update_fields=["purchase_order"])
    mrp.run_mrp()
    comp.refresh_from_db()
    # Swept stale but the PO vehicle holds it IN_PROGRESS; the PO line is untouched.
    assert comp.status == NetRequirement.Status.IN_PROGRESS
    assert po.lines.get().quantity_ordered == 20


# --- availability: an open PO on a need is incoming (WS2 #6) -------------------

def test_open_po_on_need_counts_as_incoming_with_purchase_eta():
    doc = Doctrine.objects.create(name="D")
    fit = DoctrineFit.objects.create(doctrine=doc, name="Rifter", ship_type_id=RIFTER, modules=[])
    FitOffer.objects.create(fit=fit, target_stock=10)
    po = PurchaseOrder.objects.create(
        supplier=_supplier(), status=S.ACCEPTED,
        promised_by=timezone.now() + timezone.timedelta(days=5),
    )
    PurchaseOrderLine.objects.create(po=po, type_id=RIFTER, doctrine_fit=fit,
                                     quantity_ordered=8, quantity_received=2)
    FitSupplyNeed.objects.create(doctrine_fit=fit, quantity_required=6,
                                 status=FitSupplyNeed.Status.IN_PROGRESS, purchase_order=po)
    avail = availability_for_fits([fit])[fit.id]
    assert avail.incoming == 6   # line remaining 8 - 2
    assert avail.eta_source == "purchase"
