"""Director member-audit dossier (consolidated per-member view)."""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.pilots.services import record_contribution
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, name, role):
    user = django_user_model.objects.create(username=f"eve:{name}")
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


@pytest.mark.django_db
def test_member_audit_shows_dossier(client, django_user_model):
    director = _user(django_user_model, "aud-dir", rbac.ROLE_DIRECTOR)
    member = django_user_model.objects.create(username="eve:aud-mem")
    EveCharacter.objects.create(character_id=7001, user=member, name="Audited Pilot",
                                is_main=True, is_corp_member=True)
    record_contribution(member, "build", 3, "ships", description="built feroxes",
                        ref_type="job", ref_id="1")
    client.force_login(director)
    resp = client.get(f"/ops/admin/members/{member.id}/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Audited Pilot" in body and "Built" in body


@pytest.mark.django_db
def test_member_audit_is_director_only(client, django_user_model):
    officer = _user(django_user_model, "aud-off", rbac.ROLE_OFFICER)
    member = django_user_model.objects.create(username="eve:aud-mem2")
    client.force_login(officer)
    assert client.get(f"/ops/admin/members/{member.id}/").status_code == 403
