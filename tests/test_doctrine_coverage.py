"""DOC-2 (roadmap 2.5) — cached corp-wide doctrine coverage dashboard.

Acceptance: a per-doctrine optimal/viable/not-ready pilot count, priority doctrines
highlighted, cached to avoid the O(doctrines×fits×members) recompute per request.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.doctrines.services import _coverage_cache_key, corp_doctrine_coverage
from apps.sde.models import SdeType
from apps.sso.models import EveCharacter
from core import rbac
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db

GUNNERY = 3300


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _doctrine(priority=90):
    SdeType.objects.filter(type_id=GUNNERY).update(rank=1)
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    doctrine = Doctrine.objects.create(name="Gunline", category=cat, priority=priority)
    fit = DoctrineFit.objects.create(doctrine=doctrine, name="Gunline", ship_type_id=587)
    SkillRequirement.objects.create(fit=fit, skill_type_id=GUNNERY, min_level=3, optimal_level=5)
    return doctrine


def _char(dum, cid, gunnery_level):
    u = dum.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(
        character_id=cid, user=u, name=f"P{cid}", is_main=True, is_corp_member=True
    )
    if gunnery_level is not None:
        CharacterSkillSnapshot.objects.create(
            character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": gunnery_level}}
        )
    return ch


def test_coverage_counts_by_status(django_user_model, sde):
    doctrine = _doctrine()
    _char(django_user_model, 1, gunnery_level=5)     # >= optimal → optimal
    _char(django_user_model, 2, gunnery_level=4)     # >= min, < optimal → viable
    _char(django_user_model, 3, gunnery_level=1)     # < min → not_ready
    _char(django_user_model, 4, gunnery_level=None)  # no snapshot → unknown
    chars = list(EveCharacter.objects.filter(is_corp_member=True))
    rows = corp_doctrine_coverage(chars)
    row = next(r for r in rows if r["doctrine_id"] == doctrine.id)
    assert (row["optimal"], row["viable"], row["not_ready"], row["unknown"]) == (1, 1, 1, 1)
    assert row["can_fly"] == 2
    assert row["total"] == 4
    assert row["priority"] == 90


def test_coverage_is_cached_and_cheaper(django_user_model, sde):
    _doctrine()
    for i in range(4):
        _char(django_user_model, 10 + i, gunnery_level=5)
    chars = list(EveCharacter.objects.filter(is_corp_member=True))
    with CaptureQueriesContext(connection) as cold:
        r1 = corp_doctrine_coverage(chars)
    assert cache.get(_coverage_cache_key(chars)) is not None
    with CaptureQueriesContext(connection) as warm:
        r2 = corp_doctrine_coverage(chars)
    assert r2 == r1
    assert len(warm) < len(cold)


def test_cache_key_moves_on_fresh_sync(django_user_model, sde):
    _doctrine()
    ch = _char(django_user_model, 20, gunnery_level=1)
    chars = list(EveCharacter.objects.filter(is_corp_member=True))
    k1 = _coverage_cache_key(chars)
    CharacterSkillSnapshot.objects.filter(character=ch).update(is_latest=False)
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": 5}}
    )
    assert _coverage_cache_key(chars) != k1


def test_dashboard_renders_for_officer(client, django_user_model, sde):
    _doctrine()
    _char(django_user_model, 30, gunnery_level=5)
    user, _ = enrol_pilot(django_user_model, 9900, roles=(rbac.ROLE_OFFICER,))
    client.force_login(user)
    r = client.get(reverse("doctrines:coverage"))
    assert r.status_code == 200
    assert b"Gunline" in r.content and b"Doctrine coverage" in r.content


def test_dashboard_forbidden_for_member(client, django_user_model):
    user, _ = enrol_pilot(django_user_model, 9901, roles=(rbac.ROLE_MEMBER,))
    client.force_login(user)
    r = client.get(reverse("doctrines:coverage"))
    assert r.status_code in (302, 403)
