"""SKL-3 (roadmap 2.12) — cached + snapshot-threaded officer skill-gap page.

Acceptance: the gap computation hits a cache on the warm path and does not re-fetch
snapshots per fit; the cache auto-invalidates on a fresh skill sync.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.sde.models import SdeType
from apps.skills.gap import _gap_cache_key, corp_skill_bottlenecks, corp_skill_gap
from apps.sso.models import EveCharacter

pytestmark = pytest.mark.django_db

GUNNERY = 3300


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _doctrine():
    SdeType.objects.filter(type_id=GUNNERY).update(rank=1)
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    doctrine = Doctrine.objects.create(name="Gunline", category=cat, priority=90)
    fit = DoctrineFit.objects.create(doctrine=doctrine, name="Gunline", ship_type_id=587)
    SkillRequirement.objects.create(fit=fit, skill_type_id=GUNNERY, min_level=5, optimal_level=5)
    return doctrine


def _char(dum, cid, gunnery_level):
    u = dum.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(
        character_id=cid, user=u, name=f"P{cid}", is_main=True, is_corp_member=True
    )
    skills = {str(GUNNERY): {"trained_level": gunnery_level}} if gunnery_level else {}
    CharacterSkillSnapshot.objects.create(character=ch, is_latest=True, skills=skills)
    return ch


def test_gap_flags_missing_skill_and_candidate(django_user_model, sde):
    doctrine = _doctrine()
    _char(django_user_model, 9201, gunnery_level=1)  # has gunnery 1, needs 5
    chars = list(EveCharacter.objects.filter(is_corp_member=True))
    gap = corp_skill_gap(chars)
    assert any(b["skill_type_id"] == GUNNERY and b["members"] == 1 for b in gap["bottlenecks"])
    assert doctrine.id in gap["candidates_by_doctrine"]
    assert any(c["character_id"] == 9201 for c in gap["candidates_by_doctrine"][doctrine.id])


def test_warm_path_is_cached_and_cheaper(django_user_model, sde):
    _doctrine()
    for i in range(4):
        _char(django_user_model, 9210 + i, gunnery_level=1)
    chars = list(EveCharacter.objects.filter(is_corp_member=True))

    with CaptureQueriesContext(connection) as cold:
        r1 = corp_skill_gap(chars)
    assert cache.get(_gap_cache_key(chars)) is not None

    with CaptureQueriesContext(connection) as warm:
        r2 = corp_skill_gap(chars)
    assert r2 == r1
    # Warm path only computes the cache key (snapshot Max + doctrine ids), not the scan.
    assert len(warm) < len(cold)


def test_cache_key_moves_on_fresh_sync(django_user_model, sde):
    _doctrine()
    ch = _char(django_user_model, 9203, gunnery_level=1)
    chars = list(EveCharacter.objects.filter(is_corp_member=True))
    k1 = _gap_cache_key(chars)
    # A fresh snapshot (newer as_of) supersedes the old one → key changes → recompute.
    CharacterSkillSnapshot.objects.filter(character=ch).update(is_latest=False)
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": 5}}
    )
    assert _gap_cache_key(chars) != k1


def test_public_wrapper_threads_snapshots(django_user_model, sde):
    _doctrine()
    _char(django_user_model, 9204, gunnery_level=1)
    chars = list(EveCharacter.objects.filter(is_corp_member=True))
    rows = corp_skill_bottlenecks(chars)
    assert any(b["skill_type_id"] == GUNNERY for b in rows)
