"""Skill-gap intelligence: leverage, bottlenecks, fastest candidates.

Builds doctrines with explicit skill requirements and a character with a known
snapshot, so the two-sided analysis is fully deterministic.
"""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.skills.gap import (
    corp_skill_bottlenecks,
    fastest_candidates_for_doctrine,
    highest_leverage_skill,
)
from apps.sso.models import EveCharacter

GUNNERY = 3300
SMALL_HYBRID = 3301


def _doctrine(name, priority, reqs):
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    doctrine = Doctrine.objects.create(name=name, category=cat, priority=priority)
    fit = DoctrineFit.objects.create(doctrine=doctrine, name=name, ship_type_id=587)
    for skill_id, level in reqs.items():
        SkillRequirement.objects.create(
            fit=fit, skill_type_id=skill_id, min_level=level, optimal_level=level
        )
    return doctrine


def _character(user_model, name, cid, skills):
    user = user_model.objects.create(username=f"eve:{cid}")
    char = EveCharacter.objects.create(
        character_id=cid, user=user, name=name, is_main=True, is_corp_member=True
    )
    CharacterSkillSnapshot.objects.create(
        character=char,
        is_latest=True,
        skills={str(k): {"trained_level": v, "sp": 0} for k, v in skills.items()},
    )
    return char


@pytest.mark.django_db
def test_highest_leverage_prefers_skill_unlocking_most_doctrines(django_user_model, sde):
    # Gunnery is needed by BOTH doctrines; small hybrid only by the second.
    d1 = _doctrine("Primary DPS", 90, {GUNNERY: 3})
    _doctrine("Secondary DPS", 50, {GUNNERY: 4, SMALL_HYBRID: 2})
    char = _character(django_user_model, "Gunner", 2001, {})

    best = highest_leverage_skill(char)
    assert best is not None
    assert best["skill_type_id"] == GUNNERY  # advances 2 doctrines, beats the 1-doctrine skill
    assert best["doctrine_count"] == 2
    assert best["target_level"] == 4  # max level demanded across doctrines
    assert best["top_doctrine"]["id"] == d1.id  # highest-priority doctrine it advances


@pytest.mark.django_db
def test_highest_leverage_none_when_nothing_to_train(django_user_model, sde):
    _doctrine("Primary DPS", 90, {GUNNERY: 3})
    char = _character(django_user_model, "Maxed", 2002, {GUNNERY: 5})
    assert highest_leverage_skill(char) is None


@pytest.mark.django_db
def test_corp_bottlenecks_count_blocked_members(django_user_model, sde):
    _doctrine("Primary DPS", 90, {GUNNERY: 3, SMALL_HYBRID: 2})
    a = _character(django_user_model, "A", 2003, {})           # missing both
    b = _character(django_user_model, "B", 2004, {SMALL_HYBRID: 5})  # missing only gunnery

    rows = {r["skill_type_id"]: r for r in corp_skill_bottlenecks([a, b])}
    assert rows[GUNNERY]["members"] == 2
    assert rows[SMALL_HYBRID]["members"] == 1


@pytest.mark.django_db
def test_fastest_candidates_excludes_ready_and_unknown(django_user_model, sde):
    doctrine = _doctrine("Logi", 80, {GUNNERY: 4})
    near = _character(django_user_model, "Near", 2005, {GUNNERY: 2})   # not ready → candidate
    ready = _character(django_user_model, "Ready", 2006, {GUNNERY: 5})  # already flies → excluded
    # Unknown: a corp character with no snapshot.
    unknown_user = django_user_model.objects.create(username="eve:2007")
    EveCharacter.objects.create(
        character_id=2007, user=unknown_user, name="Unknown", is_corp_member=True
    )
    unknown = EveCharacter.objects.get(character_id=2007)

    candidates = fastest_candidates_for_doctrine(doctrine, [near, ready, unknown])
    ids = [c["character_id"] for c in candidates]
    assert 2005 in ids       # near-ready pilot is a candidate
    assert 2006 not in ids   # already flies → not a candidate
    assert 2007 not in ids   # unknown (no import) → not a candidate
    assert candidates[0]["seconds"] > 0
