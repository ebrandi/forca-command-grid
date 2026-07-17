"""P6 freight pipeline — consolidate → assign → transit → receive.

A **freight batch** consolidates purchase/import lines for one lane (origin →
destination) and one trip. Officers open a batch, add typed lines (or the MRP
fan-out appends a planned line), assign it to the existing courier flow (or a
member haul), track ETD/ETA, and **receipt** landed stock one deliberate,
audited transaction per line. Between the ISK leaving the wallet and the goods
landing, every unreceipted line quantity is **in-transit supply** that feeds the
P3 MRP pool as a destination-pinned scheduled receipt — so "bought at Jita" is
visibly incoming, never available, until receipted (P1 rule 7 is untouched).

This module owns the lifecycle, the locks, the freight-share allocation and the
``in_transit()`` reader. It consumes the rate card, ``quote()`` and
``create_contract_from_quote`` as-is — the batch never re-prices, never
re-verifies. The app only PLANS: it records that an officer bought/shipped/
received; the buying, contracting and flying happen in game by hand.

**Global lock order** (extending the ``erp.deliver`` order, which P4 grew with
``PurchaseOrder``)::

    BuildJob → IndustryProject → PurchaseOrder → FreightBatch → FreightBatchLine
        → StockpileItem (pk asc) → StockReservation

Every batch transition and line mutation takes ``select_for_update()`` on the
``FreightBatch`` row first and re-validates ``status`` under that lock (the P1
status-guard-every-transition discipline), then its lines, before touching stock.
The receipt path holds the batch + line locks while blind-incrementing the
destination ``StockpileItem`` — so freight slots **before** ``StockpileItem`` in
the order. No existing transaction acquires a freight lock, so the insertion is
deadlock-free by vacuity; every future writer must honour the order.

``NetRequirement`` sits **outermost**: ``add_requirement_to_batch`` (and the MRP
reconcile refresh) may hold it before acquiring ``FreightBatch`` → ``FreightBatchLine``,
and no freight path ever locks a ``NetRequirement`` while holding a freight lock — so
``NetRequirement`` → FreightBatch → FreightBatchLine is the consistent acquisition
order across the industry ↔ logistics boundary.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext as _

from core.audit import audit_log

from .models import (
    CourierContract,
    FreightBatch,
    FreightBatchLine,
    FreightConfig,
    FreightReceipt,
)

log = logging.getLogger("forca.logistics")

# Batch statuses whose unreceipted lines are live in-transit supply (a purchase on
# an OPEN batch IS incoming — bought and sitting at origin). Terminal batches
# (CLOSED / CANCELLED) contribute nothing.
ACTIVE_BATCH_STATUSES = (
    FreightBatch.Status.OPEN,
    FreightBatch.Status.ASSIGNED,
    FreightBatch.Status.IN_TRANSIT,
    FreightBatch.Status.ARRIVED,
)
# Only a batch that is actually moving (or arrived) may post stock.
_RECEIVABLE_STATUSES = (FreightBatch.Status.IN_TRANSIT, FreightBatch.Status.ARRIVED)

_CENT = Decimal("0.01")


class FreightError(Exception):
    """A refused freight operation carrying a translated, user-safe message."""


@dataclass(frozen=True)
class TransitLot:
    """One in-transit (or received-but-unsynced) supply lot for the MRP pool.

    ``kind`` is ``in_transit`` (unreceipted remainder, dated by the batch ETA) or
    ``received_unsynced`` (the covered-destination bridge — receipted units the
    corp-asset mirror has not caught up to yet, ``eta=None``).
    """

    line_id: int
    type_id: int
    destination_id: int
    remaining: int
    eta: datetime | None
    kind: str


# --------------------------------------------------------------------------- #
#  Lane / batch open
# --------------------------------------------------------------------------- #
def _get_or_create_open_batch(origin, destination, *, actor, ip=None) -> FreightBatch:
    """Return the lane's OPEN batch, row-locked, creating it if absent.

    The ``push_project_to_jobs`` IntegrityError-collapse under the OPEN-per-lane
    partial unique: two racers converge on one row. Must run inside a transaction —
    the returned batch is locked for the caller's subsequent line/assign work.
    """
    existing = (
        FreightBatch.objects.select_for_update()
        .filter(origin=origin, destination=destination, status=FreightBatch.Status.OPEN)
        .first()
    )
    if existing is not None:
        return existing
    created = False
    try:
        with transaction.atomic():
            batch = FreightBatch.objects.create(
                origin=origin, destination=destination, created_by=actor,
                status=FreightBatch.Status.OPEN,
            )
            created = True
    except IntegrityError:
        batch = (
            FreightBatch.objects.select_for_update()
            .filter(origin=origin, destination=destination, status=FreightBatch.Status.OPEN)
            .first()
        )
    if created:
        audit_log(actor, "freight.batch_create", target_type="freight_batch",
                  target_id=str(batch.pk),
                  metadata={"origin": origin.pk, "destination": destination.pk}, ip=ip)
    return batch


@transaction.atomic
def open_batch_for_lane(origin, destination, *, actor, ip=None) -> FreightBatch:
    """Officer entry point: get-or-create the lane's single OPEN batch (audited)."""
    return _get_or_create_open_batch(origin, destination, actor=actor, ip=ip)


def _lock_open_batch(batch_id: int) -> FreightBatch:
    """Lock a batch and require it OPEN (line ops); raise otherwise."""
    locked = FreightBatch.objects.select_for_update().get(pk=batch_id)
    if locked.status != FreightBatch.Status.OPEN:
        raise FreightError(_("Lines can only change while the batch is open."))
    return locked


# --------------------------------------------------------------------------- #
#  Line ops (OPEN only, batch locked first)
# --------------------------------------------------------------------------- #
@transaction.atomic
def add_line(batch, *, type_id: int, quantity: int, actor, unit_purchase_cost=None,
             cost_source: str = "", purchase_ref: str = "", ip=None) -> FreightBatchLine:
    """Add or consolidate an officer-typed line on an OPEN batch."""
    quantity = int(quantity)
    if quantity < 1:
        raise FreightError(_("Enter a quantity of at least 1."))
    locked = _lock_open_batch(batch.pk)
    line = (
        FreightBatchLine.objects.select_for_update()
        .filter(batch=locked, type_id=int(type_id)).first()
    )
    if line is None:
        line = FreightBatchLine.objects.create(
            batch=locked, type_id=int(type_id), quantity=quantity,
            unit_purchase_cost=unit_purchase_cost,
            cost_source=cost_source or ("typed" if unit_purchase_cost is not None else ""),
            purchase_ref=purchase_ref or "",
        )
    else:
        line.quantity = int(line.quantity) + quantity
        fields = ["quantity"]
        if unit_purchase_cost is not None:
            line.unit_purchase_cost = unit_purchase_cost
            line.cost_source = cost_source or "typed"
            fields += ["unit_purchase_cost", "cost_source"]
        if purchase_ref:
            line.purchase_ref = purchase_ref
            fields.append("purchase_ref")
        line.save(update_fields=fields)
    audit_log(actor, "freight.line_add", target_type="freight_batch_line",
              target_id=str(line.pk),
              metadata={"batch": locked.pk, "type_id": int(type_id), "quantity": quantity}, ip=ip)
    return line


_UNSET = object()


@transaction.atomic
def edit_line(line, *, quantity=None, unit_purchase_cost=_UNSET, purchase_ref=None,
              actor, ip=None) -> FreightBatchLine:
    """Edit an officer line on an OPEN batch (quantity / purchase cost / evidence)."""
    _lock_open_batch(line.batch_id)
    locked = FreightBatchLine.objects.select_for_update().get(pk=line.pk)
    fields: list[str] = []
    if quantity is not None:
        quantity = int(quantity)
        floor = max(1, int(locked.planned_quantity))
        if quantity < floor:
            raise FreightError(
                _("Quantity can't drop below the plan's %(n)s units.") % {"n": floor}
            )
        locked.quantity = quantity
        fields.append("quantity")
    if unit_purchase_cost is not _UNSET:
        locked.unit_purchase_cost = unit_purchase_cost
        locked.cost_source = "typed" if unit_purchase_cost is not None else ""
        fields += ["unit_purchase_cost", "cost_source"]
    if purchase_ref is not None:
        locked.purchase_ref = purchase_ref[:64]
        fields.append("purchase_ref")
    if fields:
        locked.save(update_fields=fields)
    audit_log(actor, "freight.line_edit", target_type="freight_batch_line",
              target_id=str(locked.pk), metadata={"batch": locked.batch_id}, ip=ip)
    return locked


@transaction.atomic
def remove_line(line, *, actor, ip=None) -> None:
    """Remove a line from an OPEN batch (the NetRequirement FK auto-nulls, SET_NULL)."""
    _lock_open_batch(line.batch_id)
    locked = FreightBatchLine.objects.select_for_update().get(pk=line.pk)
    pk, tid, batch_id = locked.pk, locked.type_id, locked.batch_id
    locked.delete()
    audit_log(actor, "freight.line_remove", target_type="freight_batch_line",
              target_id=str(pk), metadata={"batch": batch_id, "type_id": tid}, ip=ip)


@transaction.atomic
def add_requirement_to_batch(requirement, *, actor, ip=None) -> FreightBatchLine:
    """MRP fan-out: append this requirement's planned share to its lane's OPEN batch.

    Idempotent per requirement (no-op when ``freight_line`` is already set). The
    target goes into BOTH ``quantity`` and ``planned_quantity`` — on a pre-existing
    officer line the officer's units persist as ``quantity − planned_quantity``; the
    fan-out never overwrites a quantity it did not contribute. Leaves
    ``unit_purchase_cost`` null so the line stays "unclaimed" (reconciliation may
    refresh the planned share until an officer types a cost).
    """
    from apps.industry.models import NetRequirement
    from apps.industry.mrp import _vehicle_target
    from apps.market.models import MarketLocation

    locked_req = NetRequirement.objects.select_for_update().get(pk=requirement.pk)
    if locked_req.freight_line_id:
        return locked_req.freight_line
    if locked_req.location_id is None:
        raise FreightError(_("This requirement has no destination to freight to."))
    origin = MarketLocation.objects.filter(is_price_reference=True).order_by("pk").first()
    if origin is None:
        raise FreightError(_("No price-reference hub is configured to freight from."))
    batch = _get_or_create_open_batch(origin, locked_req.location, actor=actor, ip=ip)
    target = max(1, _vehicle_target(locked_req))
    line = (
        FreightBatchLine.objects.select_for_update()
        .filter(batch=batch, type_id=locked_req.type_id).first()
    )
    if line is None:
        line = FreightBatchLine.objects.create(
            batch=batch, type_id=locked_req.type_id, quantity=target, planned_quantity=target,
        )
    else:
        line.quantity = int(line.quantity) + target
        line.planned_quantity = int(line.planned_quantity) + target
        line.save(update_fields=["quantity", "planned_quantity"])
    locked_req.freight_line = line
    locked_req.status = NetRequirement.Status.IN_PROGRESS
    locked_req.save(update_fields=["freight_line", "status", "updated_at"])
    audit_log(actor, "freight.line_add", target_type="freight_batch_line",
              target_id=str(line.pk),
              metadata={"requirement": locked_req.pk, "quantity": target, "action": "freight"},
              ip=ip)
    return line


# --------------------------------------------------------------------------- #
#  Assignment + freight-share allocation
# --------------------------------------------------------------------------- #
def _lane_endpoint(loc) -> dict:
    """A trusted resolved-location dict for ``create_contract_from_quote`` from a
    corp ``MarketLocation`` (structure when it has one, else the system)."""
    if loc.structure_id:
        return {"kind": "structure", "id": loc.structure_id, "name": loc.name,
                "system_id": loc.system_id}
    return {"kind": "system", "id": loc.system_id, "name": loc.name,
            "system_id": loc.system_id}


def _line_collateral(line) -> Decimal:
    """This line's collateral (value): typed purchase cost, else the price snapshot."""
    from apps.market.pricing import price_for

    unit = line.unit_purchase_cost
    if unit is None:
        unit = Decimal(price_for(line.type_id) or 0)
    return Decimal(unit) * int(line.quantity)


def _price_batch(card, batch, lines, ship_class, rush):
    """Volume/collateral totals + a frozen ``quote()`` for the leg.

    Returns ``(quote, value_pool, volume_m3, collateral)`` where ``value_pool`` is the
    collateral-driven part of the reward (``reward(collateral) − reward(0)``) — the
    honest "extra the cargo's value costs to insure", computed with the quote engine
    itself (no second pricing definition, no reliance on translated line labels).
    """
    from apps.logistics.costing import packaged_volume
    from apps.logistics.jumps import SHIPS_BY_KEY, effective_range
    from apps.logistics.models import ShipClass
    from apps.logistics.pricing import quote
    from apps.logistics.routing import RouteUnavailable, jf_route_facts

    volume = 0.0
    collateral = Decimal("0")
    for ln in lines:
        volume += packaged_volume(ln.type_id) * int(ln.quantity)
        collateral += _line_collateral(ln)

    jumps, band, routable = 0, "highsec", False
    rng = effective_range(SHIPS_BY_KEY["jf"]["range"], int(card.jf_assumed_jdc))
    try:
        facts = jf_route_facts(batch.origin.system_id, batch.destination.system_id, rng)
        jumps, band, routable = facts["jumps"], facts["sec_band"], facts["jumps"] > 0
    except RouteUnavailable:
        pass
    is_jf = ship_class == ShipClass.JF

    def _q(coll):
        return quote(card, ship_class=ship_class, jumps=jumps,
                     jump_hops=(jumps if is_jf else None),
                     volume_m3=volume, collateral=coll, sec_band=band, rush=rush)

    q_full = _q(collateral)
    if not q_full.ok:
        if not routable:
            # Clearer than quote()'s generic "enter jumps" for a lane the officer picked
            # by system; reuses the existing (already-translated) routing refusal string.
            raise FreightError(_("No jump route within range between those systems."))
        raise FreightError(q_full.error)
    q_zero = _q(Decimal("0"))
    value_pool = q_full.reward - (q_zero.reward if q_zero.ok else q_full.reward)
    if value_pool < 0:
        value_pool = Decimal("0")
    return q_full, value_pool, volume, collateral


def _split_cents(total: Decimal, weights: list[Decimal]) -> list[int]:
    """Split ``total`` (a whole-cent Decimal) across lines by ``weights`` in integer
    cents — floor each but the last, remainder into the LAST line (pk order upstream).
    Deterministic and exactly-summing: Σ result == round(total×100)."""
    total_cents = int((total * 100).to_integral_value(ROUND_HALF_UP))
    n = len(weights)
    if n == 0:
        return []
    if total_cents == 0:
        return [0] * n
    wsum = sum(weights)
    if wsum <= 0:
        # No weight to split by — dump the whole pool on the last line, deterministically.
        return [0] * (n - 1) + [total_cents]
    out: list[int] = []
    assigned = 0
    for i in range(n - 1):
        c = int((Decimal(total_cents) * weights[i] / wsum).to_integral_value(ROUND_FLOOR))
        out.append(c)
        assigned += c
    out.append(total_cents - assigned)
    return out


def _allocate_freight_shares(lines, freight_cost: Decimal, value_pool: Decimal) -> None:
    """Freeze each line's slice of ``freight_cost`` (§17 landed-cost split).

    Space components (base/route/jumps/rush) split by packaged-m³ share; the value
    component (``value_pool``) splits by collateral share. Min-reward loads have
    ``value_pool == 0`` → the whole reward splits by m³ alone. Invariant (tested):
    Σ ``freight_share`` == ``freight_cost`` exactly, in every shape.
    """
    from apps.logistics.costing import packaged_volume

    lines = sorted(lines, key=lambda ln: ln.pk)
    space_w = [Decimal(str(packaged_volume(ln.type_id) * int(ln.quantity))) for ln in lines]
    value_w = [_line_collateral(ln) for ln in lines]
    space_pool = freight_cost - value_pool
    space_cents = _split_cents(space_pool, space_w)
    value_cents = _split_cents(value_pool, value_w)
    for i, ln in enumerate(lines):
        ln.freight_share = (Decimal(space_cents[i] + value_cents[i]) / 100).quantize(_CENT)
        ln.save(update_fields=["freight_share"])


@transaction.atomic
def assign_batch(batch, *, ship_class=None, rush: bool = False, actor, ip=None) -> FreightBatch:
    """Price an OPEN batch, post ONE courier contract, freeze cost + shares → ASSIGNED.

    Refuses (surfacing ``quote()``'s translated error verbatim) when the load is over
    the ship class's volume/collateral cap — never silently splits. Re-priceable via
    :func:`unassign_batch`.
    """
    from apps.logistics.services import active_rate_card, create_contract_from_quote

    locked = FreightBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.status != FreightBatch.Status.OPEN:
        raise FreightError(_("Only an open batch can be assigned."))
    lines = list(locked.lines.select_for_update().order_by("pk"))
    if not lines:
        raise FreightError(_("Add at least one line before assigning."))
    cfg = FreightConfig.active()
    ship_class = ship_class or cfg.default_ship_class
    card = active_rate_card()
    q_full, value_pool, volume, collateral = _price_batch(card, locked, lines, ship_class, rush)

    from apps.erp.messages import english_text

    note = english_text("freight.batch_leg",
                        {"origin": locked.origin.name, "destination": locked.destination.name})
    contract = create_contract_from_quote(
        quote=q_full, card=card,
        origin=_lane_endpoint(locked.origin), dest=_lane_endpoint(locked.destination),
        ship_class=ship_class, volume_m3=volume, collateral=collateral, rush=rush,
        created_by=actor, notes=note,
    )
    locked.courier_contract = contract
    locked.ship_class = ship_class
    locked.freight_cost = q_full.reward
    locked.freight_breakdown = q_full.breakdown
    if locked.etd_planned is None:
        locked.etd_planned = timezone.now() + timedelta(days=int(cfg.default_dispatch_days))
    locked.status = FreightBatch.Status.ASSIGNED
    locked.save(update_fields=[
        "courier_contract", "ship_class", "freight_cost", "freight_breakdown",
        "etd_planned", "status", "updated_at",
    ])
    _allocate_freight_shares(lines, q_full.reward, value_pool)
    audit_log(actor, "freight.batch_assign", target_type="freight_batch",
              target_id=str(locked.pk),
              metadata={"vehicle": "courier", "contract": contract.pk,
                        "reward": int(q_full.reward), "ship_class": ship_class}, ip=ip)
    return locked


@transaction.atomic
def assign_to_haul_board(batch, *, actor, ip=None) -> FreightBatch:
    """Assign an OPEN batch to a member ``HaulingTask`` (small loads) → ASSIGNED.

    Writes the batch manifest into the task's ``manifest`` JSONField (adopting the
    dead field — §3.5 renders it on the board) and books PACKAGED volume. Member
    hauls carry no freight reward, so landed cost is purchase-only.
    """
    from apps.logistics.costing import packaged_volume
    from apps.stockpile.models import HaulingTask

    locked = FreightBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.status != FreightBatch.Status.OPEN:
        raise FreightError(_("Only an open batch can be assigned."))
    lines = list(locked.lines.order_by("pk"))
    if not lines:
        raise FreightError(_("Add at least one line before assigning."))
    volume = sum(packaged_volume(ln.type_id) * int(ln.quantity) for ln in lines)
    manifest = [{"type_id": ln.type_id, "quantity": int(ln.quantity)} for ln in lines]
    haul = HaulingTask.objects.create(
        manifest=manifest,
        quantity=sum(int(ln.quantity) for ln in lines),
        volume_m3=volume,
        source_location=locked.origin,
        dest_location=locked.destination,
        status=HaulingTask.Status.OPEN,
    )
    cfg = FreightConfig.active()
    locked.hauling_task = haul
    if locked.etd_planned is None:
        locked.etd_planned = timezone.now() + timedelta(days=int(cfg.default_dispatch_days))
    locked.status = FreightBatch.Status.ASSIGNED
    locked.save(update_fields=["hauling_task", "etd_planned", "status", "updated_at"])
    audit_log(actor, "freight.batch_assign", target_type="freight_batch",
              target_id=str(locked.pk),
              metadata={"vehicle": "haul_board", "hauling_task": haul.pk}, ip=ip)
    return locked


def _void_vehicle(batch) -> None:
    """Cancel/close the batch's execution vehicle (unassign / cancel disposition)."""
    if batch.courier_contract_id:
        contract = CourierContract.objects.filter(pk=batch.courier_contract_id).first()
        if contract and contract.status not in (
            CourierContract.Status.DELIVERED, CourierContract.Status.FAILED,
            CourierContract.Status.CANCELLED,
        ):
            contract.status = CourierContract.Status.CANCELLED
            contract.save(update_fields=["status", "updated_at"])
    if batch.hauling_task_id:
        from apps.stockpile.models import HaulingTask

        haul = HaulingTask.objects.filter(pk=batch.hauling_task_id).first()
        if haul and haul.status != HaulingTask.Status.DONE:
            haul.status = HaulingTask.Status.DONE
            haul.save(update_fields=["status"])


@transaction.atomic
def unassign_batch(batch, *, actor, ip=None) -> FreightBatch:
    """ASSIGNED → OPEN escape hatch: void the vehicle, zero the frozen cost + shares.

    Lines, typed costs and receipts-so-far survive; re-assigning re-prices from
    scratch (never reuses a stale quote)."""
    locked = FreightBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.status != FreightBatch.Status.ASSIGNED:
        raise FreightError(_("Only an assigned batch can be unassigned."))
    _void_vehicle(locked)
    locked.courier_contract = None
    locked.hauling_task = None
    locked.freight_cost = Decimal("0")
    locked.freight_breakdown = {}
    locked.status = FreightBatch.Status.OPEN
    locked.save(update_fields=[
        "courier_contract", "hauling_task", "freight_cost", "freight_breakdown",
        "status", "updated_at",
    ])
    for ln in locked.lines.all():
        if ln.freight_share:
            ln.freight_share = Decimal("0")
            ln.save(update_fields=["freight_share"])
    audit_log(actor, "freight.batch_unassign", target_type="freight_batch",
              target_id=str(locked.pk), metadata={}, ip=ip)
    return locked


@transaction.atomic
def mark_departed(batch, *, actor, ip=None) -> FreightBatch:
    """ASSIGNED → IN_TRANSIT: stamp departure, default an unset ETA."""
    locked = FreightBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.status != FreightBatch.Status.ASSIGNED:
        raise FreightError(_("Only an assigned batch can depart."))
    locked.departed_at = timezone.now()
    if locked.eta_planned is None:
        cfg = FreightConfig.active()
        locked.eta_planned = locked.departed_at + timedelta(days=int(cfg.default_transit_days))
    locked.status = FreightBatch.Status.IN_TRANSIT
    locked.save(update_fields=["departed_at", "eta_planned", "status", "updated_at"])
    audit_log(actor, "freight.batch_depart", target_type="freight_batch",
              target_id=str(locked.pk), metadata={}, ip=ip)
    return locked


def update_eta(batch, *, eta, actor, ip=None) -> FreightBatch:
    """Set a batch ETA (audited). A *slip* (new ETA later than old) fires the
    event-gated, idempotent ``logistics.batch_delayed`` alert."""
    with transaction.atomic():
        locked = FreightBatch.objects.select_for_update().get(pk=batch.pk)
        if locked.is_terminal:
            raise FreightError(_("This batch is already closed."))
        old = locked.eta_planned
        locked.eta_planned = eta
        # A new ETA re-opens the late window: clear the stamp so the sweep can flag
        # this fresh ETA once (re-fires only after an ETA change).
        locked.late_flagged_at = None
        locked.save(update_fields=["eta_planned", "late_flagged_at", "updated_at"])
        audit_log(actor, "freight.batch_eta", target_type="freight_batch",
                  target_id=str(locked.pk),
                  metadata={"eta": eta.isoformat() if eta else None}, ip=ip)
    if old and eta and eta > old:
        _emit_batch_alert(
            locked, template="logistics.batch_delayed", event_key="logistics.batch_delayed",
            idempotency_key=f"freight:delay:{locked.pk}:{int(eta.timestamp())}",
            title="Freight batch delayed",
            body=(f"Freight batch {locked.label} slipped to "
                  f"{eta.date().isoformat()}."),
            context=_alert_context(locked, eta=eta),
        )
    return locked


def mark_arrived(batch, *, actor=None, from_sweep: bool = False, ip=None) -> FreightBatch:
    """ASSIGNED/IN_TRANSIT → ARRIVED (manual or from the sweep). Fires
    ``logistics.batch_arrived``. **Never posts stock** — the receipt is the
    deliberate human step. Legal from ASSIGNED (a courier can deliver before anyone
    clicked depart)."""
    with transaction.atomic():
        locked = FreightBatch.objects.select_for_update().get(pk=batch.pk)
        if locked.status not in (FreightBatch.Status.ASSIGNED, FreightBatch.Status.IN_TRANSIT):
            raise FreightError(_("This batch can't be marked arrived."))
        locked.arrived_at = timezone.now()
        locked.status = FreightBatch.Status.ARRIVED
        locked.save(update_fields=["arrived_at", "status", "updated_at"])
        audit_log(actor, "freight.batch_arrive", target_type="freight_batch",
                  target_id=str(locked.pk), metadata={"from_sweep": from_sweep}, ip=ip)
    _emit_batch_alert(
        locked, template="logistics.batch_arrived", event_key="logistics.batch_arrived",
        idempotency_key=f"freight:arrived:{locked.pk}",
        title="Freight batch arrived",
        body=f"Freight batch {locked.label} has arrived — receipt its lines to post stock.",
        context=_alert_context(locked),
    )
    return locked


@transaction.atomic
def cancel_batch(batch, *, actor, ip=None) -> FreightBatch:
    """Cancel a non-terminal batch (voids the vehicle; releases nothing into stock).
    Receipts already posted survive (immutable evidence)."""
    locked = FreightBatch.objects.select_for_update().get(pk=batch.pk)
    if locked.is_terminal:
        raise FreightError(_("This batch is already closed."))
    _void_vehicle(locked)
    locked.status = FreightBatch.Status.CANCELLED
    locked.save(update_fields=["status", "updated_at"])
    audit_log(actor, "freight.batch_cancel", target_type="freight_batch",
              target_id=str(locked.pk), metadata={}, ip=ip)
    return locked


# --------------------------------------------------------------------------- #
#  The receipt transaction (acceptance №6)
# --------------------------------------------------------------------------- #
@transaction.atomic
def receive_line(line, quantity: int, *, actor, stockpile=None, ip=None) -> FreightReceipt:
    """THE receipt: post landed stock, write immutable evidence, shrink the transit
    lot — one atomic transaction so no durable state ever double-counts or loses the
    units. Lock order: FreightBatch → FreightBatchLine → StockpileItem.
    """
    from apps.industry.mrp import _corp_stockpile_at
    from apps.stockpile.models import StockpileItem

    batch = FreightBatch.objects.select_for_update().get(pk=line.batch_id)
    locked = FreightBatchLine.objects.select_for_update().get(pk=line.pk)
    if batch.status not in _RECEIVABLE_STATUSES:
        raise FreightError(_("This batch is not in transit."))
    qty = int(quantity)
    remaining = locked.remaining
    if qty <= 0 or qty > remaining:
        raise FreightError(
            _("Enter a quantity between 1 and %(n)s.") % {"n": remaining}
        )
    sp = stockpile or _corp_stockpile_at(batch.destination_id)
    if sp is None:
        raise FreightError(
            _("No corp stockpile exists at the destination — create one first.")
        )
    item, _created = StockpileItem.objects.get_or_create(stockpile=sp, type_id=locked.type_id)
    # Blind F() increment — the row is fresh or lock-ordered behind the batch/line locks
    # (the erp.deliver discipline), never a read-modify-write.
    StockpileItem.objects.filter(pk=item.pk).update(quantity_current=F("quantity_current") + qty)

    locked.quantity_received = int(locked.quantity_received) + qty
    fields = ["quantity_received"]
    if locked.quantity_received >= int(locked.quantity):
        locked.received_at = timezone.now()
        locked.received_by = actor
        fields += ["received_at", "received_by"]
    locked.save(update_fields=fields)

    landed = None
    if locked.unit_purchase_cost is not None:
        landed = (
            Decimal(locked.unit_purchase_cost)
            + (Decimal(locked.freight_share) / int(locked.quantity))
        ).quantize(_CENT)
    receipt = FreightReceipt.objects.create(
        line=locked, stockpile=sp, quantity=qty, unit_landed_cost=landed, received_by=actor,
    )
    # Close the batch once every line is fully received.
    if not batch.lines.exclude(quantity_received=F("quantity")).exists():
        batch.status = FreightBatch.Status.CLOSED
        batch.save(update_fields=["status", "updated_at"])
    audit_log(actor, "freight.line_receive", target_type="freight_batch_line",
              target_id=str(locked.pk),
              metadata={"batch": batch.pk, "quantity": qty,
                        "landed": str(landed) if landed is not None else None}, ip=ip)
    return receipt


# --------------------------------------------------------------------------- #
#  The in-transit reader (feeds the MRP pool)
# --------------------------------------------------------------------------- #
def _batch_eta(batch, cfg) -> datetime | None:
    """A batch's ETA: the planned ETA, else ETD + default transit, else None
    (never a fabricated date)."""
    if batch.eta_planned:
        return batch.eta_planned
    if batch.etd_planned:
        return batch.etd_planned + timedelta(days=int(cfg.default_transit_days))
    return None


def _corp_assets_synced_at() -> datetime | None:
    """When the corp-asset mirror last synced (the ``sync:corp_assets`` health stamp)."""
    from django.utils.dateparse import parse_datetime

    from apps.admin_audit.models import AppSetting

    setting = AppSetting.objects.filter(key="sync:corp_assets").first()
    if not setting or not setting.value:
        return None
    stamp = setting.value.get("at") if isinstance(setting.value, dict) else None
    return parse_datetime(stamp) if stamp else None


def _destination_covered(location) -> bool:
    """Whether the corp-asset mirror holds ≥1 row at this destination (ESI-covered)."""
    from django.conf import settings

    from apps.stockpile.models import Asset
    from apps.stockpile.services import _asset_location_ids_for

    loc_ids = _asset_location_ids_for(location)
    if not loc_ids:
        return False
    return Asset.objects.filter(
        owner_type=Asset.Owner.CORPORATION,
        owner_id=settings.FORCA_HOME_CORP_ID,
        location_id__in=loc_ids,
    ).exists()


def _received_unsynced_lots(type_ids, destination) -> list[TransitLot]:
    """The covered-destination bridge lots: receipted units the corp-asset mirror
    has not caught up to yet (per-receipt aging), so P3 never re-demands them in
    the sync window. Emitted ONLY at ESI-covered destinations (at uncovered ones the
    receipt IS the availability truth and a lot would double-count)."""
    synced_at = _corp_assets_synced_at()
    if synced_at is None:
        # No freshness signal (the corp-asset mirror never stamped a sync). The bridge
        # lot's whole justification is "the mirror hasn't caught up SINCE the receipt" —
        # without a stamp we cannot honestly assert that, so we don't fabricate the
        # claim (honest-data rule) and emit nothing. This also avoids resurrecting every
        # historical receipt as phantom supply. Covered destinations always carry a stamp
        # in normal operation (a sync both writes the assets and stamps), so this only
        # affects the abnormal stamp-missing state — where re-planning as if uncovered is
        # the visible, self-correcting failure rather than a silent permanent over-count.
        return []
    qs = FreightReceipt.objects.select_related(
        "line", "line__batch", "line__batch__destination"
    ).filter(created_at__gt=synced_at)
    if type_ids is not None:
        qs = qs.filter(line__type_id__in=list(type_ids))
    if destination is not None:
        qs = qs.filter(line__batch__destination=destination)

    covered_cache: dict[int, bool] = {}
    out: list[TransitLot] = []
    for receipt in qs:
        dest = receipt.line.batch.destination
        if dest is None:
            continue
        covered = covered_cache.get(dest.pk)
        if covered is None:
            covered = _destination_covered(dest)
            covered_cache[dest.pk] = covered
        if not covered:
            continue
        out.append(TransitLot(
            line_id=receipt.line_id, type_id=receipt.line.type_id,
            destination_id=dest.pk, remaining=int(receipt.quantity), eta=None,
            kind="received_unsynced",
        ))
    return out


def in_transit(type_ids=None, *, destination=None) -> list[TransitLot]:
    """Every in-transit + received-unsynced supply lot, destination-pinned.

    Read once by MRP step 0 (``type_ids=None`` → all) and by the console. The
    in-transit bucket is derived from ``quantity_received`` (never materialised), so
    it cannot drift.
    """
    cfg = FreightConfig.active()
    lines = (
        FreightBatchLine.objects.filter(batch__status__in=ACTIVE_BATCH_STATUSES)
        .select_related("batch")
    )
    if type_ids is not None:
        lines = lines.filter(type_id__in=list(type_ids))
    if destination is not None:
        lines = lines.filter(batch__destination=destination)

    lots: list[TransitLot] = []
    for line in lines:
        remaining = line.remaining
        if remaining <= 0:
            continue
        lots.append(TransitLot(
            line_id=line.pk, type_id=line.type_id, destination_id=line.batch.destination_id,
            remaining=remaining, eta=_batch_eta(line.batch, cfg), kind="in_transit",
        ))
    lots.extend(_received_unsynced_lots(type_ids, destination))
    return lots


# --------------------------------------------------------------------------- #
#  View support: capacity fit + landed-vs-forecast
# --------------------------------------------------------------------------- #
def capacity_fit(batch, *, ship_class=None, card=None) -> dict:
    """Packaged-m³ fill and collateral vs the rate card's caps for a ship class."""
    from apps.logistics.costing import packaged_volume
    from apps.logistics.pricing import caps_for
    from apps.logistics.services import active_rate_card

    card = card or active_rate_card()
    ship_class = ship_class or batch.ship_class
    lines = list(batch.lines.all())
    volume = sum(packaged_volume(ln.type_id) * int(ln.quantity) for ln in lines)
    collateral = sum((_line_collateral(ln) for ln in lines), Decimal("0"))
    max_m3, max_coll = caps_for(card, ship_class)
    return {
        "ship_class": ship_class,
        "volume_m3": volume, "max_m3": max_m3,
        "fill_pct": round(100 * volume / max_m3, 1) if max_m3 else 0.0,
        "collateral": collateral, "max_collateral": max_coll,
        "over_cap": volume > max_m3 or collateral > max_coll,
    }


def landed_vs_forecast(batch) -> list[dict]:
    """Per received type: actual landed unit cost vs the forecaster's import basis.

    Reads ``FreightReceipt``/forecast values — never recomputes either. A line with
    no purchase cost renders its null landed state (never a fabricated 0)."""
    from apps.logistics.costing import _freight_unit, _staging_hops
    from apps.logistics.services import active_rate_card
    from apps.market.pricing import price_for

    dest_sys = batch.destination.system_id if batch.destination_id else 0
    card = active_rate_card()
    hops = _staging_hops(dest_sys) if dest_sys else 0
    rows = []
    for line in batch.lines.prefetch_related("receipts").all():
        receipts = list(line.receipts.all())
        received = sum(int(r.quantity) for r in receipts)
        if received <= 0:
            continue
        landed_units = [r.unit_landed_cost for r in receipts if r.unit_landed_cost is not None]
        landed = (sum(landed_units) / len(landed_units)).quantize(_CENT) if landed_units else None
        # Forecaster's import basis: Jita unit + per-hull jump freight (its own primitives).
        jita = Decimal(price_for(line.type_id) or 0)
        forecast_import = None
        if jita > 0:
            from apps.doctrines.hulls import hull_class_for_group
            from apps.sde.models import SdeType

            gid = SdeType.objects.filter(type_id=line.type_id).values_list(
                "group_id", flat=True).first()
            hull_class = hull_class_for_group(gid) if gid else "Other"
            freight = _freight_unit(card, hops, hull_class, jita) if hops else Decimal("0")
            forecast_import = (jita + freight).quantize(_CENT)
        rows.append({
            "type_id": line.type_id, "received": received,
            "landed_unit": landed, "forecast_import": forecast_import,
            "delta": (landed - forecast_import) if (landed is not None and forecast_import is not None) else None,
        })
    return rows


# --------------------------------------------------------------------------- #
#  Officer alerts
# --------------------------------------------------------------------------- #
def _alert_context(batch, *, eta=None) -> dict:
    when = eta or batch.eta_planned
    return {
        "origin_system": batch.origin.name if batch.origin_id else "",
        "destination_system": batch.destination.name if batch.destination_id else "",
        "eta_date": when.date().isoformat() if when else "",
        "count": batch.lines.count(),
    }


def _emit_batch_alert(batch, *, template, event_key, idempotency_key, title, body, context):
    """Officer-routed, event-gated, idempotent batch alert (never breaks the caller)."""
    try:
        from apps.pingboard import notifications
        from apps.pingboard import services as pingboard
        from apps.pingboard.models import AlertCategory

        if not notifications.is_enabled(event_key):
            return
        pingboard.emit_broadcast(
            category=AlertCategory.LOGISTICS, title=title, body=body,
            template=template, context=context,
            audience={"kind": "officer"},
            source_service="logistics", source_object_id=f"freight_batch:{batch.pk}",
            idempotency_key=idempotency_key,
        )
    except Exception:  # noqa: BLE001 — an alert must never break the transition/sweep
        log.exception("freight batch alert failed (%s)", batch.pk)
