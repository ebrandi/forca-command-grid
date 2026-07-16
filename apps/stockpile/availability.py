"""THE per-type availability authority (P1 — one truthful definition).

Every "how many of type X does the corp have free?" read goes through
:func:`available` / :func:`available_detail`. Never compute per-type availability
anywhere else — the pre-P1 codebase had five competing definitions that disagreed
(manual-only, double-counting manual+ESI, missing owner scoping); this module
retires all of them. (The Shipyard's *fitted-ship bundle* availability in
``apps.store.availability`` is a different domain and stays separate.)

The rules, in one place:

1. **Source choice is per location, ESI-wins** (generalising ``reconcile_stockpile``):
   a stockpile whose resolved asset-location ids hold ≥1 corp ESI asset row is
   *covered* — the ESI mirror is the on-hand truth there and its manual count is
   ignored at read time (it remains a planning record). Uncovered (wormhole, no
   corp token, location-less stockpile) ⇒ the manual count is the truth.
2. **ESI rows count once per asset location**, never once per stockpile — two
   stockpiles in one system must not double the ESI stock, so assets are
   aggregated directly and stockpile coverage only decides whether each *manual*
   row participates.
3. **Home corp only**: ESI assets filter ``owner_type=CORPORATION,
   owner_id=FORCA_HOME_CORP_ID``. A foreign corp's imported rows never count.
4. **ESI stock at locations with no stockpile still counts** for corp-wide reads
   (it is real corp property); ``location=L`` restricts to L's resolved ids.
5. **Reservations always subtract**, whichever source won — an ACTIVE
   ``StockReservation`` is a claim on corp stock, not on a data source.
6. **Floor at zero**: over-reservation (e.g. an ESI sync shrank a hangar) is
   surfaced as ``over_reserved`` in the detail, never as negative availability.
7. **Incoming supply never counts** (build jobs, hauls, projects) — the same
   principle the Shipyard enforces.

ESI-wins applies only to ``kind=CORP`` (the mirror is corp property); other kinds
are pure manual − reservations.

Known edge: a corp ``Asset`` row whose ``AssetLocation`` was deleted (NULL
location) still counts corp-wide — it is real property — but can't mark any
stockpile covered, so a manual stocktake of the same items at that spot would
also count until the mirror re-resolves the location. The asset importer always
records locations (even unreadable structures, by id), so this only occurs
transiently after a location row is manually removed.
"""
from __future__ import annotations

from django.conf import settings
from django.db.models import Sum

from .models import Asset, AssetLocation, Stockpile, StockpileItem, StockReservation


def _location_ids_by_system(system_ids) -> dict[int, set[int]]:
    """``system_id -> resolved ESI asset-location ids`` in one query for all systems."""
    out: dict[int, set[int]] = {}
    if not system_ids:
        return out
    rows = AssetLocation.objects.filter(system_id__in=list(system_ids)).values_list(
        "location_id", "system_id"
    )
    for loc_id, sys_id in rows:
        out.setdefault(sys_id, set()).add(loc_id)
    return out


def _resolved_location_ids(structure_id, system_id, by_system: dict[int, set[int]]) -> set[int]:
    """A market location's asset-location ids: the structure itself and/or every
    resolved asset location in the same solar system (mirrors ``_asset_location_ids_for``)."""
    ids: set[int] = set()
    if structure_id:
        ids.add(structure_id)
    if system_id:
        ids |= by_system.get(system_id, set())
    return ids


def available_detail(type_ids, *, location=None, kind=Stockpile.Kind.CORP) -> dict[int, dict]:
    """Per-type availability breakdown for UIs — the same numbers as :func:`available`,
    never a second definition.

    Returns ``{type_id: {esi, manual, reserved, effective, available, over_reserved,
    sources}}`` for every requested id (zeros when unknown). ``sources`` lists each
    manual stockpile row with its ``covered`` verdict so officers can see which
    counts participate. ``location=None`` is corp-wide; a ``MarketLocation``
    restricts assets, manual rows and reservations consistently.
    """
    ids = {int(t) for t in type_ids}
    if not ids:
        return {}
    corp_scope = kind == Stockpile.Kind.CORP

    # Query 1 — manual rows per (type, stockpile) with the location facts needed
    # for the coverage verdict (no per-stockpile follow-up queries).
    manual_qs = StockpileItem.objects.filter(type_id__in=ids, stockpile__kind=kind)
    if location is not None:
        manual_qs = manual_qs.filter(stockpile__location=location)
    manual_rows = list(
        manual_qs.values(
            "type_id",
            "stockpile_id",
            "stockpile__name",
            "stockpile__location__structure_id",
            "stockpile__location__system_id",
        ).annotate(q=Sum("quantity_current"))
    )

    esi: dict[int, int] = {}
    covered_by_stockpile: dict[int, bool] = {}
    if corp_scope:
        # Query 2 — resolve every relevant solar system's asset locations at once.
        system_ids = {
            r["stockpile__location__system_id"]
            for r in manual_rows
            if r["stockpile__location__system_id"]
        }
        if location is not None and location.system_id:
            system_ids.add(location.system_id)
        by_system = _location_ids_by_system(system_ids)

        # Query 3 — the coverage probe: where does the corp mirror hold anything?
        # Only needed when manual rows exist (coverage only gates manual counts).
        if manual_rows:
            corp_asset_locs = set(
                Asset.objects.filter(
                    owner_type=Asset.Owner.CORPORATION,
                    owner_id=settings.FORCA_HOME_CORP_ID,
                    location_id__isnull=False,
                )
                .values_list("location_id", flat=True)
                .distinct()
            )
            for r in manual_rows:
                sp_id = r["stockpile_id"]
                if sp_id in covered_by_stockpile:
                    continue
                resolved = _resolved_location_ids(
                    r["stockpile__location__structure_id"],
                    r["stockpile__location__system_id"],
                    by_system,
                )
                covered_by_stockpile[sp_id] = bool(resolved & corp_asset_locs)

        # Query 4 — ESI on-hand per type, counted once per asset location (rule 2);
        # includes locations with no stockpile at all (rule 4), home corp only (rule 3).
        asset_qs = Asset.objects.filter(
            owner_type=Asset.Owner.CORPORATION,
            owner_id=settings.FORCA_HOME_CORP_ID,
            type_id__in=ids,
        )
        if location is not None:
            asset_qs = asset_qs.filter(
                location_id__in=_resolved_location_ids(
                    location.structure_id, location.system_id, by_system
                )
            )
        esi = {
            row["type_id"]: int(row["q"] or 0)
            for row in asset_qs.values("type_id").annotate(q=Sum("quantity"))
        }

    manual: dict[int, int] = {}
    sources: dict[int, list[dict]] = {}
    for r in manual_rows:
        covered = corp_scope and covered_by_stockpile.get(r["stockpile_id"], False)
        qty = int(r["q"] or 0)
        if not covered:
            manual[r["type_id"]] = manual.get(r["type_id"], 0) + qty
        sources.setdefault(r["type_id"], []).append(
            {
                "stockpile_id": r["stockpile_id"],
                "name": r["stockpile__name"],
                "quantity": qty,
                "covered": covered,
            }
        )

    # Query 5 — ACTIVE reservations always subtract (rule 5), scoped like the stock.
    res_qs = StockReservation.objects.filter(
        stockpile_item__type_id__in=ids,
        stockpile_item__stockpile__kind=kind,
        status=StockReservation.Status.ACTIVE,
    )
    if location is not None:
        res_qs = res_qs.filter(stockpile_item__stockpile__location=location)
    reserved = {
        row["stockpile_item__type_id"]: int(row["s"] or 0)
        for row in res_qs.values("stockpile_item__type_id").annotate(
            s=Sum("quantity_reserved")
        )
    }

    out: dict[int, dict] = {}
    for tid in ids:
        e = esi.get(tid, 0)
        m = manual.get(tid, 0)
        r = reserved.get(tid, 0)
        effective = e + m
        out[tid] = {
            "esi": e,
            "manual": m,
            "reserved": r,
            "effective": effective,
            "available": max(0, effective - r),
            "over_reserved": max(0, r - effective),
            "sources": sources.get(tid, []),
        }
    return out


def available(type_ids, *, location=None, kind=Stockpile.Kind.CORP) -> dict[int, int]:
    """THE per-type availability: effective on-hand − ACTIVE reservations, floored at 0.

    ``location=None`` is corp-wide; a ``MarketLocation`` restricts to that location.
    Incoming supply (build jobs, industry projects, hauling) NEVER counts. Every id
    in ``type_ids`` is present in the result (0 when unknown). ≤5 queries for any
    number of ids.
    """
    return {
        tid: d["available"]
        for tid, d in available_detail(type_ids, location=location, kind=kind).items()
    }


__all__ = ["available", "available_detail"]
