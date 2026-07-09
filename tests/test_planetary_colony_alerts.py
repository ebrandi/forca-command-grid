"""PI-2 (roadmap 3.5) — colony-issue alerts.

A detected colony issue (expired extractor / unrouted factory) fires at most one DM to the
enrolled owner; a re-occurrence after a fix nudges again; a disabled event doesn't swallow it.
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.planetary.esi import _alert_colony_issues
from apps.planetary.models import PiColony
from apps.sso.models import EveCharacter
from core.mixins import Source

pytestmark = pytest.mark.django_db
_EMIT = "apps.pingboard.services.emit_broadcast"
_ISSUE = "An extractor program has expired — restart it to keep pulling P0."


def _char(django_user_model, cid=8100, *, with_user=True):
    user = django_user_model.objects.create(username=f"eve:{cid}") if with_user else None
    return EveCharacter.objects.create(character_id=cid, user=user, name=f"P{cid}", is_main=True,
                                       is_corp_member=True)


def _colony(char, issues, planet_id=1):
    return PiColony.objects.create(
        character=char, planet_id=planet_id, planet_type_name="Barren",
        solar_system_name="Amamake", summary={"issues": issues},
        source=Source.ESI_CHAR, as_of=timezone.now(), fetched_at=timezone.now(),
    )


def test_new_issue_dms_owner(django_user_model, monkeypatch):
    calls = []
    monkeypatch.setattr(_EMIT, lambda **kw: calls.append(kw))
    char = _char(django_user_model)
    col = _colony(char, [_ISSUE])
    _alert_colony_issues(col, char)
    assert len(calls) == 1
    assert calls[0]["audience"] == {"kind": "user", "id": char.user_id}
    col.refresh_from_db()
    assert col.alerted_sig != ""


def test_same_issue_not_re_alerted(django_user_model, monkeypatch):
    calls = []
    monkeypatch.setattr(_EMIT, lambda **kw: calls.append(kw))
    char = _char(django_user_model)
    col = _colony(char, [_ISSUE])
    _alert_colony_issues(col, char)
    _alert_colony_issues(col, char)  # unchanged issue-set → no re-nudge
    assert len(calls) == 1


def test_cleared_then_recurrence_re_alerts(django_user_model, monkeypatch):
    calls = []
    monkeypatch.setattr(_EMIT, lambda **kw: calls.append(kw))
    char = _char(django_user_model)
    col = _colony(char, [_ISSUE])
    _alert_colony_issues(col, char)                       # alert #1
    col.summary = {"issues": []}
    col.save(update_fields=["summary"])
    _alert_colony_issues(col, char)                       # cleared → no alert, sig resets
    col.refresh_from_db()
    assert col.alerted_sig == ""
    col.summary = {"issues": [_ISSUE]}
    col.fetched_at = timezone.now()
    col.save(update_fields=["summary", "fetched_at"])
    _alert_colony_issues(col, char)                       # recurrence → alert #2
    assert len(calls) == 2


def test_disabled_event_does_not_alert_or_advance(django_user_model, monkeypatch):
    from apps.pingboard import config as pb_config

    calls = []
    monkeypatch.setattr(_EMIT, lambda **kw: calls.append(kw))
    doc = pb_config.get("notifications")
    doc["events"] = {**(doc.get("events") or {}), "planetary.colony_issue": {"enabled": False}}
    pb_config.set("notifications", doc)
    char = _char(django_user_model)
    col = _colony(char, [_ISSUE])
    _alert_colony_issues(col, char)
    assert calls == []
    col.refresh_from_db()
    assert col.alerted_sig == ""  # not advanced, so enabling it later still fires


def test_no_owner_no_alert(django_user_model, monkeypatch):
    calls = []
    monkeypatch.setattr(_EMIT, lambda **kw: calls.append(kw))
    char = _char(django_user_model, cid=8199, with_user=False)
    col = _colony(char, [_ISSUE])
    _alert_colony_issues(col, char)
    assert calls == []
