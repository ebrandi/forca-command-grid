"""Jump-freighter routing — fewest cyno jumps over a proximity graph.

A jump freighter does not fly stargates on the main leg of a trip: it jumps
directly from a cyno in one system to a cyno in another, anywhere within its
range. So the "jump count" is a shortest-path over a *proximity graph* (systems
within jump range of each other), not the gate graph ESI ``/route`` returns.

We build that graph from the galactic coordinates the SDE carries on every
system (``SdeSolarSystem.x/y/z``, metres). Nodes are cyno-capable systems
(security < 0.5, known space — high-sec can't host a cyno and a JF can't jump
*into* high-sec; wormhole/abyssal space can't be targeted). Edges connect any two
systems within the chosen range. BFS gives the fewest hops.

See research/jump-freighter-routing.md (Option A). Pure computation: one SDE read
to build the graph (memoised per range), then in-memory BFS per query.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque

from apps.sde.models import SdeSolarSystem

# The ship capability catalogue is the single source of truth in ``ships.py``;
# re-exported here so existing callers (freight pricing, store forecast, the
# navigation views and tests) keep importing ``JUMP_SHIPS`` / ``SHIPS_BY_KEY``
# from ``apps.logistics.jumps`` unchanged.
from .ships import (  # noqa: F401
    JUMP_SHIPS,
    SHIP_GROUPS,
    SHIP_PROFILES,
    SHIPS_BY_KEY,
    ShipProfile,
    jde_fuel_multiplier,
    profile_for,
)

# Metres per light-year — converts SDE coordinates to jump distances.
LIGHT_YEAR_M = 9.4607e15

# Base jump range is 5 ly; Jump Drive Calibration adds +20% per level, so range
# is 5·(1 + 0.20·JDC): 8 ly at III, 9 at IV, 10 at V.
_JF_BASE_RANGE_LY = 5.0


def effective_range(base_ly: float, jdc_level: int) -> float:
    """Jump range (ly) for a ship's base range at a Jump Drive Calibration level."""
    level = max(0, min(5, int(jdc_level)))
    return base_ly * (1.0 + 0.20 * level)


def jump_range_ly(jdc_level: int) -> float:
    """Maximum single-jump range (ly) for a jump freighter at a JDC level (0–5)."""
    return effective_range(_JF_BASE_RANGE_LY, jdc_level)


# Wormhole/abyssal regions start at 11000000. Pochven (Triglavian space) is in
# the normal range (10000070) but is jump-isolated like wormhole space — a cyno
# can't be lit there and a capital can't jump in or out (it's reached only via
# its own gates/filaments) — so it's excluded from the cyno graph too.
_KNOWN_SPACE_MAX_REGION = 11000000
POCHVEN_REGION_ID = 10000070


def _region_jumpable(region_id: int | None) -> bool:
    """Whether a cyno can be lit / a capital can jump to a system in this region."""
    return (
        region_id is not None
        and region_id < _KNOWN_SPACE_MAX_REGION
        and region_id != POCHVEN_REGION_ID
    )

# A cyno can only be lit in low-sec / null-sec. EVE rounds a system's true
# security to one decimal for display and treats >= 0.5 (rounded) as high-sec,
# i.e. any system with a *true* security of 0.45 or higher is high-sec (it shows
# as "0.5" even at 0.46) and cannot host a cyno or be jumped into. So cyno-
# capable means true security STRICTLY below 0.45 — not below 0.5.
CYNO_MAX_TRUESEC = 0.45


def is_cyno_capable(security: float) -> bool:
    """True if a cyno can be lit / a capital can jump into a system of this security."""
    return security is not None and security < CYNO_MAX_TRUESEC

# The largest jump range we will ever build a proximity graph for. Well above any
# real hull (a JF at JDC5 is ~10 ly), but a hard ceiling so a hostile/huge/inf
# "range" override can't collapse the grid bucketing into an O(n²) build + a
# complete graph (hundreds of MB) — a defence-in-depth backstop behind the view clamp.
MAX_JUMP_RANGE_LY = 50.0

# Module-level memo: range_ly (rounded) -> built graph. The graph only changes
# when CCP adds systems; a deploy restarts the process and clears it. Tests that
# load different system sets call clear_graph_cache(). Bounded in size so a caller
# cycling many distinct range values can't grow it without limit (OOM).
_GRAPH_CACHE_MAX = 32
_graph_cache: dict[float, dict] = {}


_station_cache: dict[str, set[int]] = {}


def clear_graph_cache() -> None:
    _graph_cache.clear()
    _station_cache.clear()


def _station_systems() -> set[int]:
    """Systems with a dockable NPC station (player structures aren't in the SDE)."""
    cached = _station_cache.get("v")
    if cached is None:
        from apps.sde.models import SdeStation

        cached = set(SdeStation.objects.values_list("system_id", flat=True))
        _station_cache["v"] = cached
    return cached


def _candidate_rows() -> list[tuple[int, float, float, float]]:
    """Cyno-capable systems with coordinates (true sec < 0.45, known space)."""
    return list(
        SdeSolarSystem.objects.filter(
            security__lt=CYNO_MAX_TRUESEC, region_id__lt=_KNOWN_SPACE_MAX_REGION
        )
        .exclude(region_id=POCHVEN_REGION_ID)
        .exclude(x=0.0, y=0.0, z=0.0)
        .values_list("system_id", "x", "y", "z")
    )


def _build_graph(range_ly: float) -> dict:
    """Adjacency over candidate systems: edge iff within ``range_ly``.

    Grid-bucketed so each system is only compared against systems in its own and
    neighbouring cells (cell size = range), keeping the build well under a second
    instead of O(n²) over ~5,000 systems.
    """
    coords = {sid: (x / LIGHT_YEAR_M, y / LIGHT_YEAR_M, z / LIGHT_YEAR_M)
              for sid, x, y, z in _candidate_rows()}
    cell = range_ly or 1.0
    buckets: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for sid, (x, y, z) in coords.items():
        buckets[(int(x // cell), int(y // cell), int(z // cell))].append(sid)

    adj: dict[int, set[int]] = defaultdict(set)
    r2 = range_ly * range_ly
    offsets = [(dx, dy, dz) for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)]
    for (cx, cy, cz), members in buckets.items():
        nearby: list[int] = []
        for dx, dy, dz in offsets:
            nearby.extend(buckets.get((cx + dx, cy + dy, cz + dz), ()))
        for a in members:
            ax, ay, az = coords[a]
            for b in nearby:
                if a == b:
                    continue
                bx, by, bz = coords[b]
                if (ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2 <= r2:
                    adj[a].add(b)
    return {"adj": dict(adj), "coords": coords, "count": len(coords)}


def _get_graph(range_ly: float, *, use_cache: bool = True) -> dict:
    # Bound the build cost + cache size regardless of caller (defence-in-depth behind
    # the view's range clamp): a non-finite or absurd range can't O(n²)-collapse the
    # grid or mint unbounded cache entries.
    if not math.isfinite(range_ly):
        range_ly = MAX_JUMP_RANGE_LY
    range_ly = min(max(range_ly, 0.1), MAX_JUMP_RANGE_LY)
    key = round(range_ly, 3)
    if use_cache and key in _graph_cache:
        return _graph_cache[key]
    graph = _build_graph(range_ly)
    if use_cache:
        if len(_graph_cache) >= _GRAPH_CACHE_MAX:
            _graph_cache.pop(next(iter(_graph_cache)))  # evict oldest-inserted (FIFO)
        _graph_cache[key] = graph
    return graph


def _coords_ly(system_id: int) -> tuple[float, float, float] | None:
    row = (
        SdeSolarSystem.objects.filter(system_id=system_id)
        .values_list("x", "y", "z")
        .first()
    )
    if not row or row == (0.0, 0.0, 0.0):
        return None
    return (row[0] / LIGHT_YEAR_M, row[1] / LIGHT_YEAR_M, row[2] / LIGHT_YEAR_M)


def jump_route(origin_system_id: int, dest_system_id: int, range_ly: float,
               *, use_cache: bool = True, allow_highsec_endpoints: bool = True,
               avoid: set[int] | None = None, require_stations: bool = False) -> dict | None:
    """Fewest cyno jumps from origin to dest at ``range_ly``.

    Returns ``{"jumps": hops, "path": [system_id, …], "ly": total}`` or ``None``
    if either endpoint has no coordinates or no in-range path exists. Intermediate
    hops are always cyno-capable (the graph excludes high-sec). With
    ``allow_highsec_endpoints`` (the default) a high-sec origin/destination is
    accepted as a staging point — a jump freighter reaches it on gates. Set it
    False for true capitals, which can't jump into (or cyno in) high-sec at all:
    a high-sec endpoint then yields no route.
    """
    if not origin_system_id or not dest_system_id:
        return None
    if origin_system_id == dest_system_id:
        return {"jumps": 0, "path": [origin_system_id], "ly": 0.0}

    # Endpoints in jump-isolated space (Pochven / wormhole / abyssal) can never be
    # cyno endpoints, for any ship.
    endpoint_regions = dict(
        SdeSolarSystem.objects.filter(
            system_id__in=(origin_system_id, dest_system_id)
        ).values_list("system_id", "region_id")
    )
    if not all(_region_jumpable(endpoint_regions.get(s)) for s in (origin_system_id, dest_system_id)):
        return None

    if not allow_highsec_endpoints and SdeSolarSystem.objects.filter(
        system_id__in=(origin_system_id, dest_system_id), security__gte=CYNO_MAX_TRUESEC
    ).exists():
        return None

    graph = _get_graph(range_ly, use_cache=use_cache)
    coords = dict(graph["coords"])
    for sid in (origin_system_id, dest_system_id):
        if sid not in coords:
            c = _coords_ly(sid)
            if c is None:
                return None
            coords[sid] = c

    # Attach the (possibly high-sec) endpoints to the graph on the fly.
    r2 = range_ly * range_ly
    extra: dict[int, set[int]] = defaultdict(set)
    for endpoint in (origin_system_id, dest_system_id):
        ex, ey, ez = coords[endpoint]
        for sid, (sx, sy, sz) in coords.items():
            if sid == endpoint:
                continue
            if (ex - sx) ** 2 + (ey - sy) ** 2 + (ez - sz) ** 2 <= r2:
                extra[endpoint].add(sid)
                extra[sid].add(endpoint)

    adj = graph["adj"]

    # Per-query node filtering (the cached graph is unfiltered): drop avoided
    # systems and, when only dockable stops are wanted, non-station systems —
    # but never the endpoints the user explicitly chose.
    blocked: set[int] = set(avoid or ())
    if require_stations:
        stations = _station_systems()
        blocked |= {sid for sid in coords if sid not in stations}
    blocked.discard(origin_system_id)
    blocked.discard(dest_system_id)

    def neighbours(node: int):
        base = adj.get(node, ())
        ex = extra.get(node)
        return base if not ex else set(base) | ex

    prev: dict[int, int | None] = {origin_system_id: None}
    queue: deque[int] = deque([origin_system_id])
    while queue:
        node = queue.popleft()
        if node == dest_system_id:
            break
        for nxt in neighbours(node):
            if nxt not in prev and nxt not in blocked:
                prev[nxt] = node
                queue.append(nxt)
    if dest_system_id not in prev:
        return None

    path: list[int] = []
    node: int | None = dest_system_id
    while node is not None:
        path.append(node)
        node = prev[node]
    path.reverse()

    total = 0.0
    for a, b in zip(path, path[1:], strict=False):
        ax, ay, az = coords[a]
        bx, by, bz = coords[b]
        total += math.sqrt((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2)
    return {"jumps": len(path) - 1, "path": path, "ly": round(total, 2)}


def systems_in_range(origin_system_id: int, range_ly: float, *,
                     avoid: set[int] | None = None, require_stations: bool = False,
                     limit: int = 300) -> list[dict] | None:
    """Every cyno-capable system within one jump (``range_ly``) of the origin.

    Returns ``[{system_id, ly}, …]`` sorted by distance, or ``None`` if the origin
    has no coordinates. The origin itself, avoided systems, and (when
    ``require_stations``) non-station systems are excluded.
    """
    origin = _coords_ly(origin_system_id)
    if origin is None:
        return None
    graph = _get_graph(range_ly)
    ox, oy, oz = origin
    r2 = range_ly * range_ly
    blocked = set(avoid or ())
    stations = _station_systems() if require_stations else None
    out: list[dict] = []
    for sid, (x, y, z) in graph["coords"].items():
        if sid == origin_system_id or sid in blocked:
            continue
        if stations is not None and sid not in stations:
            continue
        d2 = (ox - x) ** 2 + (oy - y) ** 2 + (oz - z) ** 2
        if d2 <= r2:
            out.append({"system_id": sid, "ly": round(math.sqrt(d2), 2)})
    out.sort(key=lambda r: r["ly"])
    return out[:limit]


def systems_within_jumps(origin_system_id: int, range_ly: float, max_jumps: int, *,
                         avoid: set[int] | None = None, require_stations: bool = False,
                         limit: int = 500) -> list[dict] | None:
    """Every cyno-capable system reachable within ``max_jumps`` cyno jumps of the origin.

    A multi-hop generalisation of :func:`systems_in_range` (a cyno-chain / reach map):
    hop 1 is every cyno system within one jump of the origin *point* (the origin may
    itself be high-sec — you jump FROM anywhere TO a lit cyno); each further hop expands
    over the cyno proximity graph (edges = within ``range_ly``). Returns
    ``[{system_id, jumps}, …]`` annotated with the FEWEST jumps to reach each, sorted by
    ``(jumps, system_id)``, or ``None`` if the origin has no coordinates.

    ``avoid`` blocks routing **through** a system (it's neither reached nor traversed —
    a hostile-staging system genuinely cuts the chain); ``require_stations`` filters the
    **result** only (you can still cyno-chain *through* a stationless system).
    """
    if max_jumps < 1:
        return []
    origin = _coords_ly(origin_system_id)
    if origin is None:
        return None
    graph = _get_graph(range_ly)
    adj = graph["adj"]
    coords = graph["coords"]
    blocked = set(avoid or ())
    ox, oy, oz = origin
    r2 = range_ly * range_ly

    hop_of: dict[int, int] = {}
    frontier: list[int] = []
    # hop 1 — direct reach from the origin point (origin need not be a graph node).
    for sid, (x, y, z) in coords.items():
        if sid == origin_system_id or sid in blocked:
            continue
        if (ox - x) ** 2 + (oy - y) ** 2 + (oz - z) ** 2 <= r2:
            hop_of[sid] = 1
            frontier.append(sid)
    # hops 2..max — BFS over the cyno graph; each system keeps its first (fewest) hop.
    for hop in range(2, max_jumps + 1):
        nxt: list[int] = []
        for sid in frontier:
            for nb in adj.get(sid, ()):
                if nb == origin_system_id or nb in blocked or nb in hop_of:
                    continue
                hop_of[nb] = hop
                nxt.append(nb)
        frontier = nxt
        if not frontier:
            break

    stations = _station_systems() if require_stations else None
    out = [
        {"system_id": sid, "jumps": h}
        for sid, h in hop_of.items()
        if stations is None or sid in stations
    ]
    out.sort(key=lambda r: (r["jumps"], r["system_id"]))
    return out[:limit]


def _fuel_multiplier(jfc: int, uses_jf_skill: bool, jf_skill: int, jde_rigs: int = 0) -> float:
    """Combined fuel-reduction factor. Jump Fuel Conservation cuts 10%/level for
    every hull; the Jump Freighters skill cuts a further 10%/level for JF hulls
    only; Jump Drive Economizer rigs (JF + Rorqual) add a stacking-penalised cut.
    All three are multiplicative. Jump Drive Calibration is deliberately absent —
    it affects range, not fuel."""
    mult = max(0.0, 1.0 - 0.10 * max(0, min(5, jfc)))
    if uses_jf_skill:
        mult *= max(0.0, 1.0 - 0.10 * max(0, min(5, jf_skill)))
    if jde_rigs:
        mult *= jde_fuel_multiplier(jde_rigs)
    return mult


def _simulate_jumps(path: list[int], fuel_per_ly: float, fatigue_factor: float,
                    fuel_mult: float) -> dict:
    """Per-hop distance/fuel/cooldown + totals over a concatenated jump path."""
    coords = {
        sid: (x, y, z)
        for sid, x, y, z in SdeSolarSystem.objects.filter(system_id__in=path)
        .values_list("system_id", "x", "y", "z")
    }
    hops: list[dict] = []
    total_ly = 0.0
    total_fuel = 0
    fatigue = 0.0       # minutes of jump fatigue, accumulating across the whole trip
    travel_min = 0.0    # waiting time between consecutive jumps
    n_hops = len(path) - 1
    for i, (a, b) in enumerate(zip(path, path[1:], strict=False)):
        ax, ay, az = coords[a]
        bx, by, bz = coords[b]
        d = math.sqrt((ax - bx) ** 2 + (ay - by) ** 2 + (az - bz) ** 2) / LIGHT_YEAR_M
        fuel = math.ceil(d * fuel_per_ly * fuel_mult)
        eff = d * fatigue_factor  # Black Ops / industrials fatigue on reduced distance
        cooldown = min(max(1.0 + eff, fatigue / 10.0), 30.0)
        fatigue = min(max(10.0 * (1.0 + eff), fatigue * (1.0 + eff)), 300.0)
        total_ly += d
        total_fuel += fuel
        if i < n_hops - 1:  # the cooldown after the final jump doesn't delay arrival
            travel_min += cooldown
        hops.append({"from": a, "to": b, "ly": round(d, 2), "fuel": fuel,
                     "cooldown_min": round(cooldown, 1)})
    return {
        "jumps": n_hops,
        "path": path,
        "hops": hops,
        "total_ly": round(total_ly, 2),
        "total_fuel": total_fuel,
        "final_fatigue_min": round(fatigue, 1),
        "travel_min": round(travel_min, 1),
    }


def jump_plan_multi(system_ids: list[int], *, range_ly: float,
                    fuel_per_ly: float = 0.0, fatigue_factor: float = 1.0,
                    uses_jf_skill: bool = False, jfc: int = 5, jf_skill: int = 5,
                    jde_rigs: int = 0,
                    allow_highsec_endpoints: bool = False,
                    avoid: set[int] | None = None, require_stations: bool = False) -> dict | None:
    """A jump plan through an ordered list of systems (origin, waypoints…, dest).

    Each consecutive leg is routed with :func:`jump_route`; the legs are stitched
    into one path and the fuel/fatigue progression is simulated over the whole
    trip (so fatigue carries across waypoints). Returns ``None`` if any leg has no
    route. With two systems this is an ordinary point-to-point plan.
    """
    points = [s for s in system_ids if s]
    if len(points) < 2:
        return None
    full_path: list[int] = []
    for a, b in zip(points, points[1:], strict=False):
        leg = jump_route(a, b, range_ly, allow_highsec_endpoints=allow_highsec_endpoints,
                         avoid=avoid, require_stations=require_stations)
        if leg is None:
            return None
        leg_path = leg["path"]
        if full_path and full_path[-1] == leg_path[0]:
            full_path.extend(leg_path[1:])
        else:
            full_path.extend(leg_path)
    fuel_mult = _fuel_multiplier(jfc, uses_jf_skill, jf_skill, jde_rigs)
    return _simulate_jumps(full_path, fuel_per_ly, fatigue_factor, fuel_mult)


def jump_plan(origin_system_id: int, dest_system_id: int, *, range_ly: float,
              fuel_per_ly: float = 0.0, fatigue_factor: float = 1.0,
              uses_jf_skill: bool = False, jfc: int = 5, jf_skill: int = 5, jde_rigs: int = 0,
              allow_highsec_endpoints: bool = False,
              avoid: set[int] | None = None, require_stations: bool = False) -> dict | None:
    """A point-to-point jump plan: per-hop distance, fuel and fatigue, plus totals."""
    return jump_plan_multi(
        [origin_system_id, dest_system_id], range_ly=range_ly, fuel_per_ly=fuel_per_ly,
        fatigue_factor=fatigue_factor, uses_jf_skill=uses_jf_skill, jfc=jfc, jf_skill=jf_skill,
        jde_rigs=jde_rigs, allow_highsec_endpoints=allow_highsec_endpoints, avoid=avoid,
        require_stations=require_stations,
    )
