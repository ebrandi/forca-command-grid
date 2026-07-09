"""SKL-4 — opt-in idle-skill-queue nudges via Pingboard."""
from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.characters.models import SkillQueueSnapshot
from apps.identity.models import RoleAssignment
from apps.pilots.models import PilotPreference
from apps.pingboard import config
from apps.pingboard.models import Alert
from apps.skills.idle_notify import notify_idle_queues
from apps.skills.models import IdleQueueNudge
from apps.skills.overview import is_queue_idle
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_config():
    config.reset("notifications")
    yield
    config.reset("notifications")


@pytest.fixture(autouse=True)
def _no_esi(monkeypatch):
    """The idle fresh-check calls ESI; default it to a no-op so tests use the stored
    snapshot. Individual tests override it to simulate a queue that filled up."""
    monkeypatch.setattr("apps.characters.services.import_character_skillqueue",
                        lambda character, client=None: None)


def _pilot(django_user_model, cid, *, opted_in=True):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    PilotPreference.objects.create(user=user, notify_idle_queue=opted_in)
    ch = EveCharacter.objects.create(character_id=cid, user=user, name=f"Pilot{cid}",
                                     is_main=True, is_corp_member=True)
    return user, ch


def _queue(ch, entries):
    SkillQueueSnapshot.objects.filter(character=ch, is_latest=True).update(is_latest=False)
    return SkillQueueSnapshot.objects.create(character=ch, is_latest=True, entries=entries)


def _training_entry(hours=5):
    finish = (timezone.now() + dt.timedelta(hours=hours)).isoformat()
    return {"skill_id": 3300, "finished_level": 5, "queue_position": 0, "finish_date": finish}


def _idle_alerts(user):
    return Alert.objects.filter(source_service="skills", audience={"kind": "user", "id": user.id})


# --- is_queue_idle helper ----------------------------------------------------
def test_is_queue_idle_variants(django_user_model):
    _, ch = _pilot(django_user_model, 8001)
    assert is_queue_idle(ch) is None            # no snapshot yet
    _queue(ch, [])
    assert is_queue_idle(ch) is True            # empty queue
    _queue(ch, [_training_entry()])
    assert is_queue_idle(ch) is False           # still training
    past = (timezone.now() - dt.timedelta(hours=1)).isoformat()
    _queue(ch, [{"skill_id": 3300, "finish_date": past}])
    assert is_queue_idle(ch) is True            # every entry finished


# --- nudge behaviour ---------------------------------------------------------
def test_opted_in_idle_pilot_gets_one_nudge(django_user_model):
    user, ch = _pilot(django_user_model, 8002)
    _queue(ch, [])
    assert notify_idle_queues() == 1
    assert _idle_alerts(user).count() == 1
    tracker = IdleQueueNudge.objects.get(character_id=8002)
    assert tracker.notified_at is not None


def test_nudge_not_repeated_same_idle_period(django_user_model):
    user, ch = _pilot(django_user_model, 8003)
    _queue(ch, [])
    assert notify_idle_queues() == 1
    assert notify_idle_queues() == 0            # deduped
    assert _idle_alerts(user).count() == 1


def test_not_opted_in_gets_no_nudge(django_user_model):
    user, ch = _pilot(django_user_model, 8004, opted_in=False)
    _queue(ch, [])
    assert notify_idle_queues() == 0
    assert not _idle_alerts(user).exists()


def test_training_queue_is_not_nudged(django_user_model):
    user, ch = _pilot(django_user_model, 8005)
    _queue(ch, [_training_entry()])
    assert notify_idle_queues() == 0
    assert not _idle_alerts(user).exists()


def test_no_snapshot_is_not_nudged(django_user_model):
    user, ch = _pilot(django_user_model, 8006)  # never synced
    assert notify_idle_queues() == 0
    assert not IdleQueueNudge.objects.filter(character_id=8006).exists()


def test_reset_then_renudge_on_next_idle_period(django_user_model):
    user, ch = _pilot(django_user_model, 8007)
    _queue(ch, [])
    assert notify_idle_queues() == 1
    # pilot queues skills again → tracker resets
    _queue(ch, [_training_entry()])
    assert notify_idle_queues() == 0
    assert not IdleQueueNudge.objects.filter(character_id=8007).exists()
    # queue empties again → a fresh nudge
    _queue(ch, [])
    assert notify_idle_queues() == 1
    assert _idle_alerts(user).count() == 2


def test_fresh_check_suppresses_stale_idle(django_user_model, monkeypatch):
    """A snapshot that looks idle but is stale: the fresh pull finds a full queue → no nudge."""
    user, ch = _pilot(django_user_model, 8008)
    _queue(ch, [])  # stored snapshot looks idle

    def _fake_import(character, client=None):
        _queue(character, [_training_entry()])  # fresh pull shows training

    monkeypatch.setattr("apps.characters.services.import_character_skillqueue", _fake_import)
    assert notify_idle_queues() == 0
    assert not _idle_alerts(user).exists()


def test_disabled_event_is_noop(django_user_model):
    user, ch = _pilot(django_user_model, 8009)
    _queue(ch, [])
    config.set("notifications", {"events": {"skills.idle_queue": {"enabled": False}}})
    assert notify_idle_queues() == 0
    assert not IdleQueueNudge.objects.filter(character_id=8009).exists()


# --- opt-in toggle view ------------------------------------------------------
def test_toggle_idle_queue_nudge(client, django_user_model):
    user, _ = _pilot(django_user_model, 8010, opted_in=False)
    client.force_login(user)
    resp = client.post(reverse("pilots:toggle_idle_queue_nudge"))
    assert resp.status_code == 302
    assert PilotPreference.objects.get(user=user).notify_idle_queue is True
    client.post(reverse("pilots:toggle_idle_queue_nudge"))
    assert PilotPreference.objects.get(user=user).notify_idle_queue is False
