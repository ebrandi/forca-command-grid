"""High-sec exit planner — find the low-sec system to jump to (or from) when a
Jump Freighter's trip touches high-sec.

A jump freighter can't light a cyno in high-sec, so it can't jump directly into
(or out of) a high-sec system. Instead it jumps to a nearby **low-sec exit** and
takes stargates the rest of the way. This module ranks those exits.

"Nearest" is measured primarily by **stargate route length** to the high-sec
system — not 3-D light-year distance — using the local gate graph
(``SdeSystemJump``, the same one the region maps use). A candidate is only
useful if it's also reachable by jump drive from the trip's jump anchor (the
origin for a high-sec destination; the destination for a high-sec origin), so we
verify that against the cyno proximity graph. Dockable (NPC-station) systems and
avoid-lists factor into the ranking.

The final, exact gate route from the chosen exit to the high-sec system is left
to the ESI route engine (``apps.logistics.routing.route_plan``) — this module
only discovers and ranks candidates.
"""
from __future__ import annotations

from collections import defaultdict, deque

from django.utils.translation import gettext as _

from apps.logistics.jumps import is_cyno_capable, jump_route
from apps.logistics.routing import security_band
from apps.sde.models import SdeRegion, SdeSolarSystem, SdeSystemJump

# BFS bounds for candidate discovery. High-sec pockets border low-sec within a
# handful of gates, so a modest radius/node cap keeps this local and fast.
_MAX_HOPS = 16
_NODE_CAP = 3000

_gate_adj_cache: dict[str, dict[int, set[int]]] = {}


def clear_gate_cache() -> None:
    _gate_adj_cache.clear()


def _gate_adjacency() -> dict[int, set[int]]:
    """Undirected stargate adjacency over all known-space systems (memoised).

    Only changes when CCP adds a gate; a deploy restarts the process and clears
    it. Tests that load a custom gate graph call :func:`clear_gate_cache`.
    """
    adj = _gate_adj_cache.get("v")
    if adj is None:
        built: dict[int, set[int]] = defaultdict(set)
        for a, b in SdeSystemJump.objects.values_list("from_system_id", "to_system_id"):
            built[a].add(b)
            built[b].add(a)
        adj = dict(built)
        _gate_adj_cache["v"] = adj
    return adj


def _gate_distances(start_id: int) -> dict[int, int]:
    """Gate-hop distance from ``start_id`` to nearby systems (bounded BFS)."""
    adj = _gate_adjacency()
    dist = {start_id: 0}
    queue: deque[int] = deque([start_id])
    while queue and len(dist) < _NODE_CAP:
        node = queue.popleft()
        d = dist[node]
        if d >= _MAX_HOPS:
            continue
        for nb in adj.get(node, ()):
            if nb not in dist:
                dist[nb] = d + 1
                queue.append(nb)
    return dist


def gate_distances(origin_system_id: int) -> dict[int, int]:
    """Public: ``{system_id: gate_hops}`` from ``origin`` over the local stargate graph.

    Bounded to ~16 hops (systems farther out are simply absent from the map). Memoised
    adjacency; call :func:`clear_gate_cache` after loading a custom graph in tests.
    """
    return _gate_distances(origin_system_id)


def nearest_lowsec(highsec_system_id: int, *, avoid: set[int] | None = None,
                   require_stations: bool = False, prefer_stations: bool = True,
                   limit: int = 24) -> list[dict]:
    """Cyno-capable (low/null) systems near a high-sec system, by gate distance.

    Pure gate-graph ranking with no jump-reachability check — the building block
    for :func:`rank_exits` (single high-sec endpoint) and for pairing an entry
    with an exit when *both* ends are high-sec.
    """
    avoid = avoid or set()
    dist = _gate_distances(highsec_system_id)
    cand_ids = [sid for sid in dist if sid != highsec_system_id and sid not in avoid]
    meta = {
        sid: (name, sec, rid)
        for sid, name, sec, rid in SdeSolarSystem.objects.filter(system_id__in=cand_ids)
        .values_list("system_id", "name", "security", "region_id")
    }
    stations = _station_systems()
    region_names = dict(
        SdeRegion.objects.filter(region_id__in={m[2] for m in meta.values() if m[2]})
        .values_list("region_id", "name")
    )
    prelim = []
    for sid in cand_ids:
        name, sec, rid = meta.get(sid, (None, None, None))
        if sec is None or not is_cyno_capable(sec):
            continue
        if require_stations and sid not in stations:
            continue
        prelim.append({
            "system_id": sid, "name": name, "security": round(sec, 1),
            "band": security_band(sec), "region_id": rid,
            "region": region_names.get(rid, ""),
            "gate_jumps": dist[sid], "has_station": sid in stations,
        })
    prelim.sort(key=lambda c: (c["gate_jumps"], not (prefer_stations and c["has_station"])))
    return prelim[:limit]


def rank_exits(highsec_system_id: int, jump_anchor_system_id: int, range_ly: float, *,
               avoid: set[int] | None = None, require_stations: bool = False,
               prefer_stations: bool = True, max_candidates: int = 6) -> list[dict]:
    """Ranked low-sec exits near a high-sec system.

    ``highsec_system_id`` is the high-sec endpoint we must reach on gates.
    ``jump_anchor_system_id`` is the other end of the jump legs (the origin for a
    high-sec destination; the destination for a high-sec origin) — a candidate is
    only viable if it's within jump range of it. Returns up to ``max_candidates``
    dicts, best first, each with the gate distance to the high-sec system, jump
    reachability, dockability, region and a ranking score. An empty list means no
    reachable exit was found.
    """
    avoid = avoid or set()
    prelim = nearest_lowsec(
        highsec_system_id, avoid=avoid, require_stations=require_stations,
        prefer_stations=prefer_stations, limit=max_candidates * 4,
    )
    out: list[dict] = []
    for cand in prelim:
        # Probe reachability under the SAME station constraint the plan builder will
        # apply, so we never advertise an exit the plan can't actually route to.
        route = jump_route(jump_anchor_system_id, cand["system_id"], range_ly, avoid=avoid,
                           require_stations=require_stations)
        if route is None:
            continue
        cand = dict(cand)
        cand["jump_jumps"] = route["jumps"]
        cand["jump_ly"] = route["ly"]
        cand["score"] = round(
            cand["gate_jumps"] + route["jumps"] + (0.0 if cand["has_station"] else 0.75), 2
        )
        warnings = []
        if not cand["has_station"]:
            warnings.append(_("No NPC station — no safe dock at the exit."))
        if cand["gate_jumps"] > 6:
            warnings.append(
                _("%(jumps)s gate jumps to the high-sec endpoint.")
                % {"jumps": cand["gate_jumps"]}
            )
        cand["warnings"] = warnings
        out.append(cand)
        if len(out) >= max_candidates:
            break
    out.sort(key=lambda c: (c["score"], c["gate_jumps"]))
    return out


def pair_entry_exit(origin_highsec_id: int, dest_highsec_id: int, range_ly: float, *,
                    avoid: set[int] | None = None, require_stations: bool = False,
                    prefer_stations: bool = True, k: int = 6) -> dict | None:
    """Best (low-sec entry near origin, low-sec exit near dest) pair joined by a
    jump, for a trip that is high-sec at both ends. Minimises the combined gate +
    jump length. ``None`` if no jump-connected pair exists within range."""
    avoid = avoid or set()
    entries = nearest_lowsec(origin_highsec_id, avoid=avoid, require_stations=require_stations,
                             prefer_stations=prefer_stations, limit=k)
    exits = nearest_lowsec(dest_highsec_id, avoid=avoid, require_stations=require_stations,
                           prefer_stations=prefer_stations, limit=k)
    best = None
    for entry in entries:
        for ex in exits:
            if entry["system_id"] == ex["system_id"]:
                jumps, ly = 0, 0.0
            else:
                route = jump_route(entry["system_id"], ex["system_id"], range_ly, avoid=avoid,
                                   require_stations=require_stations)
                if route is None:
                    continue
                jumps, ly = route["jumps"], route["ly"]
            score = entry["gate_jumps"] + ex["gate_jumps"] + jumps
            if best is None or score < best["score"]:
                best = {"entry": entry, "exit": ex, "jump_jumps": jumps,
                        "jump_ly": ly, "score": score}
    return best


def _station_systems() -> set[int]:
    # Reuse the jump engine's station lookup (memoised, cleared with its cache).
    from apps.logistics.jumps import _station_systems as jump_stations

    return jump_stations()
