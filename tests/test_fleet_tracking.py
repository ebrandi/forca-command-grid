"""Live fleet tracking: pull the ESI fleet roster → attendance + fleet credit."""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.operations.models import Operation, OperationAttendance
from apps.pilots.models import ContributionEvent
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


class _Resp:
    def __init__(self, data):
        self.data = data


class _Client:
    """Fake ESI: a character's fleet, then that fleet's members."""

    def __init__(self, fleet_id, member_ids):
        self.fleet_id = fleet_id
        self.member_ids = member_ids

    def get(self, path, token=None, params=None):
        if path.endswith("/fleet/"):
            return _Resp({"fleet_id": self.fleet_id, "role": "fleet_commander"})
        if "/members/" in path:
            return _Resp([{"character_id": c, "ship_type_id": 587} for c in self.member_ids])
        return _Resp(None)


def _member(django_user_model, cid, role=rbac.ROLE_MEMBER, main=True):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    EveCharacter.objects.create(character_id=cid, user=user, name=f"Pilot{cid}",
                                is_main=main, is_corp_member=True)
    return user


@pytest.fixture
def _granted(monkeypatch):
    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda ch, sc: "tok")


@pytest.mark.django_db
def test_pull_records_attendance_for_linked_members(_granted, django_user_model):
    from apps.operations.fleet_esi import pull_fleet_attendance

    _member(django_user_model, 1001, role=rbac.ROLE_OFFICER)
    _member(django_user_model, 1002)
    _member(django_user_model, 1003)
    op = Operation.objects.create(name="Home Defence")

    # Fleet has the 3 linked members + an unlinked stranger (9999).
    client = _Client(fleet_id=42, member_ids=[1001, 1002, 1003, 9999])
    fc_char = EveCharacter.objects.get(character_id=1001)
    res = pull_fleet_attendance(op, fc_char, client=client)

    assert res["status"] == "ok" and res["fleet_size"] == 4
    assert res["recorded"] == 3  # only the linked members
    assert OperationAttendance.objects.filter(operation=op).count() == 3
    att = OperationAttendance.objects.get(operation=op, user__username="eve:1002")
    assert att.confirmed and att.added_by_officer
    assert ContributionEvent.objects.filter(kind=ContributionEvent.Kind.FLEET).count() == 3

    # Idempotent: pulling again doesn't duplicate attendance or credit.
    pull_fleet_attendance(op, fc_char, client=client)
    assert OperationAttendance.objects.filter(operation=op).count() == 3
    assert ContributionEvent.objects.filter(kind=ContributionEvent.Kind.FLEET).count() == 3


@pytest.mark.django_db
def test_not_in_fleet_is_handled(_granted, django_user_model):
    from apps.operations.fleet_esi import pull_fleet_attendance

    _member(django_user_model, 2001, role=rbac.ROLE_OFFICER)
    op = Operation.objects.create(name="Op")
    client = _Client(fleet_id=None, member_ids=[])
    res = pull_fleet_attendance(op, EveCharacter.objects.get(character_id=2001), client=client)
    assert res["status"] == "not_in_fleet" and res["recorded"] == 0


@pytest.mark.django_db
def test_no_token_is_handled(monkeypatch, django_user_model):
    from apps.operations.fleet_esi import pull_fleet_attendance
    from apps.sso.token_service import NoValidToken

    def _raise(ch, sc):
        raise NoValidToken()

    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", _raise)
    _member(django_user_model, 3001, role=rbac.ROLE_OFFICER)
    op = Operation.objects.create(name="Op")
    res = pull_fleet_attendance(op, EveCharacter.objects.get(character_id=3001))
    assert res["status"] == "no_token"
