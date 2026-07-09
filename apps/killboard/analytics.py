"""Corp combat analytics for the stats dashboard.

Everything here is computed live from the killmails we already store (no rollup
table, no ESI calls) and memoized in the cache for a few minutes — the dashboard
tolerates slight staleness, and at the current data volume the aggregate queries
are cheap (all run on indexed columns: ``killmail_time``, ``involves_home_corp``,
``home_corp_role``, ``solar_system_id`` / ``region_id``).

Consistent with the rankings (``leaderboards.py``): every metric is **PvP only** —
killmails with no player attacker (``is_npc``: ratting, structure shoots) are
excluded, so the numbers match what the leaderboards already show.
"""
from __future__ import annotations

import calendar
from datetime import UTC, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Sum
from django.db.models.functions import ExtractHour, ExtractIsoWeekDay, TruncDate, TruncMonth
from django.utils import timezone

from .leaderboards import danger_rating
from .models import Killmail, KillmailParticipant, SecBand

POD_TYPE_ID = 670  # Capsule — excluded from "ships fielded" / doctrine-compliance counts

# Held longer than the cache-warm cadence (every 5 min, see config/celery.py) so a
# warmed key never expires between warm cycles and users only ever read warm cache.
CACHE_TTL = 900
CACHE_VERSION = 1

# The all-time (full-history) dashboard breakdowns scan the whole killboard and
# change only marginally as a handful of new kills land against a large base, so
# they are cached under a longer TTL and refreshed on a slower cadence than the
# 5-min warm — the fresh 12-month/heatmap/summary half stays on the 5-min tick.
ALLTIME_TTL = 3600
ALLTIME_WARM_INTERVAL = timedelta(minutes=15)

MONTHS_BACK = 12
TOP_SHIPS = 10
TOP_SYSTEMS = 10
HEATMAP_DAYS = 90

# EVE plays on UTC ("EVE time"); pin every hour/day extraction to UTC so the
# heatmap reads the same regardless of the server's local timezone.
_EVE_TZ = UTC

_ATTACKER = Killmail.HomeRole.ATTACKER
_VICTIM = Killmail.HomeRole.VICTIM


def _home() -> int:
    return getattr(settings, "FORCA_HOME_CORP_ID", 0)


def _pvp(qs):
    """Restrict to home-corp PvP killmails (drops NPC-only ratting deaths)."""
    return qs.filter(involves_home_corp=True, is_npc=False)


# --- Month buckets ----------------------------------------------------------
def _month_buckets(n: int) -> list[tuple[int, int]]:
    """The last ``n`` (year, month) pairs, oldest first, including this month."""
    now = timezone.now()
    y, m = now.year, now.month
    out: list[tuple[int, int]] = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def monthly_series(months: int = MONTHS_BACK) -> dict:
    """Kills/losses and ISK destroyed/lost per calendar month (UTC)."""
    buckets = _month_buckets(months)
    oy, om = buckets[0]
    start = timezone.now().replace(
        year=oy, month=om, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    rows = (
        _pvp(Killmail.objects.filter(killmail_time__gte=start))
        .annotate(month=TruncMonth("killmail_time", tzinfo=_EVE_TZ))
        .values("month", "home_corp_role")
        .annotate(n=Count("killmail_id"), isk=Sum("total_value"))
    )
    by_key: dict[tuple[int, int], dict] = {
        b: {"kills": 0, "losses": 0, "isk_destroyed": 0, "isk_lost": 0} for b in buckets
    }
    for r in rows:
        mo = r["month"]
        key = (mo.year, mo.month)
        if key not in by_key:
            continue
        if r["home_corp_role"] == _ATTACKER:
            by_key[key]["kills"] = r["n"]
            by_key[key]["isk_destroyed"] = int(r["isk"] or 0)
        elif r["home_corp_role"] == _VICTIM:
            by_key[key]["losses"] = r["n"]
            by_key[key]["isk_lost"] = int(r["isk"] or 0)
    labels, kills, losses, isk_destroyed, isk_lost = [], [], [], [], []
    for (y, m) in buckets:
        d = by_key[(y, m)]
        labels.append(f"{calendar.month_abbr[m]} {str(y)[2:]}")
        kills.append(d["kills"])
        losses.append(d["losses"])
        isk_destroyed.append(d["isk_destroyed"])
        isk_lost.append(d["isk_lost"])
    return {
        "labels": labels,
        "kills": kills,
        "losses": losses,
        "isk_destroyed": isk_destroyed,
        "isk_lost": isk_lost,
    }


# --- Ships -------------------------------------------------------------------
def _ship_breakdown(role: str, limit: int) -> list[dict]:
    """Top victim ship types for a home role (enemy ships killed / our ships lost)."""
    rows = list(
        _pvp(Killmail.objects.filter(involves_home_corp=True, home_corp_role=role))
        .values("victim_ship_type_id")
        .annotate(n=Count("killmail_id"), isk=Sum("total_value"))
        .order_by("-n")[:limit]
    )
    names = _type_names([r["victim_ship_type_id"] for r in rows])
    return [
        {
            "ship_type_id": r["victim_ship_type_id"],
            "name": names.get(r["victim_ship_type_id"], f"Type {r['victim_ship_type_id']}"),
            "count": r["n"],
            "isk": int(r["isk"] or 0),
        }
        for r in rows
    ]


def top_ships(limit: int = TOP_SHIPS) -> dict:
    """Enemy ships we destroy most, and our ships we lose most."""
    return {"killed": _ship_breakdown(_ATTACKER, limit), "lost": _ship_breakdown(_VICTIM, limit)}


# --- Locations ---------------------------------------------------------------
def top_systems(limit: int = TOP_SYSTEMS) -> list[dict]:
    rows = list(
        _pvp(Killmail.objects)
        .values("solar_system_id")
        .annotate(n=Count("killmail_id"))
        .order_by("-n")[:limit]
    )
    names = _system_names([r["solar_system_id"] for r in rows])
    return [
        {
            "system_id": r["solar_system_id"],
            "name": names.get(r["solar_system_id"], f"System {r['solar_system_id']}"),
            "count": r["n"],
        }
        for r in rows
    ]


def top_regions(limit: int = TOP_SYSTEMS) -> list[dict]:
    rows = list(
        _pvp(Killmail.objects.filter(region_id__isnull=False))
        .values("region_id")
        .annotate(n=Count("killmail_id"))
        .order_by("-n")[:limit]
    )
    names = _region_names([r["region_id"] for r in rows])
    return [
        {
            "region_id": r["region_id"],
            "name": names.get(r["region_id"], f"Region {r['region_id']}"),
            "count": r["n"],
        }
        for r in rows
    ]


# --- Activity heatmap (hour-of-day x day-of-week, UTC) -----------------------
def activity_heatmap(days: int = HEATMAP_DAYS) -> dict:
    """A 7x24 grid of fight activity over the last ``days`` (EVE/UTC time).

    Rows are ISO weekday 1=Mon..7=Sun; columns are hours 0..23. ``rows`` is the
    grid (list of 7 lists of 24 ints); ``peak`` is the busiest single cell, used
    to scale colour intensity in the template.
    """
    since = timezone.now() - timedelta(days=days)
    grid = [[0] * 24 for _ in range(7)]
    rows = (
        _pvp(Killmail.objects.filter(killmail_time__gte=since))
        .annotate(
            dow=ExtractIsoWeekDay("killmail_time", tzinfo=_EVE_TZ),
            hour=ExtractHour("killmail_time", tzinfo=_EVE_TZ),
        )
        .values("dow", "hour")
        .annotate(n=Count("killmail_id"))
    )
    for r in rows:
        dow, hour, n = r["dow"], r["hour"], r["n"]
        if dow and 1 <= dow <= 7 and hour is not None and 0 <= hour <= 23:
            grid[dow - 1][hour] = n
    return {**_heatmap_cells(grid), "days": days}


def _heatmap_cells(grid: list[list[int]]) -> dict:
    """Wrap a 7x24 count grid with template-friendly cells + intensity levels."""
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    peak = max((max(row) for row in grid), default=0)
    cells = [
        {
            "label": labels[d],
            "hours": [{"hour": h, "n": grid[d][h], "level": _intensity(grid[d][h], peak)}
                      for h in range(24)],
        }
        for d in range(7)
    ]
    return {
        "rows": grid,
        "cells": cells,
        "peak": peak,
        "day_labels": labels,
        "total": sum(sum(row) for row in grid),
    }


def _intensity(n: int, peak: int) -> int:
    """Bucket a cell count into 0 (none) or 1..4 by its share of the peak cell."""
    if n <= 0 or peak <= 0:
        return 0
    share = n / peak
    if share <= 0.25:
        return 1
    if share <= 0.50:
        return 2
    if share <= 0.75:
        return 3
    return 4


# --- Headline summary (efficiency / danger) ---------------------------------
def _summary_from(kills: int, losses: int, solo_kills: int, isk_destroyed: int,
                  isk_lost: int) -> dict:
    denom = isk_destroyed + isk_lost
    return {
        "kills": kills,
        "losses": losses,
        "solo_kills": solo_kills,
        "isk_destroyed": isk_destroyed,
        "isk_lost": isk_lost,
        "efficiency": (isk_destroyed / denom * 100.0) if denom else 0.0,
        "danger": danger_rating(kills, losses),
    }


def _summary_live() -> dict:
    kills_qs = _pvp(Killmail.objects.filter(involves_home_corp=True, home_corp_role=_ATTACKER))
    losses_qs = _pvp(Killmail.objects.filter(involves_home_corp=True, home_corp_role=_VICTIM))
    return _summary_from(
        kills=kills_qs.count(),
        losses=losses_qs.count(),
        solo_kills=kills_qs.filter(is_solo=True).count(),
        isk_destroyed=int(kills_qs.aggregate(s=Sum("total_value"))["s"] or 0),
        isk_lost=int(losses_qs.aggregate(s=Sum("total_value"))["s"] or 0),
    )


def summary() -> dict:
    """All-time corp headline. Served from the precomputed ``CombatMetric`` corp
    rollup (rebuilt every 15 min) when present — an indexed single-row read — and
    falls back to live aggregation when the rollup hasn't been built yet."""
    from .models import CombatMetric

    row = (
        CombatMetric.objects.filter(
            entity_type=CombatMetric.EntityType.CORPORATION, entity_id=_home(), window="all"
        )
        .values("kills", "losses", "solo_kills", "isk_destroyed", "isk_lost")
        .first()
    )
    if row is None:
        return _summary_live()
    return _summary_from(
        kills=row["kills"],
        losses=row["losses"],
        solo_kills=row["solo_kills"],
        isk_destroyed=int(row["isk_destroyed"] or 0),
        isk_lost=int(row["isk_lost"] or 0),
    )


# --- Name resolution (from the bundled SDE) ---------------------------------
def _type_names(ids: list[int]) -> dict[int, str]:
    from apps.sde.models import SdeType

    return dict(SdeType.objects.filter(type_id__in=ids).values_list("type_id", "name"))


def _system_names(ids: list[int]) -> dict[int, str]:
    from apps.sde.models import SdeSolarSystem

    return dict(SdeSolarSystem.objects.filter(system_id__in=ids).values_list("system_id", "name"))


def _region_names(ids: list[int]) -> dict[int, str]:
    from apps.sde.models import SdeRegion

    return dict(SdeRegion.objects.filter(region_id__in=ids).values_list("region_id", "name"))


# --- Assembled dashboard payload --------------------------------------------
def _alltime_breakdowns(*, use_cache: bool = True, refresh: bool = False) -> dict:
    """The all-time (full-history) half of the dashboard: the ship, ship-class,
    security-band, doctrine-compliance, system and region breakdowns. These scan
    the whole killboard, so they're cached under ``ALLTIME_TTL`` (an hour) and
    refreshed on a slower cadence than the 5-min warm (see ``warm_caches``).

    ``refresh=True`` recomputes and re-caches even on a hit — used by the warmer.
    Keys mirror the slice of ``dashboard()`` they feed, so the composed payload is
    byte-for-byte identical to the pre-split version.
    """
    key = f"kb:stats:alltime:{CACHE_VERSION}:{_home()}"
    if use_cache and not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached
    payload = {
        "ships": top_ships(),
        "ship_classes": ship_class_breakdown(),
        "space": security_breakdown(),
        "doctrine": doctrine_compliance(),
        "systems": top_systems(),
        "regions": top_regions(),
    }
    if use_cache:
        cache.set(key, payload, ALLTIME_TTL)
    return payload


def dashboard(*, use_cache: bool = True, refresh: bool = False) -> dict:
    """Everything the stats dashboard renders, memoized briefly.

    ``refresh=True`` recomputes and re-caches even on a hit — used by the cache
    warmer so the public dashboard never computes on the request path. The heavy
    all-time breakdowns are pulled from their own longer-lived cache (refreshed on
    a slower cadence) so the 5-min warm only recomputes the fresh 12-month/heatmap
    half; on a cold all-time cache they're computed here so output never degrades.
    """
    key = f"kb:stats:{CACHE_VERSION}:{_home()}"
    if use_cache and not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached
    alltime = _alltime_breakdowns(use_cache=use_cache)
    payload = {
        "summary": summary(),
        "monthly": monthly_series(),
        "ships": alltime["ships"],
        "ship_classes": alltime["ship_classes"],
        "space": alltime["space"],
        "doctrine": alltime["doctrine"],
        "systems": alltime["systems"],
        "regions": alltime["regions"],
        "heatmap": activity_heatmap(),
        "months_back": MONTHS_BACK,
        "heatmap_days": HEATMAP_DAYS,
    }
    if use_cache:
        cache.set(key, payload, CACHE_TTL)
    return payload


# --- Ship-class composition (group kills/losses by ship group) --------------
def _group_names(type_ids: list[int]) -> dict[int, str]:
    from apps.sde.models import SdeType

    return dict(SdeType.objects.filter(type_id__in=type_ids).values_list("type_id", "group__name"))


def _class_rows(role: str, limit: int = 8) -> list[dict]:
    rows = list(
        _pvp(Killmail.objects.filter(involves_home_corp=True, home_corp_role=role))
        .values("victim_ship_type_id")
        .annotate(n=Count("killmail_id"))
    )
    gmap = _group_names([r["victim_ship_type_id"] for r in rows])
    agg: dict[str, int] = {}
    for r in rows:
        cls = gmap.get(r["victim_ship_type_id"]) or "Other"
        agg[cls] = agg.get(cls, 0) + r["n"]
    out = sorted(({"name": k, "count": v} for k, v in agg.items()), key=lambda x: -x["count"])
    head, tail = out[:limit], out[limit:]
    if tail:
        head.append({"name": "Other", "count": sum(x["count"] for x in tail)})
    return head


def ship_class_breakdown() -> dict:
    """Kills and losses grouped by ship class (frigate / cruiser / capital / …)."""
    return {"killed": _class_rows(_ATTACKER), "lost": _class_rows(_VICTIM)}


# --- Doctrine compliance (are our pilots fielding doctrine hulls?) -----------
def _active_doctrine_hulls() -> set[int]:
    from apps.doctrines.models import Doctrine, DoctrineFit

    return set(
        DoctrineFit.objects.filter(doctrine__status=Doctrine.Status.ACTIVE)
        .values_list("ship_type_id", flat=True)
    )


def doctrine_compliance() -> dict:
    """Share of home ships fielded on PvP kills that are active-doctrine hulls.

    Counts attacker rows flown by home-corp pilots on the corp's PvP kills
    (capsules excluded — a pod isn't a fielded ship). Returns ``configured``
    False when no active doctrine has any hull, so the UI can hide the chart
    rather than imply 0% compliance.
    """
    hulls = _active_doctrine_hulls()
    if not hulls:
        return {"configured": False, "on": 0, "off": 0, "total": 0, "on_pct": 0.0}
    rows = list(
        KillmailParticipant.objects.filter(
            role="attacker",
            corporation_id=_home(),
            killmail__is_npc=False,
            killmail__home_corp_role=_ATTACKER,
        )
        .exclude(ship_type_id=POD_TYPE_ID)
        .exclude(ship_type_id__isnull=True)
        .values("ship_type_id")
        .annotate(n=Count("killmail_id"))
    )
    on = sum(r["n"] for r in rows if r["ship_type_id"] in hulls)
    off = sum(r["n"] for r in rows if r["ship_type_id"] not in hulls)
    total = on + off
    return {
        "configured": True,
        "on": on,
        "off": off,
        "total": total,
        "on_pct": (on / total * 100.0) if total else 0.0,
    }


# --- Per-pilot analytics ----------------------------------------------------
def _pilot_attacker_qs(character_id: int):
    return KillmailParticipant.objects.filter(
        role="attacker", character_id=character_id, killmail__is_npc=False
    )


def _pilot_loss_qs(character_id: int):
    return Killmail.objects.filter(victim_character_id=character_id, is_npc=False)


def pilot_monthly(character_id: int, months: int = MONTHS_BACK) -> dict:
    buckets = _month_buckets(months)
    oy, om = buckets[0]
    start = timezone.now().replace(
        year=oy, month=om, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    kills = (
        _pilot_attacker_qs(character_id)
        .filter(killmail__killmail_time__gte=start)
        .annotate(month=TruncMonth("killmail__killmail_time", tzinfo=_EVE_TZ))
        .values("month")
        .annotate(n=Count("killmail_id", distinct=True))
    )
    losses = (
        _pilot_loss_qs(character_id)
        .filter(killmail_time__gte=start)
        .annotate(month=TruncMonth("killmail_time", tzinfo=_EVE_TZ))
        .values("month")
        .annotate(n=Count("killmail_id"))
    )
    kmap = {(r["month"].year, r["month"].month): r["n"] for r in kills}
    lmap = {(r["month"].year, r["month"].month): r["n"] for r in losses}
    labels, k, lo = [], [], []
    for (y, m) in buckets:
        labels.append(f"{calendar.month_abbr[m]} {str(y)[2:]}")
        k.append(kmap.get((y, m), 0))
        lo.append(lmap.get((y, m), 0))
    return {"labels": labels, "kills": k, "losses": lo}


def pilot_ships(character_id: int, limit: int = TOP_SHIPS) -> dict:
    flown = list(
        _pilot_attacker_qs(character_id)
        .exclude(ship_type_id__isnull=True)
        .exclude(ship_type_id=POD_TYPE_ID)
        .values("ship_type_id")
        .annotate(n=Count("killmail_id", distinct=True))
        .order_by("-n")[:limit]
    )
    fnames = _type_names([r["ship_type_id"] for r in flown])
    flown_out = [
        {"ship_type_id": r["ship_type_id"],
         "name": fnames.get(r["ship_type_id"], f"Type {r['ship_type_id']}"), "count": r["n"]}
        for r in flown
    ]
    lost = list(
        _pilot_loss_qs(character_id)
        .values("victim_ship_type_id")
        .annotate(n=Count("killmail_id"))
        .order_by("-n")[:limit]
    )
    lnames = _type_names([r["victim_ship_type_id"] for r in lost])
    lost_out = [
        {"ship_type_id": r["victim_ship_type_id"],
         "name": lnames.get(r["victim_ship_type_id"], f"Type {r['victim_ship_type_id']}"),
         "count": r["n"]}
        for r in lost
    ]
    return {"flown": flown_out, "lost": lost_out}


def pilot_heatmap(character_id: int, days: int = HEATMAP_DAYS) -> dict:
    since = timezone.now() - timedelta(days=days)
    grid = [[0] * 24 for _ in range(7)]
    kill_rows = (
        _pilot_attacker_qs(character_id)
        .filter(killmail__killmail_time__gte=since)
        .annotate(
            dow=ExtractIsoWeekDay("killmail__killmail_time", tzinfo=_EVE_TZ),
            hour=ExtractHour("killmail__killmail_time", tzinfo=_EVE_TZ),
        )
        .values("dow", "hour")
        .annotate(n=Count("killmail_id", distinct=True))
    )
    loss_rows = (
        _pilot_loss_qs(character_id)
        .filter(killmail_time__gte=since)
        .annotate(
            dow=ExtractIsoWeekDay("killmail_time", tzinfo=_EVE_TZ),
            hour=ExtractHour("killmail_time", tzinfo=_EVE_TZ),
        )
        .values("dow", "hour")
        .annotate(n=Count("killmail_id"))
    )
    for rows in (kill_rows, loss_rows):
        for r in rows:
            dow, hour, n = r["dow"], r["hour"], r["n"]
            if dow and 1 <= dow <= 7 and hour is not None and 0 <= hour <= 23:
                grid[dow - 1][hour] += n
    return {**_heatmap_cells(grid), "days": days}


def pilot_systems(character_id: int, limit: int = TOP_SYSTEMS) -> list[dict]:
    agg: dict[int, int] = {}
    kills = (
        _pilot_attacker_qs(character_id)
        .values("killmail__solar_system_id")
        .annotate(n=Count("killmail_id", distinct=True))
    )
    for r in kills:
        sid = r["killmail__solar_system_id"]
        agg[sid] = agg.get(sid, 0) + r["n"]
    losses = (
        _pilot_loss_qs(character_id).values("solar_system_id").annotate(n=Count("killmail_id"))
    )
    for r in losses:
        sid = r["solar_system_id"]
        agg[sid] = agg.get(sid, 0) + r["n"]
    top = sorted(agg.items(), key=lambda x: -x[1])[:limit]
    names = _system_names([sid for sid, _ in top])
    return [{"system_id": sid, "name": names.get(sid, f"System {sid}"), "count": n}
            for sid, n in top]


def pilot_analytics(character_id: int, *, use_cache: bool = True) -> dict:
    """All per-pilot charts: combat card, monthly trend, ships, heatmap, systems."""
    key = f"kb:pilot:{CACHE_VERSION}:{character_id}"
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached
    from .leaderboards import pilot_combat_card

    payload = {
        "character_id": character_id,
        "card": pilot_combat_card(character_id),
        "monthly": pilot_monthly(character_id),
        "ships": pilot_ships(character_id),
        "heatmap": pilot_heatmap(character_id),
        "systems": pilot_systems(character_id),
        "months_back": MONTHS_BACK,
        "heatmap_days": HEATMAP_DAYS,
    }
    if use_cache:
        cache.set(key, payload, CACHE_TTL)
    return payload


# --- "Where in space" — kills/losses by security band -----------------------
# Geographic star maps need per-system coordinates the SDE import doesn't carry,
# so this security-band breakdown is the honest, data-backed answer to "where do
# we fight" (highsec / lowsec / nullsec / wormhole / …).
_SEC_BANDS = [
    (SecBand.HIGHSEC, "Highsec"),
    (SecBand.LOWSEC, "Lowsec"),
    (SecBand.NULLSEC, "Nullsec"),
    (SecBand.WORMHOLE, "Wormhole"),
    (SecBand.POCHVEN, "Pochven"),
    (SecBand.ABYSSAL, "Abyssal"),
    (SecBand.UNKNOWN, "Unknown"),
]


def _band_rows(role: str) -> list[dict]:
    counts = {
        r["sec_band"]: r["n"]
        for r in _pvp(Killmail.objects.filter(involves_home_corp=True, home_corp_role=role))
        .values("sec_band")
        .annotate(n=Count("killmail_id"))
    }
    return [{"name": label, "count": counts[key]} for key, label in _SEC_BANDS if counts.get(key)]


def security_breakdown() -> dict:
    """Kills and losses grouped by security band of the system."""
    return {"killed": _band_rows(_ATTACKER), "lost": _band_rows(_VICTIM)}


# --- Pilot comparison -------------------------------------------------------
# Corp-vs-corp comparison isn't possible here: we only ingest killmails that
# involve the home corp, so an external corp's record in our DB is just the
# fights against us — biased. Pilot-vs-pilot uses complete data and supports the
# prize-challenge use case directly.
def compare_pilots(character_ids: list[int]) -> dict:
    """Overlaid monthly kill trends + headline totals for up to 5 pilots."""
    from .leaderboards import pilot_combat_card

    ids = list(dict.fromkeys(character_ids))[:5]  # de-dup, cap at 5
    labels: list[str] = []
    series, table = [], []
    for cid in ids:
        m = pilot_monthly(cid)
        labels = m["labels"]
        card = pilot_combat_card(cid)
        series.append({"character_id": cid, "kills": m["kills"], "losses": m["losses"]})
        table.append({
            "character_id": cid,
            "kills": card["kills"],
            "losses": card["losses"],
            "efficiency": card["efficiency"],
            "solo_kills": card["solo_kills"],
        })
    return {"labels": labels, "series": series, "table": table}


# --- Killfeed home (the public /killboard/ portal) --------------------------
def _daily_kills(days: int = 14) -> list[int]:
    """Kill count per day for the last ``days`` (UTC), oldest first."""
    since = timezone.now() - timedelta(days=days)
    rows = (
        _pvp(Killmail.objects.filter(home_corp_role=_ATTACKER, killmail_time__gte=since))
        .annotate(day=TruncDate("killmail_time", tzinfo=_EVE_TZ))
        .values("day")
        .annotate(n=Count("killmail_id"))
    )
    by_day = {r["day"]: r["n"] for r in rows}
    today = timezone.now().astimezone(UTC).date()
    return [by_day.get(today - timedelta(days=i), 0) for i in range(days - 1, -1, -1)]


def _spark_points(values: list[int], w: int = 120, h: int = 28) -> str:
    """An SVG polyline ``points`` string for a sparkline of ``values``."""
    if not values:
        return ""
    mx = max(values) or 1
    n = len(values)
    step = w / (n - 1) if n > 1 else 0
    pts = []
    for i, v in enumerate(values):
        x = round(i * step, 1)
        y = round(h - (v / mx) * (h - 2) - 1, 1)
        pts.append(f"{x},{y}")
    return " ".join(pts)


def _active_systems(days: int = 7, limit: int = 5) -> list[dict]:
    since = timezone.now() - timedelta(days=days)
    rows = list(
        _pvp(Killmail.objects.filter(killmail_time__gte=since))
        .values("solar_system_id")
        .annotate(n=Count("killmail_id"))
        .order_by("-n")[:limit]
    )
    names = _system_names([r["solar_system_id"] for r in rows])
    return [
        {"system_id": r["solar_system_id"],
         "name": names.get(r["solar_system_id"], f"System {r['solar_system_id']}"),
         "count": r["n"]}
        for r in rows
    ]


def killfeed_overview(*, use_cache: bool = True, refresh: bool = False) -> dict:
    """Portal data for the public killboard home: hero stats, a 14-day spark,
    the biggest recent kills, and 7-day top-killer / active-system rails.

    Memoised briefly — this rides the hot path of the public killfeed, but the
    figures (and the leaderboards it reuses) are already public on the rankings
    page, so nothing new is exposed. ``refresh=True`` re-caches on a hit (warmer).
    """
    key = f"kb:feed:{CACHE_VERSION}:{_home()}"
    if use_cache and not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached

    from .leaderboards import leaderboards

    lb = leaderboards("7d")
    top_killers = next(
        (c["rows"][:5] for c in lb["categories"] if c["key"] == "top_killers"), []
    )
    spark = _daily_kills(14)
    payload = {
        "summary": summary(),
        "spark": spark,
        "spark_points": _spark_points(spark),
        "biggest": lb["most_valuable"][:5],
        "top_killers": top_killers,
        "active_systems": _active_systems(7, 5),
    }
    if use_cache:
        cache.set(key, payload, CACHE_TTL)
    return payload


# Rankings windows worth keeping permanently warm — the ones the UI lands on most
# (the rankings default is "month"; the killfeed rail reuses "7d").
WARM_WINDOWS = ("7d", "30d", "month")


def _alltime_due() -> bool:
    """True at most once per ``ALLTIME_WARM_INTERVAL``. Throttles the heavy all-time
    dashboard refresh to a slower cadence than the 5-min warm tick, without needing a
    separate beat entry. The marker TTL is shorter than ``ALLTIME_TTL`` so the all-time
    cache is always re-warmed before it can lapse; if it ever does, ``dashboard()``
    still recomputes it on a miss, so output is never stale-broken."""
    key = f"kb:stats:alltime:warmed:{CACHE_VERSION}:{_home()}"
    if cache.get(key) is not None:
        return False
    cache.set(key, True, int(ALLTIME_WARM_INTERVAL.total_seconds()))
    return True


def warm_caches() -> int:
    """Recompute and re-cache the public killboard read paths so visitors never
    pay a cold computation. Called on a schedule (config/celery.py) more often
    than CACHE_TTL, so the dashboard / killfeed / rankings stay warm continuously.
    """
    from .leaderboards import corp_combat_roster, leaderboards

    # Leaderboard windows first — killfeed_overview reuses the "7d" board.
    for window in WARM_WINDOWS:
        leaderboards(window, refresh=True)
    # Refresh the heavy all-time dashboard breakdowns on a slower (~15 min) cadence;
    # dashboard() then reuses that warm slice instead of recomputing it every tick.
    if _alltime_due():
        _alltime_breakdowns(refresh=True)
    dashboard(refresh=True)
    killfeed_overview(refresh=True)
    # The corp roster and the officer loss-impact board are all-time reads too;
    # keep them warm so no member/officer pays the cold recompute after a TTL lapse.
    corp_combat_roster(refresh=True)
    loss_impact_summary(refresh=True)
    return 3 + len(WARM_WINDOWS)


def loss_impact_summary(days: int = 90, *, use_cache: bool = True, refresh: bool = False) -> dict:
    """Corp-wide fit-deviation rollup for the officer loss-impact board (SRP-7):
    losses by doctrine, the most commonly-missing modules, and the pilots with the
    most deviated losses. Officer-gated at the view (individual deviations are
    sensitive). Aggregated in Python because missing/extra are JSON lists; the
    set of doctrine-tagged losses is small relative to the whole board.

    Memoized like the sibling dashboard aggregates so the officer request path never
    pays the full deviation scan. ``refresh=True`` re-caches on a hit (warmer).
    """
    from collections import Counter, defaultdict

    from .models import FitDeviation

    key = f"kb:lossimpact:{CACHE_VERSION}:{_home()}:{days}"
    if use_cache and not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached

    since = timezone.now() - timedelta(days=days)
    deviations = list(
        FitDeviation.objects.select_related("doctrine_fit__doctrine", "killmail").filter(
            killmail__killmail_time__gte=since,
            killmail__home_corp_role=Killmail.HomeRole.VICTIM,
        )
    )

    by_doctrine: dict[str, dict] = defaultdict(lambda: {"losses": 0, "deviated": 0})
    missing_counter: Counter = Counter()
    per_pilot: dict[int, dict] = defaultdict(lambda: {"losses": 0, "deviated": 0})
    for dev in deviations:
        doctrine = dev.doctrine_fit.doctrine
        name = doctrine.name if doctrine else "—"
        deviated = bool(dev.missing or dev.extra)
        by_doctrine[name]["losses"] += 1
        by_doctrine[name]["deviated"] += 1 if deviated else 0
        for module in dev.missing:
            missing_counter[module["type_id"]] += module.get("quantity", 1)
        cid = dev.killmail.victim_character_id
        if cid:
            per_pilot[cid]["losses"] += 1
            per_pilot[cid]["deviated"] += 1 if deviated else 0

    doctrines = sorted(
        ({"name": k, **v} for k, v in by_doctrine.items()), key=lambda r: r["losses"], reverse=True
    )
    most_missing = [{"type_id": tid, "count": n} for tid, n in missing_counter.most_common(12)]
    repeat_offenders = sorted(
        ({"character_id": cid, **v} for cid, v in per_pilot.items() if v["deviated"]),
        key=lambda r: (r["deviated"], r["losses"]),
        reverse=True,
    )[:10]
    payload = {
        "days": days,
        "total_losses": len(deviations),
        "total_deviated": sum(1 for d in deviations if d.missing or d.extra),
        "doctrines": doctrines,
        "most_missing": most_missing,
        "repeat_offenders": repeat_offenders,
    }
    if use_cache:
        cache.set(key, payload, CACHE_TTL)
    return payload
