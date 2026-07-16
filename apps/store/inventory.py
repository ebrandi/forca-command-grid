"""Write side of Shipyard fitted-ship inventory (SHIP-1).

Reservation lifecycle and every stock movement live here, and each one is
ledger-backed: the mutable ``FitStock.quantity_on_hand`` balance never changes
without an immutable :class:`FitStockEntry` explaining it.

Concurrency contract
--------------------
* Every path that creates reservations first locks the relevant ``FitStock``
  rows with ``select_for_update`` and re-derives ATP under that lock, so two
  buyers racing for the last ship serialize and only genuine stock is promised.
* Lock ordering is always **FitStock before StoreOrder** (allocation) — never
  the reverse — so writer paths cannot deadlock each other.
* Reservation transitions are status-guarded UPDATEs
  (``filter(status=ACTIVE).update(...)``): a double release or double consume is
  a no-op, and the ``quantity_on_hand >= 0`` check constraint is the final
  backstop against any oversell bug.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .availability import manifest_hash
from .models import (
    FitReservation,
    FitStock,
    FitStockEntry,
    ShipyardPolicy,
    StoreOrder,
)


def _entry(stock: FitStock, kind: str, delta: int, *, actor=None, order=None,
           reason: str = "") -> FitStockEntry:
    """Append one immutable ledger line for a balance change already saved."""
    return FitStockEntry.objects.create(
        stock=stock, kind=kind, delta=delta, balance_after=stock.quantity_on_hand,
        reason=reason, actor=actor, order=order,
    )


def _locked_rows(fit, *, location, current_hash: str) -> list[FitStock]:
    """Lock and return this fit's current-revision stock rows (FIFO by id).

    ``location=None`` (nothing configured) locks every location's rows, matching
    the read side's "any location counts" semantics."""
    qs = FitStock.objects.select_for_update().filter(
        doctrine_fit=fit, manifest_hash=current_hash
    )
    if location is not None:
        qs = qs.filter(location=location)
    return list(qs.order_by("id"))


def _active_reserved(rows: list[FitStock]) -> dict[int, int]:
    if not rows:
        return {}
    return {
        r["stock_id"]: r["s"]
        for r in FitReservation.objects.filter(
            stock_id__in=[s.pk for s in rows], status=FitReservation.Status.ACTIVE
        ).values("stock_id").annotate(s=Sum("quantity"))
    }


def locked_atp(fit, *, location) -> tuple[int, list[FitStock], dict[int, int]]:
    """ATP re-derived under row locks. Caller must hold a transaction."""
    rows = _locked_rows(fit, location=location, current_hash=manifest_hash(fit))
    reserved = _active_reserved(rows)
    atp = sum(max(r.quantity_on_hand - reserved.get(r.pk, 0), 0) for r in rows)
    return atp, rows, reserved


def reserve_for_order(order: StoreOrder, fit, quantity: int, *, location,
                      _rows: list[FitStock] | None = None,
                      _reserved: dict[int, int] | None = None) -> int:
    """Reserve up to ``quantity`` for ``order``, FIFO across locked stock rows.

    Returns what was actually reserved (may be less when stock is short — the
    caller decides whether the remainder becomes a backorder). Caller must hold
    a transaction; rows are locked here unless the caller already locked them
    this transaction and passes them through (``_rows``/``_reserved``)."""
    if quantity <= 0:
        return 0
    if _rows is not None and _reserved is not None:
        rows, reserved = _rows, _reserved
    else:
        _atp, rows, reserved = locked_atp(fit, location=location)
    remaining = quantity
    for row in rows:
        if remaining <= 0:
            break
        free = row.quantity_on_hand - reserved.get(row.pk, 0)
        take = min(free, remaining)
        if take > 0:
            FitReservation.objects.create(order=order, stock=row, quantity=take)
            remaining -= take
    return quantity - remaining


def release_order_reservations(order: StoreOrder, *, expired: bool = False) -> int:
    """Release every ACTIVE reservation of an order (cancellation / expiry).

    Status-guarded: safe to call twice, safe concurrently with consumption (a
    reservation consumed first simply isn't released). Returns units released."""
    to_status = (
        FitReservation.Status.EXPIRED if expired else FitReservation.Status.RELEASED
    )
    now = timezone.now()
    released = 0
    qs = FitReservation.objects.filter(order=order, status=FitReservation.Status.ACTIVE)
    for res in qs:
        updated = FitReservation.objects.filter(
            pk=res.pk, status=FitReservation.Status.ACTIVE
        ).update(status=to_status, released_at=now)
        if updated:
            released += res.quantity
    return released


@transaction.atomic
def consume_order_reservations(order: StoreOrder, *, actor=None) -> int:
    """Deliver an order's reserved stock: decrement balances, write ledger lines.

    Each reservation is consumed exactly once (status-guarded), and the stock row
    is locked before its balance moves. Returns units consumed."""
    consumed = 0
    reservations = list(
        FitReservation.objects.filter(
            order=order, status=FitReservation.Status.ACTIVE
        ).order_by("id")
    )
    now = timezone.now()
    for res in reservations:
        stock = FitStock.objects.select_for_update().get(pk=res.stock_id)
        updated = FitReservation.objects.filter(
            pk=res.pk, status=FitReservation.Status.ACTIVE
        ).update(status=FitReservation.Status.CONSUMED, consumed_at=now)
        if not updated:
            continue  # raced with a release/expiry — nothing to consume
        stock.quantity_on_hand -= res.quantity  # check constraint backstops >= 0
        stock.save(update_fields=["quantity_on_hand", "updated_at"])
        _entry(stock, FitStockEntry.Kind.CONSUMED, -res.quantity, actor=actor, order=order)
        consumed += res.quantity
    return consumed


@dataclass
class AllocationResult:
    order: StoreOrder
    quantity: int


@dataclass
class ReceiptResult:
    stock: FitStock
    allocations: list[AllocationResult] = field(default_factory=list)


def unfilled_quantity(order: StoreOrder) -> int:
    """How much of an order no reservation (live or already consumed) covers."""
    covered = (
        FitReservation.objects.filter(
            order=order,
            status__in=(FitReservation.Status.ACTIVE, FitReservation.Status.CONSUMED),
        ).aggregate(s=Sum("quantity"))["s"] or 0
    )
    return max(order.quantity - covered, 0)


def allocate_backorders(fit, *, location, policy: ShipyardPolicy | None = None) -> list[AllocationResult]:
    """Reserve freed/arrived stock for waiting backorders, oldest order first.

    Caller must hold a transaction. Locks FitStock rows, then order rows (the
    global lock order). Recomputing each order's unfilled quantity under the lock
    makes duplicate invocation idempotent."""
    current = manifest_hash(fit)
    rows = _locked_rows(fit, location=location, current_hash=current)
    if not rows:
        return []
    reserved = _active_reserved(rows)
    free_by_row = {r.pk: max(r.quantity_on_hand - reserved.get(r.pk, 0), 0) for r in rows}
    total_free = sum(free_by_row.values())
    if total_free <= 0:
        return []

    # Live semantics: any waiting order whose quantity isn't fully covered by an
    # active-or-consumed reservation is eligible — including one whose original
    # hold expired (its FROZEN quantity_backordered snapshot may say 0).
    waiting = list(
        StoreOrder.objects.select_for_update()
        .filter(
            kind=StoreOrder.Kind.DOCTRINE_FIT,
            doctrine_fit=fit,
            status__in=(
                StoreOrder.Status.OPEN, StoreOrder.Status.CLAIMED,
                StoreOrder.Status.IN_PRODUCTION,
            ),
        )
        .filter(Q(delivery_location=location) | Q(delivery_location__isnull=True))
        .order_by("created_at")
    )
    allocations: list[AllocationResult] = []
    for order in waiting:
        if total_free <= 0:
            break
        need = min(unfilled_quantity(order), total_free)
        if need <= 0:
            continue
        granted = 0
        for row in rows:
            if need <= 0:
                break
            take = min(free_by_row[row.pk], need)
            if take > 0:
                FitReservation.objects.create(order=order, stock=row, quantity=take)
                free_by_row[row.pk] -= take
                total_free -= take
                need -= take
                granted += take
        if granted:
            allocations.append(AllocationResult(order=order, quantity=granted))
    return allocations


@transaction.atomic
def receive_stock(fit, *, location, quantity: int, actor, reason: str = "",
                  order: StoreOrder | None = None,
                  policy: ShipyardPolicy | None = None) -> ReceiptResult:
    """Record newly assembled/imported complete ships at a location, then (per
    policy) allocate them to waiting backorders oldest-first."""
    if quantity <= 0:
        raise ValueError("receipt quantity must be positive")
    policy = policy or ShipyardPolicy.active()
    current = manifest_hash(fit)
    stock, _created = (
        FitStock.objects.select_for_update()
        .get_or_create(doctrine_fit=fit, location=location, manifest_hash=current)
    )
    stock.quantity_on_hand += quantity
    stock.save(update_fields=["quantity_on_hand", "updated_at"])
    _entry(stock, FitStockEntry.Kind.RECEIPT, quantity, actor=actor, order=order, reason=reason)

    allocations: list[AllocationResult] = []
    if policy.auto_allocate_receipts:
        allocations = allocate_backorders(fit, location=location, policy=policy)
    return ReceiptResult(stock=stock, allocations=allocations)


@transaction.atomic
def adjust_stock(stock: FitStock, *, corrected_balance: int, actor, reason: str,
                 kind: str = FitStockEntry.Kind.ADJUSTMENT) -> FitStockEntry | None:
    """Officer stocktake: set a row to a counted balance, with a mandatory reason.

    Refuses a correction below the row's actively reserved quantity — reserved
    ships are promised to buyers and must be released or consumed first, never
    adjusted away silently."""
    if corrected_balance < 0:
        raise ValueError("corrected balance cannot be negative")
    if not (reason or "").strip():
        raise ValueError("an adjustment reason is required")
    locked = FitStock.objects.select_for_update().get(pk=stock.pk)
    reserved = (
        FitReservation.objects.filter(
            stock=locked, status=FitReservation.Status.ACTIVE
        ).aggregate(s=Sum("quantity"))["s"] or 0
    )
    if corrected_balance < reserved:
        raise ValueError("reserved")
    delta = corrected_balance - locked.quantity_on_hand
    if delta == 0:
        return None
    locked.quantity_on_hand = corrected_balance
    locked.last_reconciled_at = timezone.now()
    locked.save(update_fields=["quantity_on_hand", "last_reconciled_at", "updated_at"])
    return _entry(locked, kind, delta, actor=actor, reason=reason)


@transaction.atomic
def revalidate_stock(stock: FitStock, *, actor, reason: str = "") -> int:
    """Confirm a stale row's UNRESERVED ships as matching the current fit revision.

    Moves the free units into the current-revision row (creating it if needed);
    units still reserved by pre-revision orders stay behind until consumed.
    Returns units moved."""
    fit = stock.doctrine_fit
    current = manifest_hash(fit)
    stale = FitStock.objects.select_for_update().get(pk=stock.pk)
    if stale.manifest_hash == current:
        return 0
    reserved = (
        FitReservation.objects.filter(
            stock=stale, status=FitReservation.Status.ACTIVE
        ).aggregate(s=Sum("quantity"))["s"] or 0
    )
    free = max(stale.quantity_on_hand - reserved, 0)
    if free <= 0:
        return 0
    target, _created = (
        FitStock.objects.select_for_update()
        .get_or_create(doctrine_fit=fit, location=stale.location, manifest_hash=current)
    )
    stale.quantity_on_hand -= free
    stale.save(update_fields=["quantity_on_hand", "updated_at"])
    _entry(stale, FitStockEntry.Kind.REVALIDATION, -free, actor=actor, reason=reason)
    target.quantity_on_hand += free
    target.last_reconciled_at = timezone.now()
    target.save(update_fields=["quantity_on_hand", "last_reconciled_at", "updated_at"])
    _entry(target, FitStockEntry.Kind.REVALIDATION, free, actor=actor, reason=reason)
    return free
