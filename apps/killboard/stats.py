"""Precomputed killboard stat rollups (read-fast summaries).

``CombatMetric`` is a precomputed table so future surfaces (member tables, per
-ship stat pages, API) can read combat aggregates in O(1) instead of scanning
the killmail history. The live dashboards (``analytics.py``) still compute on
demand + cache; this rollup is the scheduled, precomputed foundation.

All metrics are PvP-only (NPC ratting/structure kills excluded), consistent with
the leaderboards and the analytics dashboards.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from .leaderboards import window_for
from .models import CombatMetric, Killmail

_WINDOWS = {"7d": 7, "30d": 30, "all": None}

_ATTACKER = Killmail.HomeRole.ATTACKER
_VICTIM = Killmail.HomeRole.VICTIM


def _type_names(ids: list[int]) -> dict[int, str]:
    from apps.sde.models import SdeType

    return dict(SdeType.objects.filter(type_id__in=ids).values_list("type_id", "name"))


def _system_names(ids: list[int]) -> dict[int, str]:
    from apps.sde.models import SdeSolarSystem

    return dict(SdeSolarSystem.objects.filter(system_id__in=ids).values_list("system_id", "name"))


def _top_ships(kills_qs, limit: int = 5) -> list[dict]:
    rows = list(
        kills_qs.values("victim_ship_type_id")
        .annotate(n=Count("killmail_id"))
        .order_by("-n")[:limit]
    )
    names = _type_names([r["victim_ship_type_id"] for r in rows])
    return [
        {"type_id": r["victim_ship_type_id"],
         "name": names.get(r["victim_ship_type_id"], f"Type {r['victim_ship_type_id']}"),
         "count": r["n"]}
        for r in rows
    ]


def _top_systems(union_qs, limit: int = 5) -> list[dict]:
    rows = list(
        union_qs.values("solar_system_id")
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


def rebuild_corp_metrics() -> int:
    """Recompute the home corporation's CombatMetric rows across windows."""
    home = settings.FORCA_HOME_CORP_ID
    if not home:
        return 0
    count = 0
    for window, days in _WINDOWS.items():
        base = Killmail.objects.filter(involves_home_corp=True, is_npc=False)
        if days is not None:
            base = base.filter(killmail_time__gte=timezone.now() - timedelta(days=days))
        kills = base.filter(home_corp_role=_ATTACKER)
        losses = base.filter(home_corp_role=_VICTIM)
        n_kills = kills.count()
        n_losses = losses.count()
        solo = kills.filter(is_solo=True).count()
        avg_gang = (
            kills.annotate(ac=Count("participants", filter=Q(participants__role="attacker")))
            .aggregate(a=Avg("ac"))["a"]
            or 0.0
        )
        total_fights = n_kills + n_losses
        CombatMetric.objects.update_or_create(
            entity_type=CombatMetric.EntityType.CORPORATION,
            entity_id=home,
            window=window,
            defaults={
                "kills": n_kills,
                "losses": n_losses,
                "isk_destroyed": kills.aggregate(s=Sum("total_value"))["s"] or 0,
                "isk_lost": losses.aggregate(s=Sum("total_value"))["s"] or 0,
                "points": kills.aggregate(s=Sum("points"))["s"] or 0,
                "solo_kills": solo,
                "danger_ratio": (n_kills / total_fights) if total_fights else 0.0,
                "gang_ratio": ((n_kills - solo) / n_kills) if n_kills else 0.0,
                "avg_gang_size": round(float(avg_gang), 2),
                "top_ships": _top_ships(kills),
                "top_systems": _top_systems(base),
                "as_of": timezone.now(),
            },
        )
        count += 1
    return count


def rebuild_member_metrics() -> int:
    """Recompute all-time per-pilot CombatMetric rows for home-corp members.

    Computed in a single aggregation pass over the home corp's killmails (reuses
    the leaderboard merge), so this stays cheap regardless of member count.
    """
    home = settings.FORCA_HOME_CORP_ID
    if not home:
        return 0
    from .leaderboards import _merge_pilots

    pilots = _merge_pilots(window_for("all"))
    count = 0
    for cid, p in pilots.items():
        kills = p.get("kills", 0) or 0
        losses = p.get("losses", 0) or 0
        total = kills + losses
        CombatMetric.objects.update_or_create(
            entity_type=CombatMetric.EntityType.CHARACTER,
            entity_id=cid,
            window="all",
            defaults={
                "kills": kills,
                "losses": losses,
                "isk_destroyed": p.get("isk_destroyed", 0) or 0,
                "isk_lost": p.get("isk_lost", 0) or 0,
                "points": p.get("points", 0) or 0,
                "solo_kills": p.get("solo_kills", 0) or 0,
                "final_blows": p.get("final_blows", 0) or 0,
                "danger_ratio": (kills / total) if total else 0.0,
                "as_of": timezone.now(),
            },
        )
        count += 1
    # Prune pilots who no longer have any record (left the corp / data wiped).
    fresh_ids = set(pilots.keys())
    stale = CombatMetric.objects.filter(
        entity_type=CombatMetric.EntityType.CHARACTER, window="all"
    ).exclude(entity_id__in=fresh_ids)
    stale.delete()
    return count
