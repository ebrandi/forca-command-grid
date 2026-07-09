"""OPS-1 — committed-pilot T-minus form-up reminders.

Acceptance: each YES-committed pilot gets exactly one targeted reminder before form-up;
it is not corp-wide; it is deduped; a MAYBE or an op outside the lead window gets none.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.operations.models import Operation, OperationCommitment
from apps.operations.services import send_formup_reminders
from apps.pingboard import config
from apps.pingboard.models import Alert
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_notifications_config():
    config.reset("notifications")
    yield
    config.reset("notifications")


def _user(django_user_model, cid):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=cid, user=user, name=f"Pilot{cid}",
                                is_main=True, is_corp_member=True)
    return user


def _op(minutes_out=30, **kw):
    kw.setdefault("name", "Saturday Fleet")
    kw.setdefault("type", Operation.Type.PVP)
    kw.setdefault("status", Operation.Status.PLANNED)
    kw.setdefault("target_at", timezone.now() + timedelta(minutes=minutes_out))
    return Operation.objects.create(**kw)


def _commit(op, user, response=OperationCommitment.Response.YES):
    return OperationCommitment.objects.create(operation=op, user=user, response=response,
                                              character_name=user.username)


def _reminders_for(user):
    return Alert.objects.filter(source_service="operations", audience={"kind": "user", "id": user.id})


# --- the happy path ----------------------------------------------------------
def test_committed_pilot_gets_one_reminder(django_user_model):
    op = _op(minutes_out=30)  # inside the 60-min lead window
    u = _user(django_user_model, 5001)
    c = _commit(op, u)
    sent = send_formup_reminders()
    assert sent == 1
    assert _reminders_for(u).count() == 1
    c.refresh_from_db()
    assert c.reminder_sent_at is not None
    # targeted, not corp-wide
    assert _reminders_for(u).first().audience == {"kind": "user", "id": u.id}


def test_reminder_not_repeated(django_user_model):
    op = _op(minutes_out=30)
    u = _user(django_user_model, 5002)
    _commit(op, u)
    assert send_formup_reminders() == 1
    assert send_formup_reminders() == 0  # deduped by reminder_sent_at
    assert _reminders_for(u).count() == 1


# --- window + eligibility gates ----------------------------------------------
def test_op_outside_lead_window_not_reminded(django_user_model):
    op = _op(minutes_out=180)  # 3h out, beyond the 60-min lead
    u = _user(django_user_model, 5003)
    _commit(op, u)
    assert send_formup_reminders() == 0
    assert not _reminders_for(u).exists()


def test_past_formup_not_reminded(django_user_model):
    op = _op(minutes_out=-10)  # already formed up
    u = _user(django_user_model, 5004)
    _commit(op, u)
    assert send_formup_reminders() == 0


def test_maybe_commitment_not_reminded(django_user_model):
    op = _op(minutes_out=30)
    u = _user(django_user_model, 5005)
    _commit(op, u, response=OperationCommitment.Response.MAYBE)
    assert send_formup_reminders() == 0
    assert not _reminders_for(u).exists()


def test_cancelled_op_not_reminded(django_user_model):
    op = _op(minutes_out=30, status=Operation.Status.CANCELLED)
    u = _user(django_user_model, 5006)
    _commit(op, u)
    assert send_formup_reminders() == 0


# --- leadership off switch ----------------------------------------------------
def test_disabled_event_is_noop(django_user_model):
    op = _op(minutes_out=30)
    u = _user(django_user_model, 5007)
    c = _commit(op, u)
    config.set("notifications", {"events": {"operations.formup_reminder": {"enabled": False}}})
    assert send_formup_reminders() == 0
    c.refresh_from_db()
    assert c.reminder_sent_at is None  # trackers untouched, so a later re-enable still fires
    assert not _reminders_for(u).exists()
