"""Roaming-target intel: where will a PvP gang find ratters and miners?

CCP publishes hourly per-system NPC kills (ratting/mining proxy) and ship kills via
public ESI. We rank systems by NPC activity and bias toward space that is busy but
lightly defended (high NPC kills, few recent ship kills) — the classic signature of
unescorted ratters and mining fleets. Pure public data, no token.
"""
from __future__ import annotations

from apps.sde.models import SdeRegion, SdeSolarSystem

from .maps import security_band, security_colour

_KNOWN_SPACE_MAX_REGION = 11000000

_BANDS = {"highsec", "lowsec", "nullsec"}


def roaming_targets(*, region_id: int | None = None, band: str = "nullsec",
                    limit: int = 40) -> list[dict]:
    """Top systems by ratting/mining activity, scored for soft (undefended) targets.

    ``band`` filters by security class ("all" keeps every band); ``region_id`` scopes
    to one region. Score rewards NPC kills and penalises recent ship kills, so quiet,
    target-rich systems float to the top.
    """
    from .map_overlays import system_jumps, system_kills

    kills = system_kills()
    active = {sid: k for sid, k in kills.items() if (k.get("npc_kills") or 0) > 0}
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
    if not meta:
        return []

    region_names = dict(
        SdeRegion.objects.filter(region_id__in={m["region_id"] for m in meta.values()})
        .values_list("region_id", "name")
    )
    traffic = system_jumps()

    rows = []
    for sid, m in meta.items():
        b = security_band(m["security"])
        if band in _BANDS and b != band:
            continue
        k = active[sid]
        npc = k.get("npc_kills", 0)
        ship = k.get("ship_kills", 0)
        pod = k.get("pod_kills", 0)
        # Busy ratting, little PvP → high opportunity. Ship kills mean the field is
        # already contested (or defended), so they damp the score.
        score = round(npc / (1.0 + 2.0 * ship), 1)
        rows.append({
            "system_id": sid, "name": m["name"],
            "security": round(m["security"], 1), "band": b,
            "colour": security_colour(m["security"]),
            "region": region_names.get(m["region_id"], ""),
            "region_id": m["region_id"],
            "npc_kills": npc, "ship_kills": ship, "pod_kills": pod,
            "traffic": traffic.get(sid, 0),
            "score": score,
            "contested": ship > 0,
        })

    rows.sort(key=lambda r: (r["score"], r["npc_kills"]), reverse=True)
    return rows[:limit]
