"""Saved-route camp / incursion push alerts (4.5).

Opt-in DM to the OWNER of a saved jump route when a gate camp or incursion appears on one
of its systems — so a hauler scouts before undocking. Rides the Pingboard governance-event
+ signature-dedup fabric: per route, at most one push per *change* in the threat set (the
marker is advanced only when alerted or cleared). Reuses the cached camp/incursion feeds —
no ESI per system. A RISK INDICATOR, not a guarantee (the public system-kills signal is
hourly and system-level; a quiet lethal camp reads as clear) — the DM says so.
"""
from __future__ import annotations

import hashlib
import logging

log = logging.getLogger("forca.navigation")
_EVENT_KEY = "navigation.route_watch"
_DANGER_LEVEL = "danger"


def _route_system_ids(route) -> list[int]:
    """The explicit systems on a saved route: origin, destination, exit, and any resolvable
    waypoints (SDE only). Order-preserving + deduped."""
    from .services import resolve_waypoints

    ids: list[int] = [route.origin_system_id, route.dest_system_id]
    if route.exit_system_id:
        ids.append(route.exit_system_id)
    if route.waypoints:
        try:
            systems, _unresolved = resolve_waypoints(route.waypoints)
            ids.extend(s.system_id for s in systems)
        except Exception:  # noqa: BLE001 - waypoint parsing is best-effort, never fatal
            log.exception("route-watch waypoint parse failed for route %s", route.id)
    seen: set[int] = set()
    out: list[int] = []
    for sid in ids:
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _threats_for(system_ids, kills, incursions, names) -> list[dict]:
    from .gatecamp import assess

    threats: list[dict] = []
    for sid in system_ids:
        if sid in incursions:
            threats.append({"system_id": sid, "kind": "incursion",
                            "name": names.get(sid, f"system {sid}"), "detail": "incursion"})
            continue
        verdict = assess(kills.get(sid) or {})
        if verdict["level"] == _DANGER_LEVEL:  # high-confidence camp signature only
            threats.append({"system_id": sid, "kind": "camp",
                            "name": names.get(sid, f"system {sid}"),
                            "detail": ", ".join(verdict["reasons"]) or "camp risk"})
    return threats


def _signature(threats) -> str:
    if not threats:
        return ""
    raw = ",".join(sorted(f"{t['system_id']}:{t['kind']}" for t in threats))
    return hashlib.sha1(raw.encode()).hexdigest()  # noqa: S324 - a dedup key, not security


def _names(ids) -> dict[int, str]:
    from apps.sde.models import SdeSolarSystem

    return dict(SdeSolarSystem.objects.filter(system_id__in=set(ids)).values_list("system_id", "name"))


def scan_route_watches() -> dict:
    """One sweep: DM the owner of each watched route whose threat set CHANGED. Inert unless
    the governance event is armed and a route has ``watch_enabled``."""
    from apps.pingboard.notifications import is_enabled

    from .models import SavedJumpRoute

    if not is_enabled(_EVENT_KEY):
        return {"status": "disabled"}
    routes = list(SavedJumpRoute.objects.filter(watch_enabled=True).select_related("owner"))
    if not routes:
        return {"alerted": 0, "routes": 0}

    from apps.navigation import map_overlays
    from apps.navigation.services import incursion_systems

    kills = map_overlays.system_kills()
    incursions = incursion_systems()

    per_route: dict[int, list[int]] = {}
    all_ids: set[int] = set()
    for r in routes:
        sids = _route_system_ids(r)
        per_route[r.id] = sids
        all_ids.update(sids)
    names = _names(all_ids)

    alerted = 0
    for r in routes:
        threats = _threats_for(per_route[r.id], kills, incursions, names)
        sig = _signature(threats)
        if sig == r.alerted_sig:
            continue  # threat set unchanged since the last sweep — nothing to say
        if threats:
            try:
                _emit(r, threats)
            except Exception:  # noqa: BLE001 - best-effort; one route mustn't sink the sweep
                log.exception("route-watch emit failed for route %s", r.id)
                continue
            r.alerted_sig = sig
            r.save(update_fields=["alerted_sig", "updated_at"])
            alerted += 1
        else:
            # Threats cleared → reset the marker (no DM) so a fresh threat re-alerts. Only
            # advanced here because the event is enabled (guarded above), so a disabled
            # event never swallows a pending change.
            r.alerted_sig = ""
            r.save(update_fields=["alerted_sig", "updated_at"])
    return {"alerted": alerted, "routes": len(routes)}


def _emit(route, threats) -> None:
    from apps.pingboard import services as pingboard
    from apps.pingboard.models import AlertCategory

    lines = "\n".join(f"• {t['name']}: {t['detail']}" for t in threats[:6])
    body = (
        f"⚠ Route **{route.name}** ({route.origin_name} → {route.dest_name}) — "
        f"{len(threats)} watched system(s) flagged (endpoints & waypoints only):\n{lines}\n"
        "Risk indicator, not a guarantee — this doesn't cover every gate hop. Scout before you undock."
    )
    # No idempotency_key: the per-route ``alerted_sig`` gates re-alerts to a *changed*
    # threat set, and emit's own 10-min duplicate_hash collapses concurrent/rapid repeats —
    # a permanent idempotency key would wrongly suppress a camp that clears then recurs.
    pingboard.emit_broadcast(
        category=AlertCategory.GATECAMP,
        title="Route watch: {route_name}",
        body=body,
        # Scaffold + raw context: the warning chrome localises per recipient while the route,
        # system names and threat lines stay raw. ``body`` remains the frozen English audit column.
        template="navigation.route_watch",
        context={
            "route_name": route.name,
            "origin_system": route.origin_name,
            "destination_system": route.dest_name,
            "threat_count": len(threats),
            "details": lines,
        },
        source_service="navigation",
        source_object_id=str(route.id),
        audience={"kind": "user", "id": route.owner_id},
    )
