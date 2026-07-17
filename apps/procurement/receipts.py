"""P4 receipts: post supplier deliveries through the P1 / Phase-0 ledgered paths.

A receipt is a physical fact an officer may always attest (or a matched contract's
landed quantities, when auto-receipt is armed). It writes an immutable ``PoReceipt``
evidence row and raises ``PurchaseOrderLine.quantity_received`` — the single
increment that shrinks the MRP ``po_line`` incoming lot by exactly what the stock
ledger just gained, so supply is never counted twice. It never adds a second stock
counter: ``available()`` stays the one type-level authority and
``store.inventory.receive_stock`` the one fit ledger.

Lock order (§3.3.2): the PO row first, then its line, then the stock rows — never
the reverse.
"""
from __future__ import annotations

from decimal import Decimal

from django.db import models, transaction
from django.utils.translation import gettext as _

from apps.market.pricing import price_for
from core.audit import audit_log

from .models import PoReceipt, PurchaseOrder, PurchaseOrderLine
from .services import RECEIVABLE_STATUSES, ProcurementError, _q, apply_derived_state

S = PurchaseOrder.Status


def _corp_stockpile_for(location_id):
    """The corp stockpile at a location, falling back to any corp stockpile
    (created on demand) — the ``_corp_stockpile_at`` + ``_default_corp_stockpile``
    shape combined."""
    from apps.stockpile.models import Stockpile

    sp = None
    if location_id is not None:
        sp = (
            Stockpile.objects.filter(kind=Stockpile.Kind.CORP, location_id=location_id)
            .order_by("pk").first()
        )
    if sp is None:
        sp = Stockpile.objects.filter(kind=Stockpile.Kind.CORP).order_by("pk").first()
    if sp is None:
        sp = Stockpile.objects.create(name="Production", kind=Stockpile.Kind.CORP)
    return sp


@transaction.atomic
def receive_po_delivery(po: PurchaseOrder, line: PurchaseOrderLine, quantity: int, *,
                        actor, kind: str = PoReceipt.Kind.MANUAL, contract_id=None) -> PoReceipt:
    """Record a delivery against one PO line. Clamps to the outstanding quantity,
    writes the evidence row, increments ``quantity_received``, posts to the corp
    stockpile (type-level line) or the fit ledger (fit-level line), then advances
    the PO to its evidence-derived state."""
    locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if locked.status not in RECEIVABLE_STATUSES:
        raise ProcurementError(_("This purchase order is not ready to receive deliveries."))
    line_locked = PurchaseOrderLine.objects.select_for_update().get(pk=line.pk, po=locked)

    remaining = line_locked.quantity_ordered - line_locked.quantity_received
    if remaining <= 0:
        raise ProcurementError(_("This line is already fully received."))
    qty = min(int(quantity), remaining)
    if qty <= 0:
        raise ProcurementError(_("Receipt quantity must be positive."))

    stockpile = None
    if line_locked.doctrine_fit_id is None:
        # Type-level: post to the corp stockpile. P1's ESI-wins rule means a manual
        # count is ignored by available() at an ESI-covered location (it appears at
        # the next asset sync) and is the immediate truth at an uncovered one — either
        # way available()/available_detail stays the only reader.
        from apps.stockpile.models import StockpileItem

        sp = _corp_stockpile_for(locked.location_id)
        item, _created = StockpileItem.objects.get_or_create(
            stockpile=sp, type_id=line_locked.type_id,
        )
        StockpileItem.objects.filter(pk=item.pk).update(
            quantity_current=models.F("quantity_current") + qty
        )
        stockpile = item
    else:
        # Fit-level: go through the one fit ledger so the entry, backorder allocation
        # and buyer notifications all fire for free. Assembly is a human act, so a
        # fit line is always a manual receipt (auto-receipt never touches it).
        from apps.store.inventory import receive_stock
        from apps.store.models import FitStockEntry

        receive_stock(
            line_locked.doctrine_fit, location=locked.location, quantity=qty, actor=actor,
            kind=FitStockEntry.Kind.PO_RECEIPT,
        )

    jita = price_for(line_locked.type_id)
    receipt = PoReceipt.objects.create(
        po=locked, line=line_locked, quantity=qty, kind=kind, contract_id=contract_id,
        stockpile=stockpile, unit_jita_at_receipt=_q(jita or Decimal(0)), actor=actor,
    )
    # The one increment that shrinks the MRP po_line lot — on EVERY receipt, fit-level
    # included; skipping it on the fit path double-counts at ship level.
    PurchaseOrderLine.objects.filter(pk=line_locked.pk).update(
        quantity_received=models.F("quantity_received") + qty
    )
    locked.refresh_from_db()
    audit_log(actor, "procurement.po_receipt", target_type="purchase_order",
              target_id=str(locked.pk),
              metadata={"line": line_locked.pk, "quantity": qty, "kind": kind})

    previous = locked.status
    apply_derived_state(locked, actor=None)
    locked.refresh_from_db()
    if locked.status == S.DELIVERED and previous != S.DELIVERED:
        _notify_linked_needs(locked)
    return receipt


@transaction.atomic
def create_haul_for_po(po: PurchaseOrder, actor) -> list:
    """The deferred "haul button": for a hub-pickup PO in a receivable state, mint
    one OPEN ``HaulingTask`` per line moving the ordered hulls from the supplier's
    hub to the PO's delivery location. The corp hauls what the supplier dropped at
    the hub — so this only makes sense for ``HUB_PICKUP`` deliveries.

    Deliberately one task per line (no route optimisation here): the freight desk
    claims and consolidates. Idempotency is not enforced (a second press mints a
    second batch) — the officer decides, and every press is audited."""
    from apps.industry.mrp import _packaged_volume
    from apps.market.models import MarketLocation
    from apps.stockpile.models import HaulingTask

    locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if locked.delivery_mode != PurchaseOrder.DeliveryMode.HUB_PICKUP:
        raise ProcurementError(_("Only a hub-pickup purchase order needs a corp haul."))
    if locked.status not in RECEIVABLE_STATUSES:
        raise ProcurementError(_("This purchase order is not ready to be hauled yet."))

    source = locked.supplier.default_location or (
        MarketLocation.objects.filter(is_price_reference=True).order_by("pk").first()
    )
    tasks = []
    for line in locked.lines.all():
        tasks.append(HaulingTask.objects.create(
            type_id=line.type_id,
            quantity=line.quantity_ordered,
            volume_m3=_packaged_volume(line.type_id) * line.quantity_ordered,
            source_location=source,
            dest_location=locked.location,
            status=HaulingTask.Status.OPEN,
        ))
    audit_log(actor, "procurement.po_haul_created", target_type="purchase_order",
              target_id=str(locked.pk), metadata={"tasks": len(tasks)})
    return tasks


def _notify_linked_needs(po: PurchaseOrder) -> None:
    """A fully-delivered PO flags its linked shipyard needs (best-effort). Linked
    MRP requirements close on the next run when the arrived stock clears gross."""
    try:
        from apps.store.supply import on_vehicle_completed

        on_vehicle_completed(purchase_order=po)
    except Exception:  # noqa: BLE001 — notification must never break the receipt
        import logging

        logging.getLogger("forca.procurement").exception(
            "PO-completed notification failed (po %s)", po.pk
        )


@transaction.atomic
def auto_receive_from_contract(po: PurchaseOrder, *, actor=None) -> list[PoReceipt]:
    """The §17 "no manual bookkeeping" path: a matched contract has finished, so
    its landed quantities post as ``contract_auto`` receipts against the type-level
    lines. Fit lines are skipped (assembly is a human act). Clamping keeps a
    re-run idempotent."""
    locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if locked.status not in RECEIVABLE_STATUSES or not locked.contract_id:
        return []
    landed: dict[int, int] = {}
    for entry in locked.contract_items or []:
        try:
            tid = int(entry.get("type_id"))
            qty = int(entry.get("quantity"))
        except (TypeError, ValueError):
            continue
        if qty > 0:
            landed[tid] = landed.get(tid, 0) + qty

    # ``contract_items`` are the contract's CUMULATIVE landed totals, so a re-run
    # must not post them again: seed the per-type consumed tally with the landed
    # quantity already auto-received on this PO. This makes the whole call idempotent.
    consumed: dict[int, int] = {}
    prior = (
        PoReceipt.objects.filter(po=locked, kind=PoReceipt.Kind.CONTRACT_AUTO)
        .values("line__type_id").annotate(total=models.Sum("quantity"))
    )
    for row in prior:
        consumed[row["line__type_id"]] = row["total"] or 0

    receipts: list[PoReceipt] = []
    for line in locked.lines.filter(doctrine_fit__isnull=True):
        available = landed.get(line.type_id, 0) - consumed.get(line.type_id, 0)
        outstanding = line.quantity_ordered - line.quantity_received
        take = min(available, outstanding)
        if take <= 0:
            continue
        receipts.append(receive_po_delivery(
            locked, line, take, actor=actor, kind=PoReceipt.Kind.CONTRACT_AUTO,
            contract_id=locked.contract_id,
        ))
        consumed[line.type_id] = consumed.get(line.type_id, 0) + take
    return receipts
