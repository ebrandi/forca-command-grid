"""Route facts for freight pricing — jumps and security band via ESI.

EVE's static data here has no stargate graph, so jump counts come from ESI's
public ``POST /route/{origin}/{destination}/`` endpoint (no token; the body-based
route, compatibility date ≥ 2025-09-30). Results are cached for a week: routes
only change when CCP adds/removes a gate. ``preference="Safer"`` prefers high-sec
— the successor to the old ``?flag=secure`` — matching how a careful hauler flies;
the worst security on that route decides whether the job is priced high-sec or
low/null. This is gate routing, used for Freighter / DST / Blockade Runner; Jump
Freighters don't use gates (see research/jump-freighter-routing.md).
"""
from __future__ import annotations

import logging

from django.core.cache import cache

from apps.sde.models import SdeSolarSystem
from core.esi.client import ESIClient, ESIError

log = logging.getLogger("forca.logistics")

_CACHE_TTL = 7 * 24 * 3600


def security_band(security: float) -> str:
    rounded = round(security, 1)
    if rounded >= 0.5:
        return "highsec"
    if rounded >= 0.1:
        return "lowsec"
    return "nullsec"


class RouteUnavailable(Exception):
    """ESI could not return a gate route (e.g. wormhole-only destination)."""


def route_facts(origin_system_id: int, dest_system_id: int, *, client: ESIClient | None = None) -> dict:
    """Resolve {jumps, lowsec_jumps, sec_band} for a gate route between systems.

    ``lowsec_jumps`` counts systems on the route with security < 0.5 (the input
    to jump-freighter pricing). Raises RouteUnavailable if ESI has no route.
    """
    if not origin_system_id or not dest_system_id:
        raise RouteUnavailable("Both systems are required.")
    if origin_system_id == dest_system_id:
        return {"jumps": 1, "lowsec_jumps": 0, "sec_band": _band_of(origin_system_id), "systems": [origin_system_id]}

    key = f"logi:route:{origin_system_id}:{dest_system_id}"
    cached = cache.get(key)
    if cached is None:
        cached = _fetch_route(origin_system_id, dest_system_id, client)
        cache.set(key, cached, _CACHE_TTL)
    return cached


def _fetch_route(origin: int, dest: int, client: ESIClient | None) -> dict:
    client = client or ESIClient()
    try:
        # Body-based POST /route: origin/destination in the path, the routing
        # preference in the body. "Safer" is the successor to the old flag=secure.
        resp = client.post(
            f"/route/{origin}/{dest}/", json={"preference": "Safer"}, essential=True
        )
    except ESIError as exc:
        log.warning("route lookup failed %s→%s: %s", origin, dest, exc)
        raise RouteUnavailable("No gate route between those systems.") from exc
    # New response shape is {"route": [system_id, …]}; tolerate a bare list too.
    data = resp.data or {}
    systems = data.get("route", []) if isinstance(data, dict) else list(data)
    if not systems or len(systems) < 2:
        raise RouteUnavailable("No gate route between those systems.")

    secs = dict(
        SdeSolarSystem.objects.filter(system_id__in=systems).values_list("system_id", "security")
    )
    bands = [security_band(secs.get(sid, 1.0)) for sid in systems]
    lowsec_jumps = sum(1 for b in bands if b != "highsec")
    worst = "nullsec" if "nullsec" in bands else ("lowsec" if "lowsec" in bands else "highsec")
    return {
        "jumps": len(systems) - 1,
        "lowsec_jumps": lowsec_jumps,
        "sec_band": worst,
        "systems": systems,
    }


def _band_of(system_id: int) -> str:
    sec = SdeSolarSystem.objects.filter(system_id=system_id).values_list("security", flat=True).first()
    return security_band(sec if sec is not None else 1.0)


# Route-planner preferences → ESI /route ``preference`` values.
ROUTE_PREFERENCES = {
    "safer": ("Safer", "Safer (prefer high-sec)"),
    "shortest": ("Shorter", "Shortest"),
    "insecure": ("LessSecure", "Less secure (prefer low/null)"),
}


def route_plan(origin_system_id: int, dest_system_id: int, preference: str = "safer",
               *, avoid: set[int] | None = None,
               connections: list[dict] | None = None) -> dict:
    """A detailed gate route: every system with its security band and region.

    ``preference`` is one of ``safer`` / ``shortest`` / ``insecure``. ``avoid`` is
    a set of solar-system ids to route around (ESI ``avoid_systems``);
    ``connections`` are extra one-way links like Ansiblex jump bridges, each an
    object ``{"from": id, "to": id}``. Raises RouteUnavailable when ESI has no
    gate route.
    """
    if not origin_system_id or not dest_system_id:
        raise RouteUnavailable("Both systems are required.")
    pref_key, _ = ROUTE_PREFERENCES.get(preference, ROUTE_PREFERENCES["safer"])
    if origin_system_id == dest_system_id:
        return {"jumps": 0, "preference": preference, "systems": [_system_detail(origin_system_id)]}

    avoid_list = sorted(avoid) if avoid else []
    conns = connections or []
    avoid_tag = "" if not avoid_list else str(hash(tuple(avoid_list)) & 0xFFFFFFFF)
    conn_tag = "" if not conns else str(
        hash(tuple(sorted((c["from"], c["to"]) for c in conns))) & 0xFFFFFFFF
    )
    key = f"logi:routeplan:{origin_system_id}:{dest_system_id}:{pref_key}:{avoid_tag}:{conn_tag}"
    cached = cache.get(key)
    if cached is None:
        cached = _fetch_route_detail(
            origin_system_id, dest_system_id, pref_key, preference, avoid_list, conns
        )
        cache.set(key, cached, _CACHE_TTL)
    return cached


def route_plan_multi(system_ids: list[int], preference: str = "safer",
                     *, avoid: set[int] | None = None,
                     connections: list[dict] | None = None) -> dict:
    """A gate route through an ordered list of systems (origin, waypoints…, dest).

    Each consecutive leg is planned with :func:`route_plan` and stitched into one
    system list (the shared boundary system isn't repeated). Raises
    RouteUnavailable if any leg has no route.
    """
    points = [s for s in system_ids if s]
    if len(points) < 2:
        raise RouteUnavailable("Need at least an origin and a destination.")
    systems: list[dict] = []
    total_jumps = 0
    for a, b in zip(points, points[1:], strict=False):
        leg = route_plan(a, b, preference, avoid=avoid, connections=connections)
        total_jumps += leg["jumps"]
        leg_systems = leg["systems"]
        if systems and leg_systems and systems[-1]["system_id"] == leg_systems[0]["system_id"]:
            systems.extend(leg_systems[1:])
        else:
            systems.extend(leg_systems)
    return {"jumps": total_jumps, "preference": preference, "systems": systems}


def _fetch_route_detail(origin: int, dest: int, pref_key: str, preference: str,
                        avoid_list: list[int], connections: list[dict] | None = None) -> dict:
    from apps.sde.models import SdeRegion

    client = ESIClient()
    body: dict = {"preference": pref_key}
    if avoid_list:
        body["avoid_systems"] = avoid_list
    if connections:
        body["connections"] = connections
    try:
        resp = client.post(f"/route/{origin}/{dest}/", json=body, essential=True)
    except ESIError as exc:
        log.warning("route plan failed %s→%s: %s", origin, dest, exc)
        raise RouteUnavailable("No gate route between those systems.") from exc
    data = resp.data or {}
    systems = data.get("route", []) if isinstance(data, dict) else list(data)
    if not systems or len(systems) < 2:
        raise RouteUnavailable("No gate route between those systems.")

    rows = {
        sid: (name, sec, rid)
        for sid, name, sec, rid in SdeSolarSystem.objects.filter(system_id__in=systems)
        .values_list("system_id", "name", "security", "region_id")
    }
    region_ids = {r[2] for r in rows.values() if r[2]}
    region_names = dict(SdeRegion.objects.filter(region_id__in=region_ids).values_list("region_id", "name"))
    detail = []
    for sid in systems:
        name, sec, rid = rows.get(sid, (f"System {sid}", 1.0, None))
        detail.append({
            "system_id": sid, "name": name, "security": round(sec, 1),
            "band": security_band(sec), "region": region_names.get(rid, ""),
        })
    return {"jumps": len(systems) - 1, "preference": preference, "systems": detail}


def _system_detail(system_id: int) -> dict:
    from apps.sde.models import SdeRegion

    row = (
        SdeSolarSystem.objects.filter(system_id=system_id)
        .values_list("name", "security", "region_id").first()
    )
    name, sec, rid = row or (f"System {system_id}", 1.0, None)
    region = SdeRegion.objects.filter(region_id=rid).values_list("name", flat=True).first() if rid else ""
    return {"system_id": system_id, "name": name, "security": round(sec, 1),
            "band": security_band(sec), "region": region or ""}


def jf_route_facts(origin_system_id: int, dest_system_id: int, range_ly: float) -> dict:
    """Cyno-jump facts for a Jump Freighter trip (proximity graph, not gates).

    Returns ``{jumps, ly, path, range_ly, sec_band}`` where ``jumps`` is the
    fewest cyno hops at ``range_ly``. Raises RouteUnavailable when no in-range
    path exists (e.g. a system with no coordinates, or an isolated endpoint) so
    the caller can fall back to a manual jump count.
    """
    from .jumps import jump_route

    if not origin_system_id or not dest_system_id:
        raise RouteUnavailable("Both systems are required.")
    route = jump_route(origin_system_id, dest_system_id, range_ly)
    if route is None:
        raise RouteUnavailable("No jump route within range between those systems.")
    # JF trips run through low/null; the trip is priced as a low/null job.
    band = "highsec" if route["jumps"] == 0 else "nullsec"
    return {
        "jumps": route["jumps"],
        "ly": route["ly"],
        "path": route["path"],
        "range_ly": range_ly,
        "sec_band": band,
    }
