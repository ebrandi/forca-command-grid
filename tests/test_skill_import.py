"""Manual 'import my skills' action + default-scope coverage."""
from __future__ import annotations

import pytest
from django.conf import settings

from apps.characters.models import CharacterSkillSnapshot
from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def test_default_login_scopes_include_skills():
    # A fresh deploy must request skills so members can be read at all.
    assert "esi-skills.read_skills.v1" in settings.EVE_SSO_DEFAULT_SCOPES
    assert "esi-skills.read_skillqueue.v1" in settings.EVE_SSO_DEFAULT_SCOPES


@pytest.mark.django_db
def test_import_mine_without_scope_is_graceful(client, django_user_model):
    user = django_user_model.objects.create(username="eve:1001")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(
        character_id=1001, user=user, name="Pilot", is_main=True, is_corp_member=True
    )
    client.force_login(user)
    # No token/scope → must not 500; redirects back to the plans page.
    resp = client.post("/skills/import/")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/skills/"
    assert CharacterSkillSnapshot.objects.count() == 0


@pytest.mark.django_db
def test_import_mine_is_post_only_and_member_only(client, django_user_model):
    user = django_user_model.objects.create(username="eve:1002")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=1002, user=user, name="P", is_corp_member=True)
    client.force_login(user)
    assert client.get("/skills/import/").status_code == 405  # GET not allowed
