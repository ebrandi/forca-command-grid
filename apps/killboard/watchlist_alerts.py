"""Watchlist activity tripwire alerts (4.4).

Opt-in early warning when a watched entity (hostile pilot / corp / alliance) shows up on a
fresh killmail — a gate-camp / roam tripwire. Rides the Pingboard due-sweep + governance
event fabric: per watchlist, at most one alert per entity per cooldown, only on a fresh
activation, corp audience. Framed as a RISK INDICATOR, not a guarantee (a quiet lethal camp
can read as "clear"; a passing roam as a threat) — the message says so.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

from django.db.models import Q
from django.utils import timezone

log = logging.getLogger("forca.killboard")
_EVENT_KEY = "killboard.watchlist_activity"


def scan_watchlist_activity(*, window_minutes: int = 90, cooldown_hours: int = 6) -> dict:
    """One sweep: alert on any watched entity that appeared on a killmail in the window and
    isn't still inside its per-entry cooldown. Inert (no-op) unless the governance event is
    armed and at least one watchlist has ``alerts_enabled``."""
    from apps.pingboard.notifications import is_enabled

    from .models import Killmail, KillmailParticipant, WatchlistEntry

    if not is_enabled(_EVENT_KEY):
        return {"status": "disabled"}
    now = timezone.now()
    window_start = now - dt.timedelta(minutes=max(1, window_minutes))
    cooldown = dt.timedelta(hours=max(1, cooldown_hours))

    entries = list(
        WatchlistEntry.objects.filter(watchlist__alerts_enabled=True).select_related("watchlist")
    )
    if not entries:
        return {"alerted": 0, "entries": 0}

    watched: dict[str, set[int]] = {"character": set(), "corporation": set(), "alliance": set()}
    for e in entries:
        bucket = watched.get(e.entity_type)  # tolerate an out-of-choices row, don't KeyError the sweep
        if bucket is not None:
            bucket.add(e.entity_id)

    # Narrow by the killmail time window first (index-backed), then match participants —
    # the app's windowed idiom, bounded by the window, not board history.
    km_ids = list(
        Killmail.objects.filter(killmail_time__gte=window_start).values_list("killmail_id", flat=True)
    )
    if not km_ids:
        return {"alerted": 0, "entries": len(entries)}

    matched: dict[tuple[str, int], dict] = {}
    for p in (
        KillmailParticipant.objects.filter(killmail_id__in=km_ids)
        .filter(Q(character_id__in=watched["character"])
                | Q(corporation_id__in=watched["corporation"])
                | Q(alliance_id__in=watched["alliance"]))
        .values("character_id", "corporation_id", "alliance_id", "killmail_id",
                "killmail__solar_system_id", "killmail__killmail_time")
        .order_by("killmail__killmail_time")  # last wins → freshest context per entity
    ):
        ctx = {"km_id": p["killmail_id"], "system_id": p["killmail__solar_system_id"],
               "time": p["killmail__killmail_time"]}
        if p["character_id"] in watched["character"]:
            matched[("character", p["character_id"])] = ctx
        if p["corporation_id"] in watched["corporation"]:
            matched[("corporation", p["corporation_id"])] = ctx
        if p["alliance_id"] in watched["alliance"]:
            matched[("alliance", p["alliance_id"])] = ctx

    if not matched:
        return {"alerted": 0, "entries": len(entries)}

    names = _resolve_names(
        {eid for _t, eid in matched} | {c["system_id"] for c in matched.values() if c["system_id"]}
    )

    # Group armed entries by the corp-wide entity so one hostile in several watchlists
    # alerts the corp ONCE per cooldown (not once per watchlist) — review LOW-1.
    by_entity: dict[tuple[str, int], list] = defaultdict(list)
    for e in entries:
        by_entity[(e.entity_type, e.entity_id)].append(e)

    alerted = 0
    for key, group in by_entity.items():
        ctx = matched.get(key)
        if ctx is None:
            continue
        if any(e.last_alerted_at is not None and (now - e.last_alerted_at) < cooldown for e in group):
            continue  # this entity was alerted recently — don't re-fire until it cools down
        try:
            _emit(group[0], ctx, names, now)
        except Exception:  # noqa: BLE001 - best-effort tripwire; one bad entity mustn't sink the sweep
            log.exception("watchlist tripwire emit failed for %s %s", key[0], key[1])
            continue
        for e in group:  # stamp EVERY entry for this entity so none re-fires next sweep
            e.last_alerted_at = now
            e.save(update_fields=["last_alerted_at"])
        alerted += 1
    return {"alerted": alerted, "entries": len(entries)}


def _resolve_names(ids) -> dict[int, str]:
    """Best-effort id → name from our own caches (no ESI in the sweep): the EveName cache
    for entities, the SDE for systems."""
    from apps.corporation.models import EveName
    from apps.sde.models import SdeSolarSystem

    ids = set(ids)
    out = dict(EveName.objects.filter(entity_id__in=ids).values_list("entity_id", "name"))
    for sid, name in SdeSolarSystem.objects.filter(system_id__in=ids).values_list("system_id", "name"):
        out.setdefault(sid, name)
    return out


def _emit(entry, ctx, names, now) -> None:
    from apps.pingboard import services as pingboard
    from apps.pingboard.models import AlertCategory

    kind = entry.get_entity_type_display().lower()
    ename = names.get(entry.entity_id) or f"{kind} #{entry.entity_id}"
    system = names.get(ctx["system_id"]) or "unknown space"
    mins = max(0, int((now - ctx["time"]).total_seconds() // 60)) if ctx["time"] else 0
    body = (
        f"⚠ Watched {kind} **{ename}** ({entry.watchlist.name}) was on a killmail in "
        f"{system} ~{mins}m ago. Risk indicator, not a guarantee — verify before acting."
    )
    pingboard.emit_broadcast(
        category=AlertCategory.HOME_DEFENCE,
        title="Watchlist tripwire: {entity_name}",
        body=body,
        # Scaffold + raw context: the tripwire chrome localises; the watched entity, the
        # watchlist name and the system stay raw. ``body`` is the frozen English audit column.
        template="killboard.watchlist_tripwire",
        context={"entity_type": kind, "entity_name": ename,
                 "watchlist_name": entry.watchlist.name, "system_name": system,
                 "minutes": mins},
        source_service="killboard",
        source_object_id=str(entry.id),
        audience={"kind": "corp"},
        idempotency_key=f"watchlist:{entry.id}:{ctx['km_id']}",
    )
    # KB-30: fan the same tripwire out to members' personal watchlist_hit subscriptions
    # (their chosen channel), reusing this existing emission point — not re-deriving the event.
    try:
        from .subscriptions import notify_watchlist_hit

        notify_watchlist_hit(entry, ctx, names)
    except Exception:  # noqa: BLE001 — a subscription hiccup must never sink the tripwire sweep
        log.exception("watchlist_hit subscription fan-out failed for entry %s", entry.id)
