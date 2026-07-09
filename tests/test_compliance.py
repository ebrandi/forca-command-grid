"""Member compliance & inactivity report + the director board."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from apps.corporation.models import CorpMember
from apps.identity.models import RoleAssignment
from apps.sso.models import AuthToken, EveCharacter
from apps.sso.services import ensure_role
from core import rbac

FULL = [s for s in settings.EVE_SSO_DEFAULT_SCOPES if s != "publicData"]


def _member_char(cid, name, *, user=None, scopes=None, login_days_ago=1):
    CorpMember.objects.create(
        character_id=cid, name=name, corporation_id=98000001,
        logon_date=timezone.now() - timedelta(days=login_days_ago),
    )
    if user is not None:
        EveCharacter.objects.create(character_id=cid, user=user, name=name,
                                    is_main=True, is_corp_member=True)
        if scopes is not None:
            AuthToken.objects.create(character_id=cid, scopes=scopes)


def _user(django_user_model, name, role):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


@pytest.mark.django_db
def test_report_classifies_each_member(django_user_model):
    from apps.corporation.compliance import compliance_report

    ua = django_user_model.objects.create(username="a")
    ub = django_user_model.objects.create(username="b")
    ud = django_user_model.objects.create(username="d")
    _member_char(1, "Compliant Carl", user=ua, scopes=FULL, login_days_ago=2)
    _member_char(2, "Missing Mary", user=ub, scopes=FULL[:-1], login_days_ago=2)  # one scope short
    _member_char(3, "Unlinked Una")  # CorpMember only, no account
    _member_char(4, "Inactive Ivan", user=ud, scopes=FULL, login_days_ago=60)

    rep = compliance_report(inactive_days=30)
    assert rep["total"] == 4
    assert rep["unregistered"] == 1 and rep["missing_scopes"] == 1 and rep["inactive"] == 1
    assert rep["compliant"] == 1

    by_name = {r["name"]: r for r in rep["rows"]}
    assert by_name["Compliant Carl"]["compliant"] is True
    assert by_name["Missing Mary"]["missing_scopes"] and not by_name["Missing Mary"]["compliant"]
    assert by_name["Unlinked Una"]["registered"] is False
    assert by_name["Inactive Ivan"]["is_inactive"] is True
    # Worst offenders first: the compliant member sorts last.
    assert rep["rows"][-1]["name"] == "Compliant Carl"


@pytest.mark.django_db
def test_inactive_threshold_is_configurable(django_user_model):
    from apps.corporation.compliance import compliance_report

    u = django_user_model.objects.create(username="x")
    _member_char(10, "Borderline", user=u, scopes=FULL, login_days_ago=20)
    assert compliance_report(inactive_days=30)["inactive"] == 0  # 20 < 30
    assert compliance_report(inactive_days=14)["inactive"] == 1  # 20 >= 14


@pytest.mark.django_db
def test_board_is_director_only(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "officer", rbac.ROLE_OFFICER))
    assert client.get("/ops/admin/compliance/").status_code == 403
    client.force_login(_user(django_user_model, "director", rbac.ROLE_DIRECTOR))
    _member_char(99, "Unlinked Pilot")
    html = client.get("/ops/admin/compliance/").content.decode()
    assert "Member compliance" in html and "Unlinked Pilot" in html
