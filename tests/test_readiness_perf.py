"""Performance guards for compute_readiness (leadership dashboard / briefing)."""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.sso.models import EveCharacter

RIFTER = 587


def _char(cid):
    c = EveCharacter.objects.create(character_id=cid, name=f"P{cid}", is_corp_member=True)
    CharacterSkillSnapshot.objects.create(
        character=c, is_latest=True, skills={"3300": {"trained_level": 5}},
    )
    return c


def _doctrine(name, fits=2):
    cat = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")[0]
    d = Doctrine.objects.create(name=name, category=cat, status=Doctrine.Status.ACTIVE, priority=10)
    for i in range(fits):
        fit = DoctrineFit.objects.create(doctrine=d, name=f"{name}-{i}", ship_type_id=RIFTER)
        SkillRequirement.objects.create(fit=fit, skill_type_id=3300, min_level=3, optimal_level=5)
    return d


@pytest.mark.django_db
def test_compute_readiness_query_count_does_not_scale(django_assert_max_num_queries, sde):
    # Several members × doctrines × fits — the snapshot is preloaded once, so the
    # query count must stay small and roughly flat, not one-per-(member,fit).
    for cid in range(1001, 1007):       # 6 members
        _char(cid)
    for n in range(4):                   # 4 doctrines × 2 fits = 8 fits
        _doctrine(f"Doc{n}")

    from apps.readiness.services import compute_readiness

    # 6×4×2 = 48 readiness evaluations; old code did ~2 queries each (~96+). The budget
    # was raised 15→20 (leadership-approved) to admit the Gap-B KPIs that read a little
    # more (per-doctrine classification, 30-day PvP participation) — still flat, not
    # per-(member,fit).
    with django_assert_max_num_queries(20):
        result = compute_readiness(use_cache=False)
    assert result["index"] is not None


@pytest.mark.django_db
def test_compute_readiness_is_cached(django_assert_num_queries, sde):
    _char(2001)
    _doctrine("Doc")
    from apps.readiness.services import compute_readiness

    first = compute_readiness()           # computes + caches
    with django_assert_num_queries(0):    # second call is a pure cache read
        second = compute_readiness()
    assert second == first
