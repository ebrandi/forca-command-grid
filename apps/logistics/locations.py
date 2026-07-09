"""Unified freight location search + trusted resolution.

A freight endpoint can be a player **structure** (pilot-private, via ESI), an NPC
**station** (public SDE), or a whole **system**. The picker searches all three;
on submit we re-derive stations/systems from the SDE so the client can't spoof a
name or system, and we trust only the bounded name + a real system id for a
pilot-private structure (which isn't in our DB).
"""
from __future__ import annotations

from apps.sde.models import SdeSolarSystem, SdeStation
from apps.sde.search import search_stations, search_systems

from .structures import search_structures

_MAX_NAME = 200


def search_locations(user, query: str, limit: int = 12) -> list[dict]:
    """Structures the pilot can dock at, then NPC stations, then systems.

    Each item: ``{kind, id, name, system_id, system_name}`` where ``kind`` is
    one of ``structure`` / ``station`` / ``system``.
    """
    query = (query or "").strip()
    if len(query) < 2:
        return []
    out: list[dict] = list(search_structures(user, query, limit=8))
    for st in search_stations(query, limit=limit):
        out.append({
            "kind": "station", "id": st["station_id"], "name": st["name"],
            "system_id": st["system_id"], "system_name": st["system_name"],
        })
    for sy in search_systems(query, limit=limit):
        out.append({
            "kind": "system", "id": sy["type_id"], "name": sy["name"],
            "system_id": sy["type_id"], "system_name": sy["name"],
        })
    return out


def resolve_location(kind: str, location_id, name: str, system_id) -> dict | None:
    """Normalise a submitted endpoint to a trusted ``{kind,id,name,system_id}``.

    Stations and systems are re-derived from the SDE by id (the client's name and
    system are ignored). A structure is pilot-private, so its name is trusted
    (bounded) but its system id must still be a real solar system. Falls back to
    an exact/prefix system-name lookup when nothing structured was submitted.
    Returns ``None`` if the endpoint can't be resolved to a real system.
    """
    kind = (kind or "").strip()
    name = (name or "").strip()[:_MAX_NAME]
    location_id = _to_int(location_id)
    system_id = _to_int(system_id)

    if kind == "station" and location_id:
        st = SdeStation.objects.filter(station_id=location_id).first()
        if st:
            return {"kind": "station", "id": st.station_id, "name": st.name, "system_id": st.system_id}
        return None

    if kind == "system" and (location_id or system_id):
        sy = SdeSolarSystem.objects.filter(system_id=(location_id or system_id)).first()
        if sy:
            return {"kind": "system", "id": sy.system_id, "name": sy.name, "system_id": sy.system_id}
        return None

    if kind == "structure" and name and system_id:
        if SdeSolarSystem.objects.filter(system_id=system_id).exists():
            return {"kind": "structure", "id": location_id, "name": name, "system_id": system_id}
        return None

    # Fallback: a free-typed system name (no structured selection).
    if name:
        sy = (
            SdeSolarSystem.objects.filter(name__iexact=name).first()
            or SdeSolarSystem.objects.filter(name__istartswith=name).order_by("name").first()
        )
        if sy:
            return {"kind": "system", "id": sy.system_id, "name": sy.name, "system_id": sy.system_id}
    return None


def _to_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
