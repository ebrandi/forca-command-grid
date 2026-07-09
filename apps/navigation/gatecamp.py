"""Gate-camp risk intel from public ESI + our static topology.

A gate camp is a hostile fleet sat on a stargate killing travellers. The public
signal is hourly per-system kills: pods dying is the camp signature, NPC kills are
ratting noise to subtract, and a low-gate-degree security-border system (a pipe) is
where camps actually sit. We score each system from those signals — system-level and
hourly, like every community tool, so it's a *risk* indicator, not a guarantee.

See research/06-gate-camp-intel.md.
"""
from __future__ import annotations

from apps.sde.models import SdeRegion, SdeSolarSystem, SdeSystemJump

from .maps import security_band, security_colour

_KNOWN_SPACE_MAX_REGION = 11000000
_BANDS = {"highsec", "lowsec", "nullsec"}
_BAND_RANK = {"highsec": 2, "lowsec": 1, "nullsec": 0}

# clear < elevated < caution < danger
LEVEL_RANK = {"clear": 0, "elevated": 1, "caution": 2, "danger": 3}
_LEVEL_BY_RANK = {v: k for k, v in LEVEL_RANK.items()}


def assess(kills: dict, jumps: int = 0, *, chokepoint: bool = False) -> dict:
    """Camp-risk verdict for one system from its last-hour kill counts.

    ``kills`` is an ESI ``system_kills`` row (ship_kills / pod_kills / npc_kills);
    ``jumps`` is last-hour ship jumps; ``chokepoint`` is the static pipe flag.
    Returns ``{level, score, reasons}``.
    """
    s = kills.get("ship_kills", 0) if kills else 0
    p = kills.get("pod_kills", 0) if kills else 0
    n = kills.get("npc_kills", 0) if kills else 0
    pvp = s + p
    if pvp == 0:
        return {"level": "clear", "score": 0, "reasons": []}

    ratting = n > 3 * max(pvp, 1)
    base = p * 3 + s            # pods weigh 3× — the camp signature
    score = round(base * (0.35 if ratting else 1.0))
    if chokepoint and not ratting:
        score = round(score * 1.4)

    reasons: list[str] = []
    if p:
        reasons.append(f"{p} pod{'s' if p != 1 else ''} killed")
    if s:
        reasons.append(f"{s} ship{'s' if s != 1 else ''} killed")
    if chokepoint:
        reasons.append("chokepoint (few gates)")
    if ratting:
        reasons.append("looks like ratting, not a camp")
    elif jumps <= 2 and pvp >= 1:
        reasons.append("kills with almost no traffic — possible ambush")

    if ratting:
        level = "elevated"
    elif p >= 3 or (s >= 8 and p >= 1) or (chokepoint and p >= 1 and s >= 2):
        level = "danger"
    elif p >= 1 or s >= 4 or (chokepoint and pvp >= 1):
        level = "caution"
    else:
        level = "elevated"
    return {"level": level, "score": score, "reasons": reasons}


def chokepoint_flags(system_ids: list[int]) -> dict[int, bool]:
    """Which of these systems are camp-prone pipes: few gates and either not high-sec
    or bordering a lower-security band (the gate hostiles wait on)."""
    ids = list(system_ids)
    if not ids:
        return {}
    deg: dict[int, int] = {}
    neigh: dict[int, list[int]] = {}
    for f, t in (
        SdeSystemJump.objects.filter(from_system_id__in=ids)
        .values_list("from_system_id", "to_system_id")
    ):
        deg[f] = deg.get(f, 0) + 1
        neigh.setdefault(f, []).append(t)

    own_band = {
        sid: security_band(sec)
        for sid, sec in SdeSolarSystem.objects.filter(system_id__in=ids)
        .values_list("system_id", "security")
    }
    all_neigh = {t for ns in neigh.values() for t in ns}
    nb_band = {
        sid: security_band(sec)
        for sid, sec in SdeSolarSystem.objects.filter(system_id__in=all_neigh)
        .values_list("system_id", "security")
    }

    flags: dict[int, bool] = {}
    for sid in ids:
        b = own_band.get(sid, "nullsec")
        borders_lower = any(
            _BAND_RANK.get(nb_band.get(t, "nullsec"), 0) < _BAND_RANK.get(b, 0)
            for t in neigh.get(sid, [])
        )
        flags[sid] = deg.get(sid, 0) <= 3 and (b != "highsec" or borders_lower)
    return flags


def camp_watch(*, region_id: int | None = None, band: str = "all",
               limit: int = 40) -> list[dict]:
    """Systems cluster-wide currently showing gate-camp signatures (caution+),
    ranked worst-first. Filterable by region and security band."""
    from .map_overlays import system_jumps, system_kills

    kills = system_kills()
    active = {
        sid: k for sid, k in kills.items()
        if (k.get("ship_kills", 0) + k.get("pod_kills", 0)) > 0
    }
    if not active:
        return []

    meta = {
        s["system_id"]: s
        for s in SdeSolarSystem.objects.filter(
            system_id__in=list(active), region_id__lt=_KNOWN_SPACE_MAX_REGION
        ).values("system_id", "name", "security", "region_id")
    }
    if region_id:
        meta = {sid: m for sid, m in meta.items() if m["region_id"] == region_id}
    if band in _BANDS:
        meta = {sid: m for sid, m in meta.items() if security_band(m["security"]) == band}
    if not meta:
        return []

    jumps = system_jumps()
    choke = chokepoint_flags(list(meta))
    region_names = dict(
        SdeRegion.objects.filter(region_id__in={m["region_id"] for m in meta.values()})
        .values_list("region_id", "name")
    )

    rows = []
    for sid, m in meta.items():
        a = assess(active[sid], jumps.get(sid, 0), chokepoint=choke.get(sid, False))
        if LEVEL_RANK[a["level"]] < LEVEL_RANK["caution"]:
            continue
        k = active[sid]
        rows.append({
            "system_id": sid, "name": m["name"],
            "security": round(m["security"], 1),
            "band": security_band(m["security"]),
            "colour": security_colour(m["security"]),
            "region": region_names.get(m["region_id"], ""),
            "region_id": m["region_id"],
            "level": a["level"], "score": a["score"], "reasons": a["reasons"],
            "pod_kills": k.get("pod_kills", 0), "ship_kills": k.get("ship_kills", 0),
            "traffic": jumps.get(sid, 0), "chokepoint": choke.get(sid, False),
        })

    rows.sort(key=lambda r: (LEVEL_RANK[r["level"]], r["score"]), reverse=True)
    return rows[:limit]


def route_camp_check(system_ids: list[int]) -> dict:
    """Per-hop camp verdicts for a planned route + a summary for the warning banner.

    Returns ``{by_id, flagged, worst, danger, caution}`` where ``by_id`` maps each
    system id to its assessment and ``flagged`` lists the caution/danger systems.
    """
    from .map_overlays import system_jumps, system_kills

    ids: list[int] = []
    seen: set[int] = set()
    for sid in system_ids:
        if sid not in seen:
            seen.add(sid)
            ids.append(sid)
    if not ids:
        return {"by_id": {}, "flagged": [], "worst": "clear", "danger": 0, "caution": 0}

    kills = system_kills()
    jumps = system_jumps()
    choke = chokepoint_flags(ids)
    names = dict(
        SdeSolarSystem.objects.filter(system_id__in=ids).values_list("system_id", "name")
    )

    by_id: dict[int, dict] = {}
    flagged: list[dict] = []
    worst = 0
    danger = caution = 0
    for sid in ids:
        a = assess(kills.get(sid, {}), jumps.get(sid, 0), chokepoint=choke.get(sid, False))
        by_id[sid] = a
        worst = max(worst, LEVEL_RANK[a["level"]])
        if a["level"] == "danger":
            danger += 1
        elif a["level"] == "caution":
            caution += 1
        if LEVEL_RANK[a["level"]] >= LEVEL_RANK["caution"]:
            flagged.append({"system_id": sid, "name": names.get(sid, str(sid)),
                            "level": a["level"], "reasons": a["reasons"]})

    return {
        "by_id": by_id, "flagged": flagged,
        "worst": _LEVEL_BY_RANK[worst], "danger": danger, "caution": caution,
    }
