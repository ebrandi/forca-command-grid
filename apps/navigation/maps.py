"""Build a 2D map model for a region from the SDE.

Self-hosted and original: positions are a top-down projection of the galactic
coordinates we load into ``SdeSolarSystem`` (drop the vertical ``y``, plot
``x``/``z``). EVE's galaxy is a near-flat disc, so this gives the familiar region
layout without copying anyone else's map data — nothing here comes from a
third-party map service.
"""
from __future__ import annotations

from apps.sde.models import SdeConstellation, SdeRegion, SdeSolarSystem, SdeSystemJump

VIEW = 1000.0   # SVG viewBox is 0..VIEW on both axes
PAD = 50.0      # px padding inside the viewBox

# Wormhole/abyssal regions start at 11000000; everything below is "known space"
# (high/low/null + Pochven). The universe map shows known space only — w-space has
# no fixed gate topology, so it can't be laid out as a static graph.
_KNOWN_SPACE_MAX_REGION = 11000000
POCHVEN_REGION_ID = 10000070


def security_band(security: float) -> str:
    rounded = round(security, 1)
    if rounded >= 0.5:
        return "highsec"
    if rounded >= 0.1:
        return "lowsec"
    return "nullsec"


# Security-graded node colours (high = blue/green … null = red), our own ramp.
_SEC_COLOURS = {
    10: "#3b7be0", 9: "#4a90d6", 8: "#5fb6cf", 7: "#69d6a6", 6: "#8fd96a",
    5: "#c4d942", 4: "#e0d23e", 3: "#e0a23e", 2: "#d97a2a", 1: "#d2502a", 0: "#c02929",
}


def security_colour(security: float) -> str:
    if security <= 0.0:
        return "#8c1c1c"
    return _SEC_COLOURS.get(max(0, min(10, round(security * 10))), "#c4d942")


def region_map(region_id: int, *, overlay: str = "security") -> dict | None:
    """Nodes (projected systems) + edges (intra-region stargate links) for a region.

    ``overlay`` re-colours the nodes and sets a per-node label (security, our
    killboard activity, or a live ESI feed — see ``apply_overlay``).
    """
    region = SdeRegion.objects.filter(region_id=region_id).first()
    if not region:
        return None

    systems = list(
        SdeSolarSystem.objects.filter(region_id=region_id)
        .exclude(x=0.0, y=0.0, z=0.0)
        .values("system_id", "name", "security", "x", "z", "constellation_id")
    )
    if not systems:
        return {"region": {"id": region.region_id, "name": region.name}, "nodes": [],
                "edges": [], "bridges": [], "view": VIEW, "constellations": [], "overlay": overlay}

    xs = [s["x"] for s in systems]
    zs = [s["z"] for s in systems]
    minx, maxx = min(xs), max(xs)
    minz, maxz = min(zs), max(zs)
    span = max(maxx - minx, maxz - minz) or 1.0
    scale = (VIEW - 2 * PAD) / span
    off_x = (VIEW - (maxx - minx) * scale) / 2
    off_z = (VIEW - (maxz - minz) * scale) / 2

    def project(x: float, z: float) -> tuple[float, float]:
        px = off_x + (x - minx) * scale
        py = off_z + (maxz - z) * scale  # flip z so higher z is "north" (up)
        return round(px, 1), round(py, 1)

    pos = {s["system_id"]: project(s["x"], s["z"]) for s in systems}
    sys_ids = set(pos)

    nodes = [
        {
            "id": s["system_id"], "name": s["name"],
            "security": round(s["security"], 1), "band": security_band(s["security"]),
            "colour": security_colour(s["security"]),
            "px": pos[s["system_id"]][0], "py": pos[s["system_id"]][1],
            "constellation_id": s["constellation_id"],
        }
        for s in systems
    ]

    edges = []
    seen: set[tuple[int, int]] = set()
    for a, b in SdeSystemJump.objects.filter(
        from_system_id__in=sys_ids, to_system_id__in=sys_ids
    ).values_list("from_system_id", "to_system_id"):
        key = (a, b) if a < b else (b, a)
        if key in seen:
            continue
        seen.add(key)
        (x1, y1), (x2, y2) = pos[a], pos[b]
        edges.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    # Gateways out of the region: systems with a stargate to another region. We label
    # each with the region(s) it leads to, so pilots can see the exits at a glance.
    out_links = list(
        SdeSystemJump.objects.filter(from_system_id__in=sys_ids)
        .exclude(to_system_id__in=sys_ids)
        .values_list("from_system_id", "to_system_id")
    )
    if out_links:
        ext_region = dict(
            SdeSolarSystem.objects.filter(system_id__in=[t for _, t in out_links])
            .values_list("system_id", "region_id")
        )
        rnames = dict(
            SdeRegion.objects.filter(region_id__in=set(ext_region.values()))
            .values_list("region_id", "name")
        )
        gateways: dict[int, set[str]] = {}
        for src, dst in out_links:
            name = rnames.get(ext_region.get(dst))
            if name:
                gateways.setdefault(src, set()).add(name)
        by_id = {n["id"]: n for n in nodes}
        for sid, regs in gateways.items():
            if sid in by_id:
                by_id[sid]["gateways"] = sorted(regs)

    # Ansiblex bridges whose both ends are in this region (drawn as special edges).
    from .models import AnsiblexBridge

    bridges = []
    for a, b in AnsiblexBridge.objects.filter(
        active=True, from_system_id__in=sys_ids, to_system_id__in=sys_ids
    ).values_list("from_system_id", "to_system_id"):
        if a in pos and b in pos:
            (x1, y1), (x2, y2) = pos[a], pos[b]
            bridges.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    apply_overlay(nodes, overlay)

    constellations = list(
        SdeConstellation.objects.filter(region_id=region_id)
        .order_by("name").values("constellation_id", "name")
    )
    return {
        "region": {"id": region.region_id, "name": region.name},
        "nodes": nodes, "edges": edges, "bridges": bridges, "view": VIEW,
        "constellations": constellations, "overlay": overlay,
    }


# --- Overlays ---------------------------------------------------------------
OVERLAYS = [
    ("security", "Security"),
    ("constellation", "Constellation"),
    ("kills", "Our activity"),
    ("traffic", "Traffic"),
    ("danger", "Ship kills"),
    ("ratting", "NPC kills"),
    ("sov", "Sovereignty"),
    ("fw", "Faction war"),
    ("incursions", "Incursions"),
]
_DIM = "#39435a"

# The four militia factions, with a stable colour each (for the FW overlay).
FW_FACTIONS = {
    500001: ("Caldari State", "#4a7ab5"),
    500002: ("Minmatar Republic", "#a0432b"),
    500003: ("Amarr Empire", "#c8a93b"),
    500004: ("Gallente Federation", "#3a8f6a"),
}
_FW_FRONT = "#f0533f"  # contested / vulnerable systems — the active front


def _lerp(a: tuple, b: tuple, t: float) -> str:
    r, g, bl = (round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return f"#{r:02x}{g:02x}{bl:02x}"


def heat_colour(value: float, mx: float) -> str:
    """Dim → gold → red ramp for an activity value against the region's max."""
    if mx <= 0 or value <= 0:
        return _DIM
    t = min(1.0, (value / mx) ** 0.6)
    if t < 0.5:
        return _lerp((57, 67, 90), (228, 210, 62), t / 0.5)
    return _lerp((228, 210, 62), (224, 53, 63), (t - 0.5) / 0.5)


def _hsl_to_hex(h: float, s: float, lum: float) -> str:
    s, lum = s / 100.0, lum / 100.0
    c = (1 - abs(2 * lum - 1)) * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = lum - c / 2
    r, g, b = [
        (c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)
    ][int(h // 60) % 6]
    rr, gg, bb = round((r + m) * 255), round((g + m) * 255), round((b + m) * 255)
    return f"#{rr:02x}{gg:02x}{bb:02x}"


def sov_colour(entity_id: int | None) -> str:
    """A distinct, stable colour per sovereignty holder (alliance/faction)."""
    if not entity_id:
        return _DIM
    return _hsl_to_hex((entity_id * 47) % 360, 60, 58)


def _heat(nodes: list, values: dict, suffix: str, radius: bool = True) -> None:
    mx = max(values.values(), default=0)
    for n in nodes:
        v = values.get(n["id"], 0)
        n["colour"] = heat_colour(v, mx)
        n["label"] = f"{v} {suffix}"
        if radius and mx:
            n["radius"] = round(5.0 + 5.0 * (v / mx) ** 0.5, 1)


def apply_overlay(nodes: list, overlay: str) -> None:
    """Re-colour nodes + set a per-node label for the active overlay (in place)."""
    ids = [n["id"] for n in nodes]
    if overlay == "kills":
        from .map_overlays import corp_system_kills
        _heat(nodes, corp_system_kills(ids), "kills")
    elif overlay == "traffic":
        from .map_overlays import system_jumps
        data = system_jumps()
        _heat(nodes, {i: data.get(i, 0) for i in ids}, "jumps/h")
    elif overlay == "danger":
        from .map_overlays import system_kills
        data = system_kills()
        _heat(nodes, {i: data.get(i, {}).get("ship_kills", 0) + data.get(i, {}).get("pod_kills", 0)
                      for i in ids}, "kills/h")
    elif overlay == "ratting":
        from .map_overlays import system_kills
        data = system_kills()
        _heat(nodes, {i: data.get(i, {}).get("npc_kills", 0) for i in ids}, "NPC/h")
    elif overlay == "sov":
        from .map_overlays import sovereignty
        sov = sovereignty()
        names = _sov_names(sov, ids)
        for n in nodes:
            s = sov.get(n["id"]) or {}
            holder = s.get("alliance_id") or s.get("corporation_id") or s.get("faction_id")
            n["colour"] = sov_colour(holder)
            n["label"] = names.get(holder, "Unclaimed") if holder else "Unclaimed"
    elif overlay == "constellation":
        cids = {n["constellation_id"] for n in nodes if n["constellation_id"]}
        cnames = dict(
            SdeConstellation.objects.filter(constellation_id__in=cids)
            .values_list("constellation_id", "name")
        )
        for n in nodes:
            cid = n["constellation_id"]
            n["colour"] = _hsl_to_hex((cid * 53) % 360, 52, 56) if cid else _DIM
            n["label"] = cnames.get(cid, "—")
    elif overlay == "fw":
        from .map_overlays import faction_warfare
        fw = faction_warfare()
        for n in nodes:
            f = fw.get(n["id"])
            if not f:
                n["colour"] = _DIM
                n["label"] = "—"
                continue
            name, colour = FW_FACTIONS.get(f["occupier"], ("Unaligned", "#888"))
            front = f["contested"] in ("contested", "vulnerable")
            n["colour"] = _FW_FRONT if front else colour
            pct = round(100 * f["vp"] / f["threshold"]) if f["threshold"] else 0
            n["label"] = (f"{name} · {f['contested']}"
                          + (f" {pct}%" if front else ""))
    elif overlay == "incursions":
        from .services import incursion_systems
        inc = incursion_systems()
        for n in nodes:
            on = n["id"] in inc
            n["colour"] = "#f0533f" if on else _DIM
            n["label"] = "Incursion" if on else "Clear"
    else:  # security (default) — node colours already set; add labels
        for n in nodes:
            n["label"] = f"sec {n['security']}"


def region_colour(region_id: int) -> str:
    """A distinct, stable colour per region (for the universe map)."""
    return _hsl_to_hex((region_id * 37) % 360, 45, 56)


_UNIVERSE_MAP_KEY = "nav:universe_map:v1"
_UNIVERSE_MAP_TTL = 21600  # 6h — the region topology only changes on a (rare) SDE re-import


def universe_map() -> dict:
    """Cached region-graph overview. The underlying topology is a full-SDE scan that is
    byte-identical until the next ``import_sde``, so it is computed at most once per TTL
    instead of on every ``/tools/maps/`` request. See :func:`_universe_map_uncached`."""
    from django.core.cache import cache

    cached = cache.get(_UNIVERSE_MAP_KEY)
    if cached is None:
        cached = _universe_map_uncached()
        cache.set(_UNIVERSE_MAP_KEY, cached, _UNIVERSE_MAP_TTL)
    return cached


def _universe_map_uncached() -> dict:
    """Known space as a readable region graph — one labelled node per region at its
    centroid, sized by system count and coloured by mean security, with edges where a
    stargate crosses from one region into another.

    Plotting all ~8,000 systems made an unreadable smear and showed w-space pockets
    that have no fixed gate topology. A region-level graph is the legible overview;
    the per-system detail lives on each region map.
    """
    systems = list(
        SdeSolarSystem.objects
        .filter(region_id__lt=_KNOWN_SPACE_MAX_REGION)
        .exclude(x=0.0, y=0.0, z=0.0)
        .values("system_id", "region_id", "security", "x", "z")
    )
    if not systems:
        return {"nodes": [], "edges": [], "view": VIEW}

    # Aggregate per region: centroid, system count, mean security.
    agg: dict[int, dict] = {}
    sys_region: dict[int, int] = {}
    for s in systems:
        rid = s["region_id"]
        sys_region[s["system_id"]] = rid
        a = agg.setdefault(rid, {"sx": 0.0, "sz": 0.0, "sec": 0.0, "n": 0})
        a["sx"] += s["x"]
        a["sz"] += s["z"]
        a["sec"] += s["security"]
        a["n"] += 1

    # Inter-region stargate links (both ends in known space) → region adjacency.
    region_edges: set[tuple[int, int]] = set()
    connected: set[int] = set()
    for f, t in SdeSystemJump.objects.values_list("from_system_id", "to_system_id"):
        ra, rb = sys_region.get(f), sys_region.get(t)
        if ra is None or rb is None or ra == rb:
            continue
        region_edges.add((ra, rb) if ra < rb else (rb, ra))
        connected.add(ra)
        connected.add(rb)

    # Keep regions wired into the gate network (drops isolated Jove pockets); keep
    # Pochven explicitly — it's reachable but its old stargate links were severed.
    keep = {rid for rid in agg if rid in connected or rid == POCHVEN_REGION_ID}
    if not keep:
        return {"nodes": [], "edges": [], "view": VIEW}

    cents = {
        rid: (a["sx"] / a["n"], a["sz"] / a["n"]) for rid, a in agg.items() if rid in keep
    }
    xs = [c[0] for c in cents.values()]
    zs = [c[1] for c in cents.values()]
    minx, maxx, minz, maxz = min(xs), max(xs), min(zs), max(zs)
    span = max(maxx - minx, maxz - minz) or 1.0
    scale = (VIEW - 2 * PAD) / span
    off_x = (VIEW - (maxx - minx) * scale) / 2
    off_z = (VIEW - (maxz - minz) * scale) / 2

    def project(x: float, z: float) -> tuple[float, float]:
        return (round(off_x + (x - minx) * scale, 1),
                round(off_z + (maxz - z) * scale, 1))

    pos = {rid: project(*c) for rid, c in cents.items()}
    region_names = dict(SdeRegion.objects.values_list("region_id", "name"))
    max_n = max(agg[rid]["n"] for rid in keep) or 1

    nodes = []
    for rid in keep:
        a = agg[rid]
        mean_sec = a["sec"] / a["n"]
        nodes.append({
            "region_id": rid, "name": region_names.get(rid, str(rid)),
            "px": pos[rid][0], "py": pos[rid][1],
            "r": round(8.0 + 16.0 * (a["n"] / max_n) ** 0.5, 1),
            "count": a["n"], "band": security_band(mean_sec),
            "colour": security_colour(mean_sec),
            "pochven": rid == POCHVEN_REGION_ID,
        })

    edges = [
        {"x1": pos[a][0], "y1": pos[a][1], "x2": pos[b][0], "y2": pos[b][1]}
        for a, b in region_edges if a in pos and b in pos
    ]
    return {"nodes": sorted(nodes, key=lambda n: n["name"]), "edges": edges, "view": VIEW}


def route_map(system_ids: list[int]) -> dict | None:
    """One combined map of a whole route, across every region it crosses.

    Projects the route systems (and their gate neighbours, for context) by galactic
    (x, z) into a single viewBox, returns the gate edges between shown systems, the
    ordered route as a polyline, and a label for each region the route passes through.
    ``None`` if none of the ids resolve to a system with coordinates.
    """
    # De-dupe while preserving the planned order.
    seen: set[int] = set()
    order: list[int] = []
    for sid in system_ids:
        if sid not in seen:
            seen.add(sid)
            order.append(sid)

    rows = {
        s["system_id"]: s
        for s in SdeSolarSystem.objects.filter(system_id__in=order)
        .exclude(x=0.0, y=0.0, z=0.0)
        .values("system_id", "name", "security", "region_id", "x", "z")
    }
    route = [rows[sid] for sid in order if sid in rows]
    if not route:
        return None
    route_ids = [r["system_id"] for r in route]
    route_set = set(route_ids)

    shown = {r["system_id"]: dict(r, route=True) for r in route}

    # Context neighbours (one gate hop off the route) make it read like a map slice
    # rather than a bare line. Skip for very long routes to keep it legible/cheap.
    if len(route) <= 60:
        nb_ids = {
            t for _, t in SdeSystemJump.objects.filter(from_system_id__in=route_ids)
            .values_list("from_system_id", "to_system_id")
            if t not in route_set
        }
        for s in (
            SdeSolarSystem.objects.filter(system_id__in=nb_ids)
            .exclude(x=0.0, y=0.0, z=0.0)
            .values("system_id", "name", "security", "region_id", "x", "z")
        ):
            shown.setdefault(s["system_id"], dict(s, route=False))

    xs = [s["x"] for s in shown.values()]
    zs = [s["z"] for s in shown.values()]
    minx, maxx, minz, maxz = min(xs), max(xs), min(zs), max(zs)
    span = max(maxx - minx, maxz - minz) or 1.0
    scale = (VIEW - 2 * PAD) / span
    off_x = (VIEW - (maxx - minx) * scale) / 2
    off_z = (VIEW - (maxz - minz) * scale) / 2

    def project(x: float, z: float) -> tuple[float, float]:
        return (round(off_x + (x - minx) * scale, 1),
                round(off_z + (maxz - z) * scale, 1))

    pos = {sid: project(s["x"], s["z"]) for sid, s in shown.items()}

    nodes = [
        {
            "id": sid, "name": s["name"], "security": round(s["security"], 1),
            "band": security_band(s["security"]), "colour": security_colour(s["security"]),
            "px": pos[sid][0], "py": pos[sid][1], "route": s["route"],
            "region_id": s["region_id"],
        }
        for sid, s in shown.items()
    ]

    # Gate edges between any two shown systems (faint structure under the route line).
    edges = []
    seen_e: set[tuple[int, int]] = set()
    shown_ids = set(shown)
    for a, b in SdeSystemJump.objects.filter(
        from_system_id__in=shown_ids, to_system_id__in=shown_ids
    ).values_list("from_system_id", "to_system_id"):
        key = (a, b) if a < b else (b, a)
        if key in seen_e:
            continue
        seen_e.add(key)
        (x1, y1), (x2, y2) = pos[a], pos[b]
        edges.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2})

    # The route itself, in order, as a single polyline ("x,y x,y …").
    points = " ".join(f"{pos[sid][0]},{pos[sid][1]}" for sid in route_ids)

    # One label per region, at the centroid of that region's shown systems.
    by_region: dict[int, list[int]] = {}
    for sid, s in shown.items():
        by_region.setdefault(s["region_id"], []).append(sid)
    region_names = dict(
        SdeRegion.objects.filter(region_id__in=by_region).values_list("region_id", "name")
    )
    region_labels = sorted(
        (
            {
                "name": region_names.get(rid, str(rid)),
                "px": round(sum(pos[i][0] for i in ids) / len(ids), 1),
                "py": round(sum(pos[i][1] for i in ids) / len(ids), 1),
            }
            for rid, ids in by_region.items()
        ),
        key=lambda r: r["name"],
    )

    return {
        "nodes": nodes, "edges": edges, "route_points": points,
        "regions": region_labels, "view": VIEW,
        "start": route[0]["name"], "end": route[-1]["name"],
        "start_id": route[0]["system_id"], "end_id": route[-1]["system_id"],
        "jumps": len(route) - 1, "region_count": len(by_region),
    }


def range_map(origin_id: int, system_ids: list[int]) -> dict | None:
    """A jump-range reach map: the origin plus every system inside jump range,
    projected into one viewBox with faint 'reach' spokes fanning out from the
    origin. Unlike :func:`route_map` there is no ordered path — the systems are a
    reachable *set*, so nothing connects them to each other. ``None`` if the
    origin has no coordinates.
    """
    # Origin first, then the in-range systems, de-duped (origin never repeats).
    seen: set[int] = {origin_id}
    targets: list[int] = []
    for sid in system_ids:
        if sid not in seen:
            seen.add(sid)
            targets.append(sid)

    rows = {
        s["system_id"]: s
        for s in SdeSolarSystem.objects.filter(system_id__in=[origin_id, *targets])
        .exclude(x=0.0, y=0.0, z=0.0)
        .values("system_id", "name", "security", "region_id", "x", "z")
    }
    if origin_id not in rows:
        return None
    shown_ids = [sid for sid in (origin_id, *targets) if sid in rows]

    xs = [rows[sid]["x"] for sid in shown_ids]
    zs = [rows[sid]["z"] for sid in shown_ids]
    minx, maxx, minz, maxz = min(xs), max(xs), min(zs), max(zs)
    span = max(maxx - minx, maxz - minz) or 1.0
    scale = (VIEW - 2 * PAD) / span
    off_x = (VIEW - (maxx - minx) * scale) / 2
    off_z = (VIEW - (maxz - minz) * scale) / 2

    def project(x: float, z: float) -> tuple[float, float]:
        return (round(off_x + (x - minx) * scale, 1),
                round(off_z + (maxz - z) * scale, 1))

    pos = {sid: project(rows[sid]["x"], rows[sid]["z"]) for sid in shown_ids}
    ox, oy = pos[origin_id]

    nodes = [
        {
            "id": sid, "name": rows[sid]["name"], "security": round(rows[sid]["security"], 1),
            "band": security_band(rows[sid]["security"]),
            "colour": security_colour(rows[sid]["security"]),
            "px": pos[sid][0], "py": pos[sid][1], "origin": sid == origin_id,
            "region_id": rows[sid]["region_id"],
        }
        for sid in shown_ids
    ]

    # Faint reach spokes from the origin to each in-range system (the "range fan").
    spokes = [
        {"x2": pos[sid][0], "y2": pos[sid][1]}
        for sid in shown_ids if sid != origin_id
    ]

    # One label per region, at the centroid of that region's shown systems.
    by_region: dict[int, list[int]] = {}
    for sid in shown_ids:
        by_region.setdefault(rows[sid]["region_id"], []).append(sid)
    region_names = dict(
        SdeRegion.objects.filter(region_id__in=by_region).values_list("region_id", "name")
    )
    region_labels = sorted(
        (
            {
                "name": region_names.get(rid, str(rid)),
                "px": round(sum(pos[i][0] for i in ids) / len(ids), 1),
                "py": round(sum(pos[i][1] for i in ids) / len(ids), 1),
            }
            for rid, ids in by_region.items()
        ),
        key=lambda r: r["name"],
    )

    return {
        "nodes": nodes, "spokes": spokes, "origin_x": ox, "origin_y": oy,
        "regions": region_labels, "view": VIEW,
        "origin": rows[origin_id]["name"], "origin_id": origin_id,
        "count": len(shown_ids) - 1, "region_count": len(by_region),
    }


def _sov_names(sov: dict, ids: list[int]) -> dict[int, str]:
    holders = set()
    for i in ids:
        s = sov.get(i) or {}
        holders.add(s.get("alliance_id") or s.get("corporation_id") or s.get("faction_id"))
    holders.discard(None)
    if not holders:
        return {}
    try:
        from core.esi.names import resolve_ids
        resolve_ids(list(holders))  # backfill any unknown alliance names
    except Exception:  # noqa: BLE001,S110 - name resolution is best-effort
        pass
    from apps.corporation.models import EveName
    return dict(EveName.objects.filter(entity_id__in=holders).values_list("entity_id", "name"))
