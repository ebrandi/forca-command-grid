"""Regression tests for the 2026-07 performance pass.

Each test pins the *behaviour that could regress if the optimisation is reverted or broken*:
- hot-path role-rank memoisation issues one role query, not N (and is not stale after a change);
- the access caches (home-corp name, service id-sets) are cached AND invalidated on write;
- the universe map is cached;
- doctrine_coverage's query count is constant in the number of characters (the critical N+1).

See docs/performance/PERFORMANCE_OPTIMISATION_PLAN.md.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


def _count(ctx, needle: str) -> int:
    return sum(1 for q in ctx.captured_queries if needle in q["sql"].lower())


# --- 0.1 role-rank memoisation ------------------------------------------------
@pytest.mark.django_db
def test_role_rank_memoised_to_one_query(django_user_model):
    u = django_user_model.objects.create(username="eve:perf-rank")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_OFFICER))
    u = django_user_model.objects.get(pk=u.pk)  # fresh instance, no memo
    with CaptureQueriesContext(connection) as ctx:
        # 5 checks — the context processor + 2 middlewares do ~this many per request.
        assert rbac.has_role(u, rbac.ROLE_MEMBER)
        assert rbac.has_role(u, rbac.ROLE_OFFICER)
        assert not rbac.has_role(u, rbac.ROLE_DIRECTOR)
        assert rbac.effective_rank(u) == rbac.ROLE_RANK[rbac.ROLE_OFFICER]
        rbac.has_role(u, rbac.ROLE_MEMBER)
    assert _count(ctx, "identity_roleassignment") == 1, "rank should be memoised per instance"


@pytest.mark.django_db
def test_role_rank_not_stale_after_role_change(django_user_model):
    u = django_user_model.objects.create(username="eve:perf-rank2")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    assert rbac.effective_rank(u) == rbac.ROLE_RANK[rbac.ROLE_MEMBER]  # memoises MEMBER
    # A new assignment for this in-memory instance must clear the memo (post_save signal).
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_OFFICER))
    assert rbac.effective_rank(u) == rbac.ROLE_RANK[rbac.ROLE_OFFICER]


# --- 0.2 home_corp_name cache -------------------------------------------------
@pytest.mark.django_db
def test_home_corp_name_cached_and_invalidated(settings):
    from apps.corporation.access import home_corp_name
    from apps.corporation.models import EveCorporation

    settings.FORCA_HOME_CORP_ID = 98000123
    settings.FORCA_CORP_NAME = "Fallback"
    corp = EveCorporation.objects.create(corporation_id=98000123, name="Alpha")
    assert home_corp_name() == "Alpha"
    with CaptureQueriesContext(connection) as ctx:
        assert home_corp_name() == "Alpha"
    assert _count(ctx, "corporation_evecorporation") == 0, "second read should be cached"
    # A rename (a .save(), which fires the signal) must invalidate.
    corp.name = "Beta"
    corp.save()
    assert home_corp_name() == "Beta"


# --- 0.3 service id-sets cache ------------------------------------------------
@pytest.mark.django_db
def test_service_ids_cached_and_invalidated():
    from apps.corporation.access import service_corp_ids
    from apps.corporation.models import FriendlyCorporation

    assert service_corp_ids() == set()
    with CaptureQueriesContext(connection) as ctx:
        service_corp_ids()
    assert _count(ctx, "friendlycorporation") == 0, "second read should be cached"
    # Adding a friendly corp must invalidate the cached set (post_save signal).
    FriendlyCorporation.objects.create(corporation_id=98007000, active=True)
    assert 98007000 in service_corp_ids()


@pytest.mark.django_db
def test_is_service_alliance_pilot_uses_cached_id_sets(django_user_model, settings):
    """The allowed alliance/corp id sets are cached (the repeated per-request queries the
    audit flagged); the access check itself is NOT memoised, so a revoke stays live."""
    from apps.corporation.access import is_service_alliance_pilot
    from apps.corporation.models import EveAlliance, EveCorporation, PartnerAlliance
    from apps.sso.models import EveCharacter

    settings.FORCA_HOME_CORP_ID = 98000200
    alliance = EveAlliance.objects.create(alliance_id=99001, name="Ally")
    EveCorporation.objects.create(corporation_id=98000200, name="Home", alliance=alliance)
    PartnerAlliance.objects.create(alliance_id=99001, active=True)
    u = django_user_model.objects.create(username="eve:perf-ally")
    EveCharacter.objects.create(character_id=6001, user=u, name="A", alliance_id=99001)
    u = django_user_model.objects.get(pk=u.pk)
    assert is_service_alliance_pilot(u) is True  # warms the id-set caches
    with CaptureQueriesContext(connection) as ctx:
        assert is_service_alliance_pilot(u) is True
    # The expensive id-set resolution is cached (no PartnerAlliance/EveCorporation queries);
    # only the cheap character EXISTS runs.
    assert _count(ctx, "corporation_partneralliance") == 0
    assert _count(ctx, "corporation_evecorporation") == 0


# --- 0.10 universe map cache --------------------------------------------------
@pytest.mark.django_db
def test_universe_map_cached():
    from apps.navigation.maps import universe_map

    universe_map()  # populates the cache (topology is static)
    with CaptureQueriesContext(connection) as ctx:
        universe_map()
    assert len(ctx.captured_queries) == 0, "universe map topology should be cached"


# --- 0.4 doctrine_coverage N+1 (the critical one) -----------------------------
@pytest.mark.django_db
def test_doctrine_coverage_query_count_is_constant_in_characters(django_user_model):
    """The fits + their skill requirements must be fetched ONCE, not per character."""
    from apps.characters.models import CharacterSkillSnapshot
    from apps.doctrines.models import Doctrine, DoctrineFit, SkillRequirement
    from apps.doctrines.services import doctrine_coverage
    from apps.sso.models import EveCharacter

    doctrine = Doctrine.objects.create(name="Perf Doctrine")
    fit = DoctrineFit.objects.create(doctrine=doctrine, name="Fit A", ship_type_id=587)
    SkillRequirement.objects.create(fit=fit, skill_type_id=3300, min_level=4, optimal_level=5)
    SkillRequirement.objects.create(fit=fit, skill_type_id=3301, min_level=3, optimal_level=4)

    def make_chars(n, base):
        chars = []
        for i in range(n):
            cid = base + i
            u = django_user_model.objects.create(username=f"eve:pc{cid}")
            c = EveCharacter.objects.create(character_id=cid, user=u, name=f"C{cid}",
                                            is_corp_member=True)
            CharacterSkillSnapshot.objects.create(
                character=c, is_latest=True,
                skills={"3300": {"trained_level": 5}, "3301": {"trained_level": 5}},
            )
            chars.append(c)
        return chars

    one = make_chars(1, 7100)
    four = make_chars(4, 7200)

    with CaptureQueriesContext(connection) as ctx1:
        doctrine_coverage(doctrine, one)
    with CaptureQueriesContext(connection) as ctx4:
        doctrine_coverage(doctrine, four)

    # Fits + skill requirements are each fetched exactly once regardless of #characters.
    assert _count(ctx1, "doctrines_skillrequirement") == 1
    assert _count(ctx4, "doctrines_skillrequirement") == 1
    assert _count(ctx1, "doctrines_doctrinefit") == _count(ctx4, "doctrines_doctrinefit")


# --- 0.11 market refresh is async --------------------------------------------
@pytest.mark.django_db
def test_refresh_market_dispatches_task_not_inline(client, django_user_model, monkeypatch):
    from apps.market.models import MarketPrice

    # One tracked price so tracked_history_type_ids() is non-empty.
    MarketPrice.objects.create(type_id=34, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min="5.00", volume=1000)
    dispatched = {}
    monkeypatch.setattr(
        "apps.market.tasks.sync_market_history.delay",
        lambda **kw: dispatched.update(kw) or None,
    )
    u = django_user_model.objects.create(username="eve:perf-mkt")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(u)
    resp = client.post("/market/refresh/")
    assert resp.status_code == 302
    assert "max_types" in dispatched, "refresh must enqueue the task, not run ESI inline"
