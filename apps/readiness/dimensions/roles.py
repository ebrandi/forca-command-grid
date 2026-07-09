"""Shared strategic-role qualification (used by leadership/strategic/fleet_comp).

Counts how many corp members qualify for a ``StrategicRoleTarget`` by its
``detection`` method. Only ``skills`` detection can be auto-counted today (against
each member's latest skill snapshot); ``manual``/``asset`` roles have a target but
no automatic source, so their qualified count is reported as *unknown* (``None``) —
the honest-score rule: a role we can't measure is excluded, never scored zero.
"""
from __future__ import annotations


def active_member_ids(days: int = 30) -> set[int]:
    """Corp members who logged in within ``days`` — the availability signal for the
    availability-aware fleet simulator (4.11).

    This is a constructive **activity** signal, not presence surveillance: it exposes
    only an aggregate "who's recently been online" set (never a per-pilot location/route/
    time), used to answer "who's realistically likely to show up" rather than the
    theoretical roster maximum. Members we have no membertracking login for are treated
    as not-recently-active (they simply don't count toward the *available* figure).
    """
    import datetime as dt

    from django.utils import timezone

    from apps.corporation.models import CorpMember

    cutoff = timezone.now() - dt.timedelta(days=max(1, days))
    return set(
        CorpMember.objects.filter(logon_date__gte=cutoff).values_list("character_id", flat=True)
    )


def qualified_count(role_target, only_char_ids: set[int] | None = None) -> int | None:
    """How many corp members meet a role target, or ``None`` if not auto-detectable.

    ``only_char_ids`` restricts the count to that set of characters (the availability-
    aware mode passes the recently-active set); ``None`` counts the whole corp roster.
    """
    from apps.characters.models import CharacterSkillSnapshot
    from apps.sso.models import EveCharacter

    if role_target.detection != role_target.Detection.SKILLS:
        return None  # manual/asset — target known, actual not auto-countable
    skills = (role_target.detection_params or {}).get("skills") or {}
    if not skills:
        return None
    char_ids = list(
        EveCharacter.objects.filter(is_corp_member=True).values_list("character_id", flat=True)
    )
    if only_char_ids is not None:
        char_ids = [c for c in char_ids if c in only_char_ids]
    snaps = CharacterSkillSnapshot.objects.filter(is_latest=True, character_id__in=char_ids)
    needed = {int(k): int(v) for k, v in skills.items()}
    count = 0
    for snap in snaps:
        if all(snap.trained_level(skill_id) >= level for skill_id, level in needed.items()):
            count += 1
    return count


def role_score(role_target):
    """``(qualified, score)`` for a role target — ``min(qualified, desired)/desired``.

    Returns ``(None, None)`` when the role isn't auto-detectable or has no target.
    """
    from ..engine.base import ratio_score

    desired = role_target.desired_count or 0
    if not desired:
        return None, None
    qualified = qualified_count(role_target)
    if qualified is None:
        return None, None
    return qualified, ratio_score(min(qualified, desired), desired)
