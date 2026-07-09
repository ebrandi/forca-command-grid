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
    """Total available (current minus active reservations) for a type.

    Two aggregate queries (not N+1): summing across the reservation join would
    fan-out and inflate the current total, so they are computed separately.
    """
    current = (
        StockpileItem.objects.filter(type_id=type_id, stockpile__kind=kind).aggregate(
            s=Sum("quantity_current")
        )["s"]
        or 0
    )
    reserved = (
        StockReservation.objects.filter(
            stockpile_item__type_id=type_id,
            stockpile_item__stockpile__kind=kind,
            status=StockReservation.Status.ACTIVE,
        ).aggregate(s=Sum("quantity_reserved"))["s"]
        or 0
    )
    return current - reserved


def available_quantities(type_ids, kind: str = Stockpile.Kind.CORP) -> dict[int, int]:
    """Batch of :func:`available_quantity`: available (current − active reservations)
    for every type in ``type_ids``, in exactly two aggregate queries total (one for
    stockpile current, one for active reservations) instead of two per type.

    Same separate-aggregate approach as :func:`available_quantity` — summing across
    the reservation join would fan-out and inflate the current total, so the two
    sums are grouped independently. For any given id the result is identical to
    calling :func:`available_quantity`; ids with no stock/reservations map to 0.
    """
    ids = {int(t) for t in type_ids}
    if not ids:
        return {}
    current = {
        row["type_id"]: row["s"]
        for row in (
            StockpileItem.objects.filter(type_id__in=ids, stockpile__kind=kind)
            .values("type_id")
            .annotate(s=Sum("quantity_current"))
        )
    }
    reserved = {
        row["stockpile_item__type_id"]: row["s"]
        for row in (
            StockReservation.objects.filter(
                stockpile_item__type_id__in=ids,
                stockpile_item__stockpile__kind=kind,
                status=StockReservation.Status.ACTIVE,
            )
            .values("stockpile_item__type_id")
            .annotate(s=Sum("quantity_reserved"))
        )
    }
    return {tid: (current.get(tid) or 0) - (reserved.get(tid) or 0) for tid in ids}


def record_manual_stock(
    stockpile: Stockpile, type_id: int, quantity_current: int, quantity_target: int | None = None
) -> StockpileItem:
    item, _ = StockpileItem.objects.update_or_create(
        stockpile=stockpile,
        type_id=type_id,
        defaults={
            "quantity_current": quantity_current,
            "quantity_target": quantity_target,
            "provenance": StockpileItem.Provenance.MANUAL,
            "source": Source.MANUAL,
        },
    )
    return item


@transaction.atomic
def reserve_for_project(project, type_id: int, quantity: int) -> int:
    """Reserve up to ``quantity`` of a type for a project, FIFO across items.

    Returns the quantity actually reserved (may be < requested if short).
    """
    remaining = quantity
    items = (
        StockpileItem.objects.select_for_update()
        .filter(type_id=type_id, stockpile__kind=Stockpile.Kind.CORP)
        .order_by("stockpile__as_of", "id")  # FIFO by stockpile age
    )
    for item in items:
        if remaining <= 0:
            break
        free = item.quantity_available
        if free <= 0:
            continue
        take = min(free, remaining)
        StockReservation.objects.create(
            stockpile_item=item, project=project, quantity_reserved=take
        )
        remaining -= take
    return quantity - remaining


def release_reservation(reservation: StockReservation) -> None:
    reservation.status = StockReservation.Status.RELEASED
    reservation.save(update_fields=["status"])


@transaction.atomic
def consume_reservation(reservation: StockReservation) -> None:
    """Consume a reservation: decrement the stockpile item's current quantity."""
    item = StockpileItem.objects.select_for_update().get(pk=reservation.stockpile_item_id)
    item.quantity_current = max(0, item.quantity_current - reservation.quantity_reserved)
    item.save(update_fields=["quantity_current"])
    reservation.status = StockReservation.Status.CONSUMED
    reservation.save(update_fields=["status"])


def _volume_for(type_id: int) -> float:
    sde = SdeType.objects.filter(type_id=type_id).first()
    return sde.volume if sde else 0.0


def generate_hauling_tasks(source_location, dest_location, kind: str = Stockpile.Kind.CORP) -> int:
    """Create hauling tasks to cover target shortfalls at the destination.

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
    """
    on_hand, covered = esi_on_hand_for(stockpile)
    rows = []
    for item in stockpile.items.all():
        esi = on_hand.get(item.type_id) if covered else None
        effective = esi if covered else item.quantity_current
        target = item.quantity_target or 0
        rows.append({
            "item": item,
            "type_id": item.type_id,
            "manual_current": item.quantity_current,
            "esi_on_hand": esi,
            "covered": covered,
            "effective": effective or 0,
            "target": target,
            "shortfall": max(0, target - (effective or 0)) if target else 0,
        })
    return {"covered": covered, "rows": rows}
