"""KB-37 (WS-D3) — the per-pilot PVP CV: the pilot's whole combat identity on one page.

Composes the pieces other workstreams already own — the combat card + rank progression (ranks),
support-role tracks, earned trophies + progress toward the next (trophies), newbro milestones
(milestones), Kill-of-the-Week mentions (kotw), season placements (seasons) — plus a few
signature stats (best kill, favourite hull, a role distribution via roles.py). Everything is
bounded and read-only; the page is member-gated in the view.
"""
from __future__ import annotations

from collections import defaultdict

from django.conf import settings

from . import kotw, ranks, roles, trophies
from .models import Killmail, KillmailParticipant, SeasonSnapshot
from .valuation import at_kill_value_expr

_ATTACKER = KillmailParticipant.Role.ATTACKER


def _home() -> int:
    return settings.FORCA_HOME_CORP_ID


def _best_kill(character_id: int) -> dict | None:
    """The pilot's single most valuable kill (at-kill value), with a little context."""
    row = (
        KillmailParticipant.objects.filter(
            role=_ATTACKER, corporation_id=_home(), character_id=character_id,
            killmail__home_corp_role=Killmail.HomeRole.ATTACKER, killmail__is_npc=False,
        )
        .annotate(at_kill=at_kill_value_expr("killmail__"))
        .order_by("-at_kill")
        .values("killmail_id", "at_kill", "killmail__victim_ship_type_id",
                "killmail__solar_system_id", "killmail__killmail_time")
        .first()
    )
    if not row:
        return None
    return {
        "killmail_id": row["killmail_id"],
        "value": row["at_kill"] or 0,
        "victim_ship_type_id": row["killmail__victim_ship_type_id"],
        "solar_system_id": row["killmail__solar_system_id"],
        "killmail_time": row["killmail__killmail_time"],
    }


def _favourite_hull(character_id: int) -> dict | None:
    """The hull the pilot flew most across their home kills (top ship_type_id by count)."""
    from django.db.models import Count

    row = (
        KillmailParticipant.objects.filter(
            role=_ATTACKER, corporation_id=_home(), character_id=character_id,
            ship_type_id__isnull=False,
            killmail__home_corp_role=Killmail.HomeRole.ATTACKER, killmail__is_npc=False,
        )
        .values("ship_type_id")
        .annotate(n=Count("killmail_id", distinct=True))
        .order_by("-n")
        .first()
    )
    if not row:
        return None
    return {"ship_type_id": row["ship_type_id"], "count": row["n"]}


def _role_distribution(character_id: int) -> list[dict]:
    """A coarse role mix over the hulls the pilot flew on kills (attacker hull approximation).

    Attacker rows carry only a hull, so this is the WS-D2 hull-based approximation (logi/capital
    inferable; other support roles fall to dps) — honest about its limits and cheap. Returns
    ``[{"role", "label", "count"}]`` ordered by ``roles.ROLE_ORDER``, non-zero only.
    """
    from django.db.models import Count

    ship_counts = (
        KillmailParticipant.objects.filter(
            role=_ATTACKER, corporation_id=_home(), character_id=character_id,
            ship_type_id__isnull=False,
            killmail__home_corp_role=Killmail.HomeRole.ATTACKER, killmail__is_npc=False,
        )
        .values("ship_type_id")
        .annotate(n=Count("killmail_id", distinct=True))
    )
    ship_counts = list(ship_counts)
    if not ship_counts:
        return []
    meta = roles._ship_group_meta({r["ship_type_id"] for r in ship_counts})
    tally: dict[str, int] = defaultdict(int)
    for r in ship_counts:
        gid, gname = meta.get(r["ship_type_id"], (None, ""))
        tally[roles.attacker_role(gid, gname)] += r["n"]
    return [
        {"role": role, "label": roles.ROLE_LABELS[role], "count": tally[role]}
        for role in roles.ROLE_ORDER if tally.get(role)
    ]


def _season_placements(character_id: int) -> list[dict]:
    """The season boards where the pilot placed top-3 (from the persisted snapshots)."""
    from .seasons import BOARD_KEYS, board_label

    out = []
    for snap in SeasonSnapshot.objects.all().order_by("-year", "-quarter"):
        for key in BOARD_KEYS:
            for row in snap.boards.get(key, []):
                if row.get("character_id") == character_id:
                    out.append({
                        "year": snap.year, "quarter": snap.quarter,
                        "board": key, "board_label": board_label(key),
                        "place": row.get("place"), "value": row.get("value"),
                    })
    return out


def pilot_cv(character_id: int) -> dict:
    """Assemble the full PVP CV payload for one pilot (bounded, read-only)."""
    from .leaderboards import pilot_combat_card
    from .milestones import milestones_for

    card = pilot_combat_card(character_id)
    counts = ranks.pilot_metric_counts(character_id)
    return {
        "character_id": character_id,
        "card": card,
        "rank_progress": ranks.rank_progress(counts["kills"]),
        "tracks": ranks.pilot_track_standings(counts),
        "trophies": trophies.pilot_trophies(character_id),
        "trophy_progress": trophies.trophy_progress_toward_next(character_id),
        "milestones": milestones_for([character_id]),
        "kotw": kotw.kotw_for_character(character_id),
        "seasons": _season_placements(character_id),
        "best_kill": _best_kill(character_id),
        "favourite_hull": _favourite_hull(character_id),
        "role_distribution": _role_distribution(character_id),
    }
