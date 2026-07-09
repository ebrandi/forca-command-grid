"""Resolve EVE asset ``location_id``s to named, grouped locations.

ESI assets carry a ``location_id`` that may be a station, a solar system, an
Upwell structure, or another item (a container/ship the asset is nested in). To
group assets by *where they actually are*, we:

1. roll nested "item" locations up to their containing root, then
2. classify the root by id range / ``location_type`` and resolve a name —
   stations and systems from public data, structures best-effort (they need
   docking access we may lack, so they fall back to their id).
"""
from __future__ import annotations

from apps.sde.models import SdeSolarSystem
from core.esi.client import ESIClient, ESIError

from .models import AssetLocation

# EVE id ranges (CCP-stable) used to classify a root location.
_STATION_LO, _STATION_HI = 60_000_000, 64_000_000
_SYSTEM_LO, _SYSTEM_HI = 30_000_000, 32_000_000
_STRUCTURE_MIN = 1_000_000_000_000  # Upwell structures


def roll_up_to_root(assets: list[dict]) -> dict[int, tuple[int, str]]:
    """Map each asset ``item_id`` to its (root_location_id, root_location_type).

    Walks ``location_type == "item"`` chains up to the first non-item parent
    (the station/system/structure the whole nest sits in). Cycles and missing
    parents terminate the walk safely.
    """
    by_item = {a["item_id"]: a for a in assets if "item_id" in a}
    roots: dict[int, tuple[int, str]] = {}
    for asset in assets:
        loc_id = asset.get("location_id")
        loc_type = asset.get("location_type", "other")
        seen = set()
        # Follow nested containers/ships to the real location.
        while loc_type == "item" and loc_id in by_item and loc_id not in seen:
            seen.add(loc_id)
            parent = by_item[loc_id]
            loc_id = parent.get("location_id")
            loc_type = parent.get("location_type", "other")
        if "item_id" in asset:
            roots[asset["item_id"]] = (loc_id, loc_type)
    return roots


def _classify(location_id: int, location_type: str) -> str:
    if location_type == "solar_system":
        return AssetLocation.Kind.SOLAR_SYSTEM
    if location_type == "station":
        return AssetLocation.Kind.STATION
    if location_id is None:
        return AssetLocation.Kind.OTHER
    if _STATION_LO <= location_id < _STATION_HI:
        return AssetLocation.Kind.STATION
    if _SYSTEM_LO <= location_id < _SYSTEM_HI:
        return AssetLocation.Kind.SOLAR_SYSTEM
    if location_id >= _STRUCTURE_MIN:
        return AssetLocation.Kind.STRUCTURE
    return AssetLocation.Kind.OTHER


def resolve_location(
    location_id: int, location_type: str, client: ESIClient | None = None, token: str | None = None
) -> AssetLocation | None:
    """Get-or-create a named AssetLocation for a root location id (cached)."""
    if not location_id:
        return None
    existing = AssetLocation.objects.filter(location_id=location_id).first()
    if existing and existing.name:
        return existing

    kind = _classify(location_id, location_type)
    name, system_id, region_id = "", None, None

    if kind == AssetLocation.Kind.SOLAR_SYSTEM:
        sys = SdeSolarSystem.objects.filter(system_id=location_id).select_related("region").first()
        if sys:
            name, system_id, region_id = sys.name, sys.system_id, sys.region_id
    elif kind == AssetLocation.Kind.STATION:
        client = client or ESIClient()
        try:
            resp = client.get(f"/universe/stations/{location_id}/", essential=True)
            data = resp.data or {}
            name = data.get("name", "")
            system_id = data.get("system_id")
        except ESIError:
            name = ""
    elif kind == AssetLocation.Kind.STRUCTURE and token:
        client = client or ESIClient()
        try:
            resp = client.get(f"/universe/structures/{location_id}/", token=token)
            data = resp.data or {}
            name = data.get("name", "")
            system_id = data.get("solar_system_id")
        except ESIError:
            name = ""  # no docking access / scope -> fall back to id in __str__

    if system_id and region_id is None:
        sys = SdeSolarSystem.objects.filter(system_id=system_id).first()
        region_id = sys.region_id if sys else None

    location, _ = AssetLocation.objects.update_or_create(
        location_id=location_id,
        defaults={"name": name, "kind": kind, "system_id": system_id, "region_id": region_id},
    )
    return location
