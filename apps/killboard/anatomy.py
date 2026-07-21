"""KB-22 — killmail-detail anatomy: attacker breakdown, parties, related kills, value tier.

Pure helpers over data we already store, so the detail page can render damage-share bars, an
attacker-side corp/alliance breakdown, a doctrine-hull badge, same-battle links and a value
badge — all in-house, no external lookups.
"""
from __future__ import annotations

from decimal import Decimal

from django.utils.translation import gettext_lazy as _

from apps.doctrines.models import Doctrine, DoctrineFit

from .models import Killmail

# Absolute ISK value tiers (cheap, zero-query). A kill below the smallest gets no badge.
_VALUE_TIERS: list[tuple[Decimal, object]] = [
    (Decimal("100000000000"), _("100B+")),
    (Decimal("10000000000"), _("10B+")),
    (Decimal("1000000000"), _("1B+")),
]


def value_tier(total_value) -> object | None:
    """A compact value-tier label for the killmail, or ``None`` for ordinary kills."""
    if total_value is None:
        return None
    value = Decimal(total_value)
    for threshold, label in _VALUE_TIERS:
        if value >= threshold:
            return label
    return None


def doctrine_hull_ids() -> set[int]:
    """Hull ``type_id`` set of our ACTIVE doctrine fits — for the attacker doctrine badge.

    Built once per request (never per-attacker). Attacker fits aren't in a killmail, so the
    badge is hull-only: 'this home-corp pilot brought a doctrine hull'.
    """
    return {
        h for h in DoctrineFit.objects.filter(doctrine__status=Doctrine.Status.ACTIVE)
        .values_list("ship_type_id", flat=True)
        if h
    }


def attacker_breakdown(killmail: Killmail, attackers, home_corp_id: int, hull_ids: set[int]) -> dict:
    """Per-attacker rows (damage share, top-damage + doctrine-hull flags) plus a
    corp-grouped parties summary. ``attackers`` must already be ordered by ``-damage_done``.
    """
    total = killmail.damage_taken or sum(a.damage_done for a in attackers) or 0

    rows = []
    for i, a in enumerate(attackers):
        pct = (a.damage_done / total * 100) if total else 0
        rows.append({
            "character_id": a.character_id,
            "corporation_id": a.corporation_id,
            "alliance_id": a.alliance_id,
            "ship_type_id": a.ship_type_id,
            "weapon_type_id": a.weapon_type_id,
            "damage_done": a.damage_done,
            "damage_pct": round(pct, 1),
            "final_blow": a.final_blow,
            "is_top": i == 0 and a.damage_done > 0,
            # KB-33: a home-corp participant links to the internal pilot page, everyone else
            # to their adversary page (killboard/_entity_link.html reads this flag).
            "is_home": bool(home_corp_id and a.corporation_id == home_corp_id),
            "doctrine_hull": bool(
                home_corp_id and a.corporation_id == home_corp_id and a.ship_type_id in hull_ids
            ),
        })

    parties: dict = {}
    for a in attackers:
        p = parties.setdefault(a.corporation_id, {
            "corporation_id": a.corporation_id, "alliance_id": a.alliance_id,
            "is_home": bool(home_corp_id and a.corporation_id == home_corp_id),
            "pilots": 0, "damage": 0, "ships": {},
        })
        p["pilots"] += 1
        p["damage"] += a.damage_done
        if a.ship_type_id:
            p["ships"][a.ship_type_id] = p["ships"].get(a.ship_type_id, 0) + 1
    party_list = sorted(parties.values(), key=lambda p: -p["damage"])
    for p in party_list:
        p["damage_pct"] = round(p["damage"] / total * 100, 1) if total else 0
        p["top_ships"] = sorted(p["ships"].items(), key=lambda kv: -kv[1])[:4]

    return {"rows": rows, "parties": party_list}


def related_killmails(killmail: Killmail, limit: int = 6) -> list[Killmail]:
    """Other kills that share a battle report with this one (empty when it's in none)."""
    report_ids = list(killmail.battle_reports.values_list("pk", flat=True))
    if not report_ids:
        return []
    return list(
        Killmail.objects.filter(battle_reports__in=report_ids)
        .exclude(pk=killmail.pk)
        .distinct()
        .order_by("-total_value")[:limit]
    )
