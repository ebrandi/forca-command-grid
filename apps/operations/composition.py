"""On-grid composition vs plan reconciliation (4.19).

After an operation, compare the PLANNED fleet composition (its ship slots) against what
home-corp pilots were actually seen flying on killmails in the op's time window — a
killboard-evidenced AAR of "did the fleet we planned show up on grid?".

This is a **lower bound**: only pilots who appeared on a killmail (scored a kill or died)
in the window are counted, so a quiet op — or a pilot who never touched a killmail — won't
show; the panel states this. It is not presence surveillance: it reports aggregate ship
counts for an after-action review, never per-pilot movement.
"""
from __future__ import annotations

import datetime as dt

from django.conf import settings

DEFAULT_DURATION_MINUTES = 120
GRACE_MINUTES = 15
MAX_WINDOW_MINUTES = 24 * 60  # cap so a mis-entered multi-day duration can't unbound the scan
CAPSULE_TYPE_IDS = frozenset({670, 33328})  # Capsule + Genolution pod — AAR noise, not a hull


def op_window(operation) -> tuple[dt.datetime, dt.datetime] | None:
    """``[start, end)`` the op plausibly covered — ``target_at`` ± its duration + grace,
    or None when the op has no target time (nothing to window a killboard scan on). The
    duration is capped so a mis-entered value can't blow the window (and the query) open."""
    if operation.target_at is None:
        return None
    grace = dt.timedelta(minutes=GRACE_MINUTES)
    dur_min = min(operation.duration_minutes or DEFAULT_DURATION_MINUTES, MAX_WINDOW_MINUTES)
    return operation.target_at - grace, operation.target_at + dt.timedelta(minutes=dur_min) + grace


def reconcile_composition(operation) -> dict | None:
    """Planned vs on-grid (killboard-evidenced) composition for one operation.

    Read-only; returns None if the op has no target time. Counts each home-corp pilot at
    most once per ship type (kills and losses both count — both are killmail participants).
    """
    from apps.killboard.models import Killmail, KillmailParticipant

    window = op_window(operation)
    if window is None:
        return None
    start, end = window

    # Planned composition first — if there's nothing to reconcile against, skip the
    # (potentially big) killboard scan entirely.
    planned: dict[int, int] = {}
    names: dict[int, str] = {}
    for slot in operation.ship_slots.all():
        if slot.ship_type_id:
            planned[slot.ship_type_id] = planned.get(slot.ship_type_id, 0) + slot.min_pilots
            names.setdefault(slot.ship_type_id, slot.ship_name)
    if not planned:
        return {"planned_rows": [], "off_plan": [], "total_planned": 0,
                "total_on_plan_fielded": 0, "any_participants": False,
                "window_start": start, "window_end": end, "has_plan": False}

    # Home-corp pilots seen on a killmail in the window — kills OR losses — unique pilot
    # per ship type. Narrow the KILLMAILS by time first (clean range scan on killmail_time),
    # then look participants up by killmail_id (hits the (killmail_id, role, seq) index) —
    # the same windowed idiom as apps/raffle/sources/pvp.py + apps/killboard/battle.py, so
    # the query is bounded by window width, not by total board history (review MED-1 fix).
    km_ids = list(
        Killmail.objects.filter(
            involves_home_corp=True, killmail_time__gte=start, killmail_time__lt=end,
        ).values_list("killmail_id", flat=True)
    )
    fielded: dict[int, set[int]] = {}
    if km_ids:
        for ship_type_id, character_id in (
            KillmailParticipant.objects.filter(
                killmail_id__in=km_ids,
                corporation_id=settings.FORCA_HOME_CORP_ID,
                character_id__isnull=False, ship_type_id__isnull=False,
            ).values_list("ship_type_id", "character_id")
        ):
            if ship_type_id in CAPSULE_TYPE_IDS:
                continue  # a pod on grid is AAR noise, not a fielded combat hull
            fielded.setdefault(ship_type_id, set()).add(character_id)
    fielded_counts = {tid: len(cids) for tid, cids in fielded.items()}

    planned_rows = [
        {
            "ship_type_id": tid, "name": names.get(tid), "planned": need,
            "fielded": fielded_counts.get(tid, 0),
            "short": max(0, need - fielded_counts.get(tid, 0)),
            "met": fielded_counts.get(tid, 0) >= need,
        }
        for tid, need in sorted(planned.items(), key=lambda kv: -kv[1])
    ]
    off_plan = sorted(
        ({"ship_type_id": tid, "fielded": n} for tid, n in fielded_counts.items()
         if tid not in planned),
        key=lambda r: -r["fielded"],
    )
    return {
        "planned_rows": planned_rows,
        "off_plan": off_plan,
        "total_planned": sum(planned.values()),
        "total_on_plan_fielded": sum(min(r["fielded"], r["planned"]) for r in planned_rows),
        "any_participants": bool(fielded_counts),
        "window_start": start, "window_end": end,
        "has_plan": bool(planned),
    }
