"""PNG-1 (roadmap 2.9) — full automation-rule editor.

Acceptance: directors can create AND edit rules with conditions/template/window/cooldown
from the console; rules ship disabled; invalid JSON is rejected; non-directors can't.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.pingboard.models import AutomationRule
from core import rbac
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db


def _url():
    return reverse("admin_audit:pingboard_automation")


def _director(client, dum, cid):
    user, _ = enrol_pilot(dum, cid, roles=(rbac.ROLE_DIRECTOR,))
    client.force_login(user)
    return user


def _post(**kw):
    data = {
        "action": "create", "key": "test-rule", "label": "Test Rule",
        "trigger_source": "srp.submitted", "category": "system", "priority": "normal",
        "condition": "", "audience": "", "channels": "", "title": "Hi", "body": "Body",
        "cooldown_minutes": "0", "max_per_window": "0", "window_minutes": "60", "expires_at": "",
    }
    data.update(kw)
    return data


def test_director_creates_full_rule_shipped_disabled(client, django_user_model):
    _director(client, django_user_model, 7000)
    r = client.post(_url(), _post(
        condition='{"amount_gt": 1000000000}', audience='{"kind": "officer"}',
        channels='["in_app"]', cooldown_minutes="15", max_per_window="3", window_minutes="120",
    ))
    assert r.status_code in (302, 200)
    rule = AutomationRule.objects.get(key="test-rule")
    assert rule.enabled is False  # ships disabled — never armed on create
    assert rule.condition == {"amount_gt": 1000000000}
    assert rule.audience == {"kind": "officer"}
    assert rule.channels == ["in_app"]
    assert (rule.cooldown_minutes, rule.max_per_window, rule.window_minutes) == (15, 3, 120)


def test_invalid_condition_json_rejected(client, django_user_model):
    _director(client, django_user_model, 7001)
    r = client.post(_url(), _post(key="bad", condition="{not valid json"))
    assert r.status_code == 200  # re-rendered with errors, not saved
    assert not AutomationRule.objects.filter(key="bad").exists()


def test_condition_must_be_object_not_list(client, django_user_model):
    _director(client, django_user_model, 7002)
    r = client.post(_url(), _post(key="listcond", condition='[1, 2, 3]'))
    assert r.status_code == 200
    assert not AutomationRule.objects.filter(key="listcond").exists()


def test_director_edits_rule_preserving_enabled(client, django_user_model):
    rule = AutomationRule.objects.create(
        key="r1", label="R1", trigger_source="srp.submitted", category="system", enabled=True
    )
    _director(client, django_user_model, 7003)
    r = client.post(_url(), _post(
        action="edit", rule_id=rule.id, key="r1", label="Renamed", cooldown_minutes="30",
    ))
    assert r.status_code in (302, 200)
    rule.refresh_from_db()
    assert rule.label == "Renamed"
    assert rule.cooldown_minutes == 30
    assert rule.enabled is True  # edit never arms/disarms — only the toggle does


def test_toggle_and_delete(client, django_user_model):
    rule = AutomationRule.objects.create(
        key="r2", label="R2", trigger_source="x", category="system", enabled=False
    )
    _director(client, django_user_model, 7004)
    client.post(_url(), {"action": "toggle", "rule_id": rule.id})
    rule.refresh_from_db()
    assert rule.enabled is True
    client.post(_url(), {"action": "delete", "rule_id": rule.id})
    assert not AutomationRule.objects.filter(pk=rule.id).exists()


def test_edit_get_prefills_the_form(client, django_user_model):
    rule = AutomationRule.objects.create(key="r3", label="R3", trigger_source="x", category="system")
    _director(client, django_user_model, 7005)
    r = client.get(_url() + f"?edit={rule.id}")
    assert r.status_code == 200
    assert b"Edit rule" in r.content and b"R3" in r.content


def test_unknown_audience_kind_is_rejected(client, django_user_model):
    # A typo like "directors" would classify as uncapped downstream → reject it here.
    _director(client, django_user_model, 7007)
    r = client.post(_url(), _post(key="typo", audience='{"kind": "directors"}'))
    assert r.status_code == 200
    assert not AutomationRule.objects.filter(key="typo").exists()


def test_negative_or_zero_throttle_is_rejected(client, django_user_model):
    _director(client, django_user_model, 7008)
    assert client.post(_url(), _post(key="w0", window_minutes="0")).status_code == 200
    assert not AutomationRule.objects.filter(key="w0").exists()
    assert client.post(_url(), _post(key="cdneg", cooldown_minutes="-5")).status_code == 200
    assert not AutomationRule.objects.filter(key="cdneg").exists()


def test_non_numeric_edit_param_does_not_500(client, django_user_model):
    _director(client, django_user_model, 7009)
    assert client.get(_url() + "?edit=abc").status_code == 200


def test_forbidden_below_director(client, django_user_model):
    user, _ = enrol_pilot(django_user_model, 7006, roles=(rbac.ROLE_OFFICER,))
    client.force_login(user)
    assert client.get(_url()).status_code in (302, 403, 404)
