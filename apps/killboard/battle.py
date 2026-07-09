"""Battle-report generation: group killmails in a system+window into sides.

A battle report aggregates every killmail in one solar system over a time window
into per-corporation sides (kills, losses, ISK lost) plus a destroyed-ship
breakdown — the after-action view officers want without hand-counting a feed.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .models import BattleReport, Killmail, KillmailParticipant


def generate_battle_report(system_id: int, hours: int = 24, title: str = "") -> BattleReport | None:
    """Build (and persist) a battle report for one system over the last ``hours``."""
    end = timezone.now()
    start = end - timedelta(hours=hours)
    kms = list(
        Killmail.objects.filter(solar_system_id=system_id, killmail_time__gte=start, killmail_time__lte=end)
    )
    if not kms:
        return None

    km_ids = [k.killmail_id for k in kms]
    parts = KillmailParticipant.objects.filter(killmail_id__in=km_ids)

    sides: dict[int, dict] = {}

    def _side(corp_id):
        return sides.setdefault(
            corp_id, {"corporation_id": corp_id, "kills": 0, "losses": 0, "isk_lost": Decimal("0")}
        )

    ship_breakdown: dict[int, int] = {}
    for km in kms:
        ship_breakdown[km.victim_ship_type_id] = ship_breakdown.get(km.victim_ship_type_id, 0) + 1
        if km.victim_corporation_id:
            side = _side(km.victim_corporation_id)
            side["losses"] += 1
            side["isk_lost"] += km.total_value or Decimal("0")
    # Attacker kills, deduped per (corp, killmail) so a 50-pilot gang counts once.
    seen = set()
    for p in parts.filter(role=KillmailParticipant.Role.ATTACKER):
        if not p.corporation_id:
            continue
        key = (p.corporation_id, p.killmail_id)
        if key in seen:
            continue
        seen.add(key)
        _side(p.corporation_id)["kills"] += 1

    ranked = sorted(sides.values(), key=lambda s: s["isk_lost"], reverse=True)
    isk_by_side = {str(s["corporation_id"]): str(s["isk_lost"]) for s in ranked}

    from apps.sde.models import SdeSolarSystem

    system_name = (
        SdeSolarSystem.objects.filter(system_id=system_id).values_list("name", flat=True).first()
        or f"system {system_id}"
    )
    report = BattleReport.objects.create(
        title=title or f"Battle in {system_name}",
        system_ids=[system_id],
        start_time=min(k.killmail_time for k in kms),
        end_time=max(k.killmail_time for k in kms),
        sides={"corporations": [{**s, "isk_lost": str(s["isk_lost"])} for s in ranked]},
        isk_destroyed_by_side=isk_by_side,
        ship_breakdown={str(t): n for t, n in sorted(ship_breakdown.items(), key=lambda x: -x[1])},
    )
    report.killmails.set(km_ids)
    return report
