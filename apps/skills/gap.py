"""Skill-gap intelligence: two-sided analysis over skills × doctrines.

Pilot side: ``highest_leverage_skill`` answers "train this one skill and you
move toward the most corp-needed doctrines". Leadership side:
``corp_skill_bottlenecks`` and ``fastest_candidates_for_doctrine`` answer "what
is blocking the most pilots" and "who can we train into this role fastest".

Roles are modelled as active doctrines here (a doctrine *is* a capability the
corp wants); a dedicated FleetRole abstraction is future work (PRD §II.5.3).
"""
from __future__ import annotations

import hashlib

from apps.doctrines.models import Doctrine
from apps.sde.models import SdeType

from .services import (
    SP_PER_HOUR,
    _ranks,
    collect_missing_for_doctrine,
    estimate_seconds_to_doctrine,
    sp_between_levels,
)


def _active_doctrines():
    return (
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .prefetch_related("fits__skill_requirements")
        .order_by("-priority", "name")
    )


def _names(skill_ids) -> dict[int, str]:
    return dict(
        SdeType.objects.filter(type_id__in=list(skill_ids)).values_list("type_id", "name")
    )


def highest_leverage_skill(character) -> dict | None:
    """The single skill that best advances this pilot toward corp doctrines.

    Leverage = (doctrines advanced × summed doctrine priority) ÷ training time.
    Returns ``None`` when the pilot can already fly everything (or has no skill
    import / known requirements to reason about).
    """
    snapshot = character.skill_snapshots.filter(is_latest=True).first()
    if snapshot is None:
        return None

    # skill_id -> aggregate of the doctrines it advances. Doctrines are walked
    # in priority order, so the first one seen for a skill is its top doctrine.
    agg: dict[int, dict] = {}
    for doctrine in _active_doctrines():
        missing = collect_missing_for_doctrine(character, doctrine)
        for sid, level in missing.items():
            entry = agg.setdefault(
                sid, {"target": 0, "doctrines": [], "priority": 0, "top": None}
            )
            entry["target"] = max(entry["target"], level)
            entry["doctrines"].append(doctrine.name)
            entry["priority"] += doctrine.priority or 0
            if entry["top"] is None:
                entry["top"] = {"id": doctrine.id, "name": doctrine.name}
    if not agg:
        return None

    ranks = _ranks(set(agg))
    names = _names(set(agg))
    best = None
    for sid, entry in agg.items():
        have = snapshot.trained_level(sid)
        sp = sp_between_levels(ranks.get(sid, 1), have, entry["target"])
        seconds = max(int(sp / SP_PER_HOUR * 3600), 1)
        count = len(entry["doctrines"])
        # Impact first: the skill that advances the most corp-need-weighted
        # doctrines. Training time is a tiebreaker (quick wins are already shown
        # by the "closest doctrines" card), not the driver.
        impact = count * max(entry["priority"], 1)
        cand = {
            "skill_type_id": sid,
            "name": names.get(sid, f"Skill {sid}"),
            "target_level": entry["target"],
            "have": have,
            "seconds": seconds,
            "advances": entry["doctrines"],
            "doctrine_count": count,
            "top_doctrine": entry["top"],
            "impact": impact,
        }
        if best is None or (impact, -seconds) > (best["impact"], -best["seconds"]):
            best = cand
    return best


# SKL-3 (2.12): the officer skill-gap page ran O(members × doctrines × fits) and
# re-fetched each character's skill snapshot per fit. Load every character's latest
# snapshot ONCE and thread it through (mirrors closest_doctrines / the briefing), and
# cache the whole page result so the warm path is instant and doesn't re-scan skills.


def _snapshots_for(characters) -> dict:
    """Each character's latest skill snapshot, loaded once, keyed by character_id."""
    from apps.characters.models import CharacterSkillSnapshot

    return {
        s.character_id: s
        for s in CharacterSkillSnapshot.objects.filter(character__in=characters, is_latest=True)
    }


def _bottlenecks(characters, doctrines, snaps, limit: int = 10) -> list[dict]:
    counts: dict[int, int] = {}
    for character in characters:
        snap = snaps.get(character.character_id)
        if snap is None:
            continue
        missing_for_member: set[int] = set()
        for doctrine in doctrines:
            missing_for_member.update(
                collect_missing_for_doctrine(character, doctrine, snapshot=snap)
            )
        for sid in missing_for_member:
            counts[sid] = counts.get(sid, 0) + 1
    if not counts:
        return []
    names = _names(set(counts))
    rows = [
        {"skill_type_id": sid, "name": names.get(sid, f"Skill {sid}"), "members": n}
        for sid, n in counts.items()
    ]
    rows.sort(key=lambda r: (-r["members"], r["name"]))
    return rows[:limit]


def _candidates(doctrine, characters, snaps, attrs_by_pk, limit: int = 8) -> list[dict]:
    from apps.doctrines.services import character_readiness

    fits = list(doctrine.fits.all())
    rows = []
    for character in characters:
        snap = snaps.get(character.character_id)
        readies = [character_readiness(character, fit, snapshot=snap) for fit in fits]
        if not readies or any(r.status in ("viable", "optimal") for r in readies):
            continue
        if all(r.status == "unknown" for r in readies):
            continue
        seconds = estimate_seconds_to_doctrine(
            character, doctrine, snapshot=snap, attrs=attrs_by_pk.get(character.pk)
        )
        if seconds <= 0:
            continue
        rows.append({
            "character_id": character.character_id,
            "name": character.name,
            "seconds": seconds,
        })
    rows.sort(key=lambda r: r["seconds"])
    return rows[:limit]


def corp_skill_bottlenecks(characters, limit: int = 10) -> list[dict]:
    """Skills blocking the most members across active doctrines (snapshot-threaded).

    A skill counts once per member who is missing it for any active doctrine.
    """
    return _bottlenecks(characters, list(_active_doctrines()), _snapshots_for(characters), limit)


def _attrs_for(characters) -> dict:
    from apps.characters.models import CharacterAttributes

    return {a.character_id: a for a in CharacterAttributes.objects.filter(character__in=characters)}


def fastest_candidates_for_doctrine(doctrine: Doctrine, characters, limit: int = 8) -> list[dict]:
    """Members who can't fly the doctrine yet, ranked by ascending train time
    (snapshot-threaded). Excludes already-viable pilots and those with no import."""
    return _candidates(doctrine, characters, _snapshots_for(characters), _attrs_for(characters), limit)


_GAP_CACHE_VERSION = 1
_GAP_TTL = 900  # 15 min — member skills sync at most every 12h, so this stays fresh.


def _gap_cache_key(characters) -> str:
    """A key that changes on a fresh member skill sync, a change to the active-doctrine
    set, or a roster change — so the cache self-invalidates without an explicit hook (a
    fit-content edit within an unchanged doctrine set self-heals within the TTL, matching
    closest_doctrines)."""
    from django.db.models import Max

    from apps.characters.models import CharacterSkillSnapshot

    member_ids = sorted(c.character_id for c in characters)
    # Scope the freshness stamp to THESE members — a non-member's sync must not invalidate.
    latest = CharacterSkillSnapshot.objects.filter(
        is_latest=True, character_id__in=member_ids
    ).aggregate(m=Max("as_of"))["m"]
    doc_ids = sorted(_active_doctrines().values_list("id", flat=True))
    sig = hashlib.sha256(f"{member_ids}|{doc_ids}".encode()).hexdigest()[:16]
    # Microsecond precision so any fresh sync (a new latest snapshot) moves the key.
    ts = int(latest.timestamp() * 1_000_000) if latest else 0
    return f"skills:gap:{_GAP_CACHE_VERSION}:{ts}:{sig}"


def corp_skill_gap(characters) -> dict:
    """Cached corp-wide skill-gap: bottlenecks + fastest candidates per active doctrine.

    Snapshots (and attributes) are loaded once and threaded through — nothing re-fetches
    per fit — and the whole result is cached, auto-invalidating when the key moves. Returns
    ``candidates_by_doctrine`` keyed by doctrine id so the caller re-attaches the Doctrine
    objects cheaply, keeping heavy ORM instances out of the cache.
    """
    from django.core.cache import cache

    key = _gap_cache_key(characters)
    cached = cache.get(key)
    if cached is not None:
        return cached

    snaps = _snapshots_for(characters)
    attrs_by_pk = _attrs_for(characters)
    doctrines = list(_active_doctrines())
    result = {
        "bottlenecks": _bottlenecks(characters, doctrines, snaps),
        "candidates_by_doctrine": {
            d.id: rows
            for d in doctrines
            if (rows := _candidates(d, characters, snaps, attrs_by_pk))
        },
    }
    cache.set(key, result, _GAP_TTL)
    return result
