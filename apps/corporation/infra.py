"""CORP-2 (roadmap 2.4) — unified corp infrastructure board.

Merges the two home-defence data sources that live on separate pages — Upwell
structure fuel/state/timers (``CorpStructure``) and sovereignty ADM
(``SovStructure``) — plus the manual/ESI timer board (``StructureTimer``) into one
list ranked by urgency: out-of-fuel / active reinforcement first, then low fuel,
soft ADM, and upcoming timers. Read-only; the leadership-set fuel/ADM thresholds
(2.3) decide what counts as low/soft, so the board and the alert agree.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

_SEV_RANK = {"critical": 0, "warning": 1, "ok": 2}


def infrastructure_board() -> list[dict]:
    """One urgency-ranked list of structures, sov systems and timers."""
    from apps.corporation.models import CorpStructure, StructureAlertConfig
    from apps.operations.models import SovStructure, StructureTimer

    now = timezone.now()
    fuel_days, adm_floor = StructureAlertConfig.thresholds()
    items: list[dict] = []

    for s in CorpStructure.objects.all():
        days = s.fuel_days_left
        reinforced = s.is_reinforced
        if s.is_out_of_fuel:
            sev, order, detail = "critical", 0.0, "Out of fuel"
        elif reinforced:
            secs = (s.state_timer_end - now).total_seconds() if s.state_timer_end else 0.0
            state = (s.state or "timer active").replace("_", " ")
            sev, order, detail = "critical", max(secs, 0.0), f"Reinforced — {state}"
        elif days is not None and days < fuel_days:
            sev, order, detail = "warning", days * 86400, f"{days:.1f} days of fuel left"
        else:
            sev = "ok"
            order = (days if days is not None else 999) * 86400
            detail = f"{days:.1f} days of fuel" if days is not None else "Fuel unknown"
        items.append({
            "kind": "structure",
            "name": s.name or f"Structure {s.structure_id}",
            "system": s.system_name,
            "severity": sev, "order": order, "detail": detail,
            "fuel_days": round(days, 1) if days is not None else None,
            # Only surface a countdown when actually reinforced (a stale past state_timer_end
            # on an otherwise-healthy structure would render a misleading "in 0 minutes").
            "timer_end": s.state_timer_end if reinforced else None,
        })

    for sov in SovStructure.objects.all():
        soft = sov.adm < adm_floor
        items.append({
            "kind": "sov",
            "name": sov.system_name or f"System {sov.solar_system_id}",
            "system": sov.system_name,
            "severity": "warning" if soft else "ok",
            "order": sov.adm * 86400,
            "detail": f"ADM {sov.adm:.1f}" + (" — soft" if soft else ""),
            "adm": round(sov.adm, 1),
            "timer_end": sov.vulnerable_end,
        })

    # Manual/ESI timers still relevant (exited within the last 6h or upcoming).
    for t in StructureTimer.objects.filter(exits_at__gte=now - timedelta(hours=6)):
        secs = (t.exits_at - now).total_seconds()
        items.append({
            "kind": "timer",
            "name": t.name,
            "system": t.system_name,
            "severity": "critical" if secs < 48 * 3600 else "warning",
            # Clamp so a just-exited timer doesn't sort above out-of-fuel (order 0).
            "order": max(secs, 0.0),
            "detail": f"{t.get_timer_type_display()} timer · {t.get_side_display()}",
            "timer_end": t.exits_at,
            "side": t.side,
        })

    items.sort(key=lambda i: (_SEV_RANK[i["severity"]], i["order"]))
    return items
