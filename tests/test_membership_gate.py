"""Membership gate: logged-in non-corp pilots see only the recruitment surface.

A pilot whose character isn't in the home corp holds no `member` role, so every
internal page must funnel them to the recruitment/onboarding page; only the
recruitment surface and their own account/data pages are reachable.
"""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

INTERNAL_PATHS = [
    # NB: /doctrines and /tools are NOT here — they're audience-controlled features now
    # (public/corp/alliance/disabled), gated by the FeatureGate audience check rather
    # than confined by the membership gate. See tests/test_feature_audience.py.
    "/dashboard/", "/killboard/", "/industry/", "/skills/",
    "/stockpile/", "/market/", "/tasks/", "/srp/", "/operations/", "/erp/",
    "/readiness/", "/pilots/briefing/", "/pilots/contributions/",
    "/recruitment/", "/ops/admin/",
]
# /kb/ is a recruiting surface: the public tier is reachable by a recruit, and the kb
# views' own visibility gate confines a non-member to public pages (see test_kb_public).
ALLOWED_PATHS = ["/onboarding/", "/auth/eve/scopes/", "/privacy/", "/kb/"]


def _non_member(django_user_model):
    """A logged-in pilot with a character that is NOT in the home corp."""
    user = django_user_model.objects.create(username="eve:nonmember")
    EveCharacter.objects.create(
        character_id=777, user=user, name="Outsider", is_main=True, is_corp_member=False
    )
    return user


@pytest.mark.django_db
@pytest.mark.parametrize("path", INTERNAL_PATHS)
def test_non_member_is_redirected_from_internal_pages(client, django_user_model, sde, path):
    client.force_login(_non_member(django_user_model))
    resp = client.get(path)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/onboarding/"


@pytest.mark.django_db
@pytest.mark.parametrize("path", ALLOWED_PATHS)
def test_non_member_can_reach_recruitment_and_account(client, django_user_model, sde, path):
    client.force_login(_non_member(django_user_model))
    assert client.get(path).status_code == 200


@pytest.mark.django_db
def test_member_keeps_full_access(client, django_user_model, sde):
    user = django_user_model.objects.create(username="eve:member")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(
        character_id=42, user=user, name="Insider", is_main=True, is_corp_member=True
    )
    client.force_login(user)
    assert client.get("/dashboard/").status_code == 200
    assert client.get("/killboard/").status_code == 200


@pytest.mark.django_db
def test_anonymous_access_is_unchanged(client, sde):
    # Public killboard still public; internal pages still go to LOGIN, not onboarding.
    assert client.get("/killboard/").status_code == 200
    resp = client.get("/dashboard/")
    assert resp.status_code == 302
    assert "/auth/eve/login" in resp.headers["Location"]


@pytest.mark.django_db
def test_superuser_bypasses_the_gate(client, django_user_model, sde):
    admin = django_user_model.objects.create(username="root", is_superuser=True, is_staff=True)
    client.force_login(admin)
    assert client.get("/dashboard/").status_code == 200
