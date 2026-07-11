"""Login + dashboard performance changes (2026-07).

- closest_doctrines is cached per character (+ invalidated on a skill snapshot).
- the login path assigns the member role synchronously but DEFERS the Director ESI
  check to warm_pilot_after_login (so the redirect to /dashboard/ isn't blocked on ESI).
"""
from __future__ import annotations

import pytest
import responses
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.sso.models import AuthToken, EveCharacter
from apps.sso.services import ROLE_SCOPE
from core import rbac

HOME_CORP = 98000001


def _char(uid, **kw):
    u = get_user_model().objects.create(username=f"eve:{uid}")
    c = EveCharacter.objects.create(character_id=uid, user=u, name="P", is_main=True, **kw)
    return u, c


def _roles_token(char):
    t = AuthToken(character=char, scopes=[ROLE_SCOPE],
                  access_expires_at=timezone.now() + timezone.timedelta(hours=1))
    t.refresh_token = "refresh"
    t.access_token = "access"
    t.save()


# --- closest_doctrines caching -----------------------------------------------
@pytest.mark.django_db
def test_closest_doctrines_cached_and_invalidated():
    from apps.skills.services import closest_doctrines, invalidate_closest_doctrines

    _, c = _char(5001)
    closest_doctrines(c)  # first call computes + caches
    with CaptureQueriesContext(connection) as ctx:
        closest_doctrines(c)
    assert len(ctx.captured_queries) == 0, "second call should be a cache hit"

    invalidate_closest_doctrines(c.character_id)
    with CaptureQueriesContext(connection) as ctx2:
        closest_doctrines(c)
    assert len(ctx2.captured_queries) > 0, "invalidation should force a recompute"


@responses.activate
@pytest.mark.django_db
def test_skill_import_invalidates_closest():
    """A skills import (the real writer) must clear the closest-doctrines cache."""
    from django.core.cache import cache

    from apps.characters.services import import_character_skills
    from apps.skills.services import closest_doctrines

    _, c = _char(5002)
    t = AuthToken(character=c, scopes=["esi-skills.read_skills.v1"],
                  access_expires_at=timezone.now() + timezone.timedelta(hours=1))
    t.refresh_token = "r"
    t.access_token = "a"
    t.save()
    responses.add(
        responses.GET, f"https://esi.evetech.net/characters/{c.character_id}/skills/",
        json={"skills": [{"skill_id": 3300, "trained_skill_level": 5,
                          "skillpoints_in_skill": 256000}], "total_sp": 256000},
        status=200,
    )
    closest_doctrines(c)  # warm the cache
    assert cache.get(f"skills:closest:{c.character_id}") is not None
    import_character_skills(c)
    assert cache.get(f"skills:closest:{c.character_id}") is None


# --- login defers the Director ESI check -------------------------------------
@responses.activate
@pytest.mark.django_db
def test_member_only_sync_makes_no_esi_call(settings):
    """check_director=False assigns member but issues NO ESI call (responses would raise)."""
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    from apps.sso.services import sync_roles_for_user

    u, _ = _char(6001, is_corp_member=True)
    sync_roles_for_user(u, check_director=False)  # no responses registered ⇒ any ESI call errors
    assert rbac.has_role(u, rbac.ROLE_MEMBER) is True
    assert rbac.has_role(u, rbac.ROLE_DIRECTOR) is False


@responses.activate
@pytest.mark.django_db
def test_warm_task_runs_deferred_director_check(settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    from apps.sso.tasks import warm_pilot_after_login

    u, c = _char(6002, is_corp_member=True)
    _roles_token(c)
    responses.add(responses.GET, f"https://esi.evetech.net/characters/{c.character_id}/",
                  json={"corporation_id": HOME_CORP, "name": "P"}, status=200)
    # CEO is someone else, so the character is recognised as Director via the explicit
    # in-game role rather than the CEO shortcut (director sync fetches public corp data).
    responses.add(responses.GET, f"https://esi.evetech.net/corporations/{HOME_CORP}/",
                  json={"ceo_id": 9999}, status=200)
    responses.add(responses.GET, f"https://esi.evetech.net/characters/{c.character_id}/roles/",
                  json={"roles": ["Director"]}, status=200)
    warm_pilot_after_login(c.character_id)  # the task does the director sync (+ best-effort warm)
    assert rbac.has_role(u, rbac.ROLE_DIRECTOR) is True
