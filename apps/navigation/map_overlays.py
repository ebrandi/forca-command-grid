"""Live map-overlay data from public ESI (no token) + our own killboard.

Each ESI feed updates roughly hourly, so results are cached ~1h. Everything is
keyed by solar-system id. Nothing here is taken from a third-party map — only
CCP's public API and our own data.
"""
from __future__ import annotations

from django.core.cache import cache

from core.esi.client import ESIClient

_TTL = 3600


def _fetch(path: str, key: str) -> list:
    cached = cache.get(key)
    if cached is None:
        try:
            cached = ESIClient().get(path).data or []
        except Exception:  # noqa: BLE001 - best-effort live feed; never break the page
            cached = []
        cache.set(key, cached, _TTL)
    return cached


def system_jumps() -> dict[int, int]:
    """Ship jumps through each system in the last hour (ESI)."""
    return {
        r["system_id"]: r.get("ship_jumps", 0)
        for r in _fetch("/universe/system_jumps/", "nav:map:jumps")
    }


def system_kills() -> dict[int, dict]:
    """Ship / pod / NPC kills per system in the last hour (ESI)."""
    return {r["system_id"]: r for r in _fetch("/universe/system_kills/", "nav:map:kills")}


def sovereignty() -> dict[int, dict]:
    """Sovereignty holder (alliance/corp/faction) per system (ESI Sovereignty Systems).

    The current ``/sovereignty/systems/`` route returns
    ``{"solar_systems": [{"solar_system_id", "claim": {"alliance"|"corporation"|
    "faction": {..._id}}}]}`` — flatten it to ``{system_id: {alliance_id, …}}``.
    """
    data = _fetch("/sovereignty/systems/", "nav:map:sov")
    rows = data.get("solar_systems", []) if isinstance(data, dict) else (data or [])
    out: dict[int, dict] = {}
    for r in rows:
        sid = r.get("solar_system_id")
        if not sid:
            continue
        claim = r.get("claim") or {}
        out[sid] = {
            "alliance_id": (claim.get("alliance") or {}).get("alliance_id"),
            "corporation_id": (claim.get("corporation") or {}).get("corporation_id"),
            "faction_id": (claim.get("faction") or {}).get("faction_id"),
        }
    return out


def system_topology(system_id: int) -> dict:
    """Static celestial layout of a system from public ESI ``/universe/systems/{id}/``.

    Planets/moons/belts/stations never change, so this is cached for 30 days — one
    ESI call the first time a system page is opened, then served from cache. Returns
    ``{planets, moons, belts, station_ids, constellation_id, name}`` (empty on error).
    """
    key = f"nav:sys:topo:{system_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    out = {"planets": 0, "moons": 0, "belts": 0, "station_ids": [],
           "constellation_id": None, "name": None}
    try:
        data = ESIClient().get(f"/universe/systems/{system_id}/").data or {}
    except Exception:  # noqa: BLE001 - best-effort topology fetch; degrade gracefully
        data = {}
    if data:
        planets = data.get("planets") or []
        out["planets"] = len(planets)
        out["moons"] = sum(len(p.get("moons") or []) for p in planets)
        out["belts"] = sum(len(p.get("asteroid_belts") or []) for p in planets)
        out["station_ids"] = data.get("stations") or []
        out["constellation_id"] = data.get("constellation_id")
        out["name"] = data.get("name")
        cache.set(key, out, 30 * 24 * 3600)
    return out


def faction_warfare() -> dict[int, dict]:
    """Faction-warfare state per system (ESI ``/fw/systems/``).

    Returns ``{system_id: {owner, occupier, contested, vp, threshold}}``. ``contested``
    is one of uncontested / contested / vulnerable / captured.
    """
    out: dict[int, dict] = {}
    for r in _fetch("/fw/systems/", "nav:map:fw") or []:
        sid = r.get("solar_system_id")
        if not sid:
            continue
        out[sid] = {
            "owner": r.get("owner_faction_id"),
            "occupier": r.get("occupier_faction_id"),
            "contested": r.get("contested") or "uncontested",
            "vp": r.get("victory_points", 0) or 0,
            "threshold": r.get("victory_points_threshold", 0) or 0,
        }
    return out


def corp_system_kills(system_ids: list[int]) -> dict[int, int]:
    """Our corp's PvP killmail count per system (kills + losses, our own data)."""
    from django.db.models import Count

    from apps.killboard.models import Killmail

    return dict(
        Killmail.objects.filter(
            involves_home_corp=True, is_npc=False, solar_system_id__in=system_ids
        )
        .values_list("solar_system_id")
        .annotate(n=Count("killmail_id"))
    )
