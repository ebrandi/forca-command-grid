"""Fleet participation (PAP): confirmed-only recognition credit + leaderboard (OPS-2 / 3.1).

Credit (ledger + raffle tickets + leaderboard) follows FC/officer confirmation or the ESI
fleet-pull — never a bare self-report — so the leaderboard and raffle stay fair.
"""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.operations.models import Operation, OperationAttendance
from apps.pilots.models import ContributionEvent
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _member(django_user_model, cid, role=rbac.ROLE_MEMBER):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    EveCharacter.objects.create(character_id=cid, user=user, name=f"Pilot{cid}",
                                is_main=True, is_corp_member=True)
    return user


def _fleet_events():
    return ContributionEvent.objects.filter(kind=ContributionEvent.Kind.FLEET)


@pytest.mark.django_db
def test_self_report_does_not_credit(client, django_user_model):
    member = _member(django_user_model, 9001)
    op = Operation.objects.create(name="Home Defence")
    client.force_login(member)
    assert client.post(f"/operations/{op.pk}/attend/").status_code == 302
    att = OperationAttendance.objects.get(operation=op, user=member)
    assert att.character_name == "Pilot9001" and att.confirmed is False
    assert not _fleet_events().exists()  # a bare self-report earns no ledger credit


@pytest.mark.django_db
def test_officer_confirm_credits_and_unconfirm_uncredits(client, django_user_model):
    member = _member(django_user_model, 9003)
    officer = _member(django_user_model, 9004, rbac.ROLE_OFFICER)
    op = Operation.objects.create(name="Timer")
    client.force_login(member)
    client.post(f"/operations/{op.pk}/attend/")
    att = OperationAttendance.objects.get()
    assert not _fleet_events().exists()

    client.force_login(officer)
    client.post(f"/operations/{op.pk}/attendance/", {"att_id": att.id, "action": "confirm"})
    att.refresh_from_db()
    assert att.confirmed is True
    ev = _fleet_events().get()
    assert ev.user_id == member.id and ev.ref_id == f"{op.pk}:{member.pk}"

    client.post(f"/operations/{op.pk}/attendance/", {"att_id": att.id, "action": "unconfirm"})
    assert not _fleet_events().exists()  # unconfirm pulls the credit back


@pytest.mark.django_db
def test_confirm_is_idempotent(client, django_user_model):
    member = _member(django_user_model, 9010)
    officer = _member(django_user_model, 9011, rbac.ROLE_OFFICER)
    op = Operation.objects.create(name="Op")
    client.force_login(member)
    client.post(f"/operations/{op.pk}/attend/")
    att = OperationAttendance.objects.get()
    client.force_login(officer)
    for _ in range(2):
        client.post(f"/operations/{op.pk}/attendance/", {"att_id": att.id, "action": "confirm"})
    assert _fleet_events().count() == 1  # no double credit on re-confirm


@pytest.mark.django_db
def test_officer_remove_uncredits(client, django_user_model):
    member = _member(django_user_model, 9005)
    officer = _member(django_user_model, 9006, rbac.ROLE_OFFICER)
    op = Operation.objects.create(name="Timer")
    client.force_login(member)
    client.post(f"/operations/{op.pk}/attend/")
    att = OperationAttendance.objects.get()
    client.force_login(officer)
    client.post(f"/operations/{op.pk}/attendance/", {"att_id": att.id, "action": "confirm"})
    assert _fleet_events().exists()
    client.post(f"/operations/{op.pk}/attendance/", {"att_id": att.id, "action": "remove"})
    assert not OperationAttendance.objects.filter(pk=att.pk).exists()
    assert not _fleet_events().exists()


@pytest.mark.django_db
def test_unattend_removes_attendance(client, django_user_model):
    member = _member(django_user_model, 9002)
    op = Operation.objects.create(name="Roam")
    client.force_login(member)
    client.post(f"/operations/{op.pk}/attend/")
    assert client.post(f"/operations/{op.pk}/unattend/").status_code == 302
    assert not OperationAttendance.objects.filter(operation=op).exists()


@pytest.mark.django_db
def test_leaderboard_counts_only_confirmed(client, django_user_model):
    from apps.operations.services import participation_leaderboard
    from apps.pilots.services import get_prefs

    member = _member(django_user_model, 9008)
    officer = _member(django_user_model, 9009, rbac.ROLE_OFFICER)
    op = Operation.objects.create(name="Op A")
    client.force_login(member)
    client.post(f"/operations/{op.pk}/attend/")
    assert participation_leaderboard() == []  # unconfirmed self-report is not counted

    att = OperationAttendance.objects.get()
    client.force_login(officer)
    client.post(f"/operations/{op.pk}/attendance/", {"att_id": att.id, "action": "confirm"})
    lb = participation_leaderboard()
    assert lb and lb[0]["name"] == "Pilot9008" and lb[0]["count"] == 1

    prefs = get_prefs(member)
    prefs.public_recognition = False
    prefs.save()
    assert participation_leaderboard() == []  # opt-out still honoured
