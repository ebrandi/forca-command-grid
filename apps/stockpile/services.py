"""Stockpile services: availability, FIFO reservations, manual stocktake."""
from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.db.models import Sum

from apps.sde.models import SdeType
from core.mixins import Source

from .models import (
    Asset,
    AssetLocation,
    HaulingTask,
    Stockpile,
    StockpileItem,
    StockReservation,
)


def available_quantity(type_id: int, kind: str = Stockpile.Kind.CORP) -> int:
    """Available quantity of one type — thin wrapper over the availability
    authority in :mod:`apps.stockpile.availability` (kept: the name is honest and
    widely referenced). ESI-covered stock counts, reservations subtract, floored
    at 0 — see the module docstring there for the full rules."""
    from .availability import available

    return available([type_id], kind=kind)[type_id]


def available_quantities(type_ids, kind: str = Stockpile.Kind.CORP) -> dict[int, int]:
    """Batch of :func:`available_quantity` — delegates to the availability
    authority in :mod:`apps.stockpile.availability`. Ids with no stock map to 0."""
    from .availability import available

    return available(type_ids, kind=kind)


_TARGET_UNSET = object()


def record_manual_stock(
    stockpile: Stockpile, type_id: int, quantity_current: int, quantity_target=_TARGET_UNSET
) -> StockpileItem:
    """Record a manual stocktake count (and optionally a target) for one item.

    ``quantity_target`` left at its sentinel means "don't touch the target" — a
    count-only update must not silently wipe an existing target (the old
    last-writer-wins behaviour). Pass an explicit value (or ``None``) to set or
    clear it. Negative counts are rejected at the service edge, pairing with the
    DB CheckConstraint.
    """
    if quantity_current < 0:
        raise ValueError("negative stock")
    defaults = {
        "quantity_current": quantity_current,
        "provenance": StockpileItem.Provenance.MANUAL,
        "source": Source.MANUAL,
    }
    if quantity_target is not _TARGET_UNSET:
        defaults["quantity_target"] = quantity_target
    item, _ = StockpileItem.objects.update_or_create(
        stockpile=stockpile,
        type_id=type_id,
        defaults=defaults,
    )
    return item


def _active_reserved_by_item(items: list[StockpileItem]) -> dict[int, int]:
    """ACTIVE claim totals per item pk, in one aggregate (never the per-row property)."""
    if not items:
        return {}
    return {
        row["stockpile_item_id"]: int(row["s"] or 0)
        for row in (
            StockReservation.objects.filter(
                stockpile_item_id__in=[i.pk for i in items],
                status=StockReservation.Status.ACTIVE,
            )
            .values("stockpile_item_id")
            .annotate(s=Sum("quantity_reserved"))
        )
    }


def _reserve_from_items(
    project, items: list[StockpileItem], quantity: int, reserved_by_item: dict[int, int]
) -> int:
    """Allocate reservations FIFO across already-locked items of one type.

    ``items`` must be row-locked by the caller (acquired in ascending pk — the
    global lock order). FIFO priority (stockpile age, then id) is applied here in
    Python, *after* locking, so lock acquisition order never depends on mutable
    values.
    """
    remaining = quantity
    for item in sorted(items, key=lambda i: (i.stockpile.as_of, i.pk)):
        if remaining <= 0:
            break
        free = item.quantity_current - reserved_by_item.get(item.pk, 0)
        if free <= 0:
            continue
        take = min(free, remaining)
        StockReservation.objects.create(
            stockpile_item=item, project=project, quantity_reserved=take
        )
        reserved_by_item[item.pk] = reserved_by_item.get(item.pk, 0) + take
        remaining -= take
    return quantity - remaining


@transaction.atomic
def reserve_for_project(project, type_id: int, quantity: int) -> int:
    """Reserve up to ``quantity`` of a type for a project, FIFO across items.

    Returns the quantity actually reserved (may be < requested if short). Locks
    ``StockpileItem`` rows in ascending pk (the one lock order); FIFO is applied
    after locking. Capped at the availability authority's number — a stale manual
    count at an ESI-covered location can't mint claims beyond the truthful stock.
    Multi-type callers must use :func:`reserve_for_project_bulk` — never this in
    a per-type loop, which would acquire locks in caller order and deadlock
    against pk-ordered walkers.
    """
    from .availability import available

    items = list(
        StockpileItem.objects.select_for_update(of=("self",))
        .select_related("stockpile")
        .filter(type_id=type_id, stockpile__kind=Stockpile.Kind.CORP)
        .order_by("pk")
    )
    quantity = min(quantity, available([type_id])[type_id])
    return _reserve_from_items(project, items, quantity, _active_reserved_by_item(items))


@transaction.atomic
def reserve_for_project_bulk(project, needed: dict[int, int]) -> dict[int, int]:
    """Reserve several types for a project in ONE pk-ordered lock acquisition.

    Returns ``{type_id: newly reserved}``. All ``StockpileItem`` rows across every
    requested type are locked in a single ascending-pk statement, then allocated
    FIFO per type — per-type sequential locking would deadlock against any other
    pk-ordered multi-row walker (delivery consumption, a rival reserve).

    Both correctness reads happen UNDER the item locks (reservations are only
    written by lock holders, so they are stable):

    * demand is netted against the project's existing ACTIVE claims — concurrent
      double-POSTs serialize on the locks and the loser reserves nothing new;
    * each type is capped at the availability authority's number (effective
      on-hand − every ACTIVE claim), so a stale manual count at an ESI-covered
      location can't mint claims beyond the truthful stock.
    """
    from .availability import available

    wanted = {int(t): int(q) for t, q in needed.items() if int(q) > 0}
    if not wanted:
        return {}
    items = list(
        StockpileItem.objects.select_for_update(of=("self",))
        .select_related("stockpile")
        .filter(type_id__in=list(wanted), stockpile__kind=Stockpile.Kind.CORP)
        .order_by("pk")
    )
    held = {
        row["stockpile_item__type_id"]: int(row["s"] or 0)
        for row in (
            StockReservation.objects.filter(
                project=project,
                status=StockReservation.Status.ACTIVE,
                stockpile_item__stockpile__kind=Stockpile.Kind.CORP,
            )
            .values("stockpile_item__type_id")
            .annotate(s=Sum("quantity_reserved"))
        )
    }
    cap = available(list(wanted))
    reserved_by_item = _active_reserved_by_item(items)
    by_type: dict[int, list[StockpileItem]] = {}
    for item in items:
        by_type.setdefault(item.type_id, []).append(item)
    out: dict[int, int] = {}
    for tid, qty in wanted.items():
        net = min(qty - held.get(tid, 0), cap.get(tid, 0))
        out[tid] = (
            _reserve_from_items(project, by_type.get(tid, []), net, reserved_by_item)
            if net > 0
            else 0
        )
    return out


def release_reservation(reservation: StockReservation) -> None:
    """Release one reservation (status-guarded: only an ACTIVE row moves)."""
    StockReservation.objects.filter(
        pk=reservation.pk, status=StockReservation.Status.ACTIVE
    ).update(status=StockReservation.Status.RELEASED)
    reservation.refresh_from_db(fields=["status"])


@transaction.atomic
def consume_reservation(reservation: StockReservation) -> None:
    """Consume one reservation: decrement its stockpile item's current quantity.

    Status-guarded FIRST — a RELEASED/CONSUMED row is always a no-op, never
    applied twice and never raising. An ACTIVE claim exceeding the item's stock
    raises ``ValueError`` (fail loudly; the caller decides) instead of silently
    clamping — the raise rolls this atomic block back, so the row stays ACTIVE.
    Lock order: item row first (pk lock), then the reservation CAS.

    This is the single-row primitive (admin/tooling). The delivery path uses its
    own batched, split-aware consumption in ``erp.services._consume_materials``.
    """
    item = StockpileItem.objects.select_for_update().get(pk=reservation.stockpile_item_id)
    claimed = StockReservation.objects.filter(
        pk=reservation.pk, status=StockReservation.Status.ACTIVE
    ).update(status=StockReservation.Status.CONSUMED)
    if not claimed:
        return
    if item.quantity_current < reservation.quantity_reserved:
        raise ValueError("insufficient stock")
    item.quantity_current -= reservation.quantity_reserved
    item.save(update_fields=["quantity_current"])
    reservation.status = StockReservation.Status.CONSUMED


def _volume_for(type_id: int) -> float:
    sde = SdeType.objects.filter(type_id=type_id).first()
    return sde.volume if sde else 0.0


def generate_hauling_tasks(source_location, dest_location, kind: str = Stockpile.Kind.CORP) -> int:
    """Create hauling tasks to cover target shortfalls at the destination.

    .. deprecated::
        Superseded by the P6 freight pipeline (:mod:`apps.logistics.freight`), which
        consolidates purchase/import lines per lane, prices and caps the leg against
        the rate card, and receipts landed stock — this helper's stockpile-target
        source double-counts across routes. It has zero production callers; kept one
        release for safety, then removed. Use ``freight.add_requirement_to_batch`` /
        ``freight.open_batch_for_lane`` for new work.

    Deficits are summed per type across stockpiles. Idempotent per
    (type, source, dest): an existing OPEN task is updated in place rather than
    duplicated, and the lookup tolerates duplicates without raising.
    """
    deficits: dict[int, int] = {}
    for short in shortfalls_against_targets(kind):
        deficits[short["type_id"]] = deficits.get(short["type_id"], 0) + short["deficit"]

    created = 0
    for type_id, qty in deficits.items():
        volume = _volume_for(type_id) * qty
        # Any not-yet-done task for this route/item already covers the shortfall.
        existing = (
            HaulingTask.objects.filter(
                type_id=type_id,
                source_location=source_location,
                dest_location=dest_location,
            )
            .exclude(status=HaulingTask.Status.DONE)
            .first()
        )
        if existing:
            # Only refresh quantities while it's still open/unclaimed.
            if existing.status == HaulingTask.Status.OPEN:
                existing.quantity = qty
                existing.volume_m3 = volume
                existing.save(update_fields=["quantity", "volume_m3"])
        else:
            HaulingTask.objects.create(
                type_id=type_id,
                source_location=source_location,
                dest_location=dest_location,
                quantity=qty,
                volume_m3=volume,
                status=HaulingTask.Status.OPEN,
            )
            created += 1
    return created


def shortfalls_against_targets(kind: str = Stockpile.Kind.CORP) -> list[dict]:
    """Items below their target quantity (deficit = target - current)."""
    out: list[dict] = []
    for item in StockpileItem.objects.filter(
        stockpile__kind=kind, quantity_target__isnull=False
    ).select_related("stockpile"):
        deficit = (item.quantity_target or 0) - item.quantity_current
        if deficit > 0:
            out.append(
                {
                    "type_id": item.type_id,
                    "stockpile": item.stockpile.name,
                    "current": item.quantity_current,
                    "target": item.quantity_target,
                    "deficit": deficit,
                }
            )
    return out


# --- CORP-1 (2.14): reconcile manual stockpiles against the live ESI asset mirror --------
def _asset_location_ids_for(market_location) -> list[int]:
    """The ESI ``AssetLocation`` ids that correspond to a stockpile's ``MarketLocation``: the
    structure itself and/or every resolved asset location in the same solar system."""
    if market_location is None:
        return []
    ids: set[int] = set()
    if market_location.structure_id:
        ids.add(market_location.structure_id)
    if market_location.system_id:
        ids.update(
            AssetLocation.objects.filter(system_id=market_location.system_id)
            .values_list("location_id", flat=True)
        )
    return list(ids)


def esi_on_hand_for(stockpile: Stockpile) -> tuple[dict[int, int], bool]:
    """``({type_id: corp ESI quantity at the stockpile's location}, covered?)``.

    ``covered`` is True when the corp asset mirror actually holds data for this location —
    i.e. a corp token reads it. When False, ESI can't see the location (e.g. a wormhole with
    no corp token) and the manual stocktake is the only source of truth.
    """
    loc_ids = _asset_location_ids_for(stockpile.location)
    if not loc_ids:
        return {}, False
    # Home corp only (matches assets_view's scoping) — never sum a foreign corp's stock even
    # if the mirror ever holds one (shared structure / future friendly-corp import).
    on_hand = {
        row["type_id"]: row["q"]
        for row in (
            Asset.objects.filter(
                owner_type=Asset.Owner.CORPORATION,
                owner_id=settings.FORCA_HOME_CORP_ID,
                location_id__in=loc_ids,
            )
            .values("type_id")
            .annotate(q=Sum("quantity"))
        )
    }
    # Covered = the corp asset mirror actually holds rows here (any type group ⇒ ≥1 row);
    # derived from the aggregate rather than a separate exists() query.
    return on_hand, bool(on_hand)


def reconcile_stockpile(stockpile: Stockpile) -> dict:
    """Cross-check a stockpile's targets against live corp ESI on-hand at its location.

    Each row carries the manual current, the ESI on-hand (when the location is covered), the
    *effective* on-hand used for the shortfall (ESI when covered, else the manual count), and
    the resulting shortfall. When ``covered`` is False, only manual entry is meaningful.

    Rows also carry the ACTIVE reservation claims (``reserved``), the floored
    ``available`` (effective − reserved, the same rule as
    :mod:`apps.stockpile.availability`) and ``over_reserved`` — claims exceeding the
    effective on-hand, surfaced here so officers can see and fix them. Reservations
    are batched in one aggregate query (never per-row properties).
    """
    on_hand, covered = esi_on_hand_for(stockpile)
    reserved_by_item = {
        row["stockpile_item_id"]: int(row["s"] or 0)
        for row in (
            StockReservation.objects.filter(
                stockpile_item__stockpile=stockpile,
                status=StockReservation.Status.ACTIVE,
            )
            .values("stockpile_item_id")
            .annotate(s=Sum("quantity_reserved"))
        )
    }
    rows = []
    for item in stockpile.items.all():
        esi = on_hand.get(item.type_id) if covered else None
        effective = (esi if covered else item.quantity_current) or 0
        target = item.quantity_target or 0
        reserved = reserved_by_item.get(item.pk, 0)
        rows.append({
            "item": item,
            "type_id": item.type_id,
            "manual_current": item.quantity_current,
            "esi_on_hand": esi,
            "covered": covered,
            "effective": effective,
            "reserved": reserved,
            "available": max(0, effective - reserved),
            "over_reserved": max(0, reserved - effective),
            "target": target,
            "shortfall": max(0, target - effective) if target else 0,
        })
    return {"covered": covered, "rows": rows}
