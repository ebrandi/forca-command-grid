"""Phase 4 — automation rules engine + urgent/large-audience governance."""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.pingboard import automation, config, services
from apps.pingboard.models import Alert, AlertStatus, AutomationRule
from apps.sso.models import EveCharacter


def _rule(**kw):
    base = dict(key="r1", label="Rule 1", enabled=True, trigger_source="test.event",
                category="system", audience={"kind": "corp"}, channels=["in_app"],
                priority="normal", title="Auto", body="auto body")
    base.update(kw)
    return AutomationRule.objects.create(**base)


def _corp_user(cid):
    User = get_user_model()
    u = User.objects.create(username=f"eve:{cid}")
    EveCharacter.objects.create(character_id=cid, user=u, name=f"P{cid}",
                                is_main=True, is_corp_member=True)
    return u


# --- automation engine -------------------------------------------------------
@pytest.mark.django_db
def test_trigger_fires_enabled_rule(character):
    rule = _rule()
    ids = automation.trigger("test.event", context={"x": "1"}, source_object_id="7")
    assert len(ids) == 1
    a = Alert.objects.get(pk=ids[0])
    assert a.source == "automation" and a.automation_rule_id == rule.id
    rule.refresh_from_db()
    assert rule.last_fired_at is not None


@pytest.mark.django_db
def test_disabled_rule_does_not_fire(character):
    _rule(enabled=False)
    assert automation.trigger("test.event", context={}) == []


@pytest.mark.django_db
def test_condition_filters(character):
    _rule(condition={"days_of_fuel_lt": 3})
    assert automation.trigger("test.event", context={"days_of_fuel": 2}, source_object_id="a") != []
    assert automation.trigger("test.event", context={"days_of_fuel": 5}, source_object_id="b") == []


@pytest.mark.django_db
def test_cooldown_suppresses_refire(character):
    _rule(cooldown_minutes=60)
    automation.trigger("test.event", context={}, source_object_id="a", dedup_suffix="1")
    automation.trigger("test.event", context={}, source_object_id="a", dedup_suffix="2")
    assert Alert.objects.filter(source="automation").count() == 1


@pytest.mark.django_db
def test_window_cap(character):
    _rule(max_per_window=2, window_minutes=60)
    for i in range(3):
        automation.trigger("test.event", context={}, source_object_id=str(i))
    assert Alert.objects.filter(source="automation").count() == 2


@pytest.mark.django_db
def test_dry_run_rule_creates_draft(character):
    _rule(dry_run=True)
    ids = automation.trigger("test.event", context={}, source_object_id="7")
    assert Alert.objects.get(pk=ids[0]).status == AlertStatus.DRAFT


@pytest.mark.django_db
def test_expired_rule_does_not_fire(character):
    _rule(expires_at=timezone.now() - dt.timedelta(hours=1))
    assert automation.trigger("test.event", context={}) == []


@pytest.mark.django_db
def test_automation_idempotent(character):
    _rule(cooldown_minutes=0)
    automation.trigger("test.event", context={}, source_object_id="7", dedup_suffix="submitted")
    automation.trigger("test.event", context={}, source_object_id="7", dedup_suffix="submitted")
    assert Alert.objects.filter(source="automation").count() == 1


# --- urgent / large-audience governance --------------------------------------
@pytest.mark.django_db
def test_dispatch_requirements():
    _corp_user(1)
    _corp_user(2)
    req = services.dispatch_requirements("emergency", "emergency", {"kind": "corp"})
    assert req["needs_two_step"] is True and req["needs_reason"] is True
    assert req["estimated_recipients"] == 2


@pytest.mark.django_db
def test_large_audience_gate():
    config.set("anti_abuse", {"large_audience_threshold": 1})
    _corp_user(1)
    _corp_user(2)  # est = 2 > 1
    with pytest.raises(ValueError):
        services.emit_alert(category="announcement", title="t", body="b",
                            audience={"kind": "corp"}, channels=["in_app"])
    ok = services.emit_alert(category="announcement", title="t", body="b",
                             audience={"kind": "corp"}, channels=["in_app"],
                             confirmation={"large_audience_ack": True})
    assert ok is not None


@pytest.mark.django_db
def test_approval_required_category_holds_as_draft(character):
    config.set("anti_abuse", {"approval_required_categories": ["announcement"]})
    a = services.emit_alert(category="announcement", title="t", body="b",
                            audience={"kind": "corp"}, channels=["in_app"], created_by=None)
    assert a.status == AlertStatus.DRAFT  # held for approval
    # with an approver it queues
    b = services.emit_alert(category="announcement", title="t2", body="b2",
                            audience={"kind": "corp"}, channels=["in_app"],
                            confirmation={"approved_by": 1})
    assert b.status == AlertStatus.QUEUED


@pytest.mark.django_db
def test_service_emitted_bypasses_manual_gates(character):
    # a service-emitted urgent alert is not subject to the manual two-step gate
    config.set("anti_abuse", {"large_audience_threshold": 0})
    a = services.emit_alert(category="emergency", title="auto", body="b", priority="emergency",
                            source_service="structures", source_object_id="1",
                            channels=["in_app"], audience={"kind": "corp"})
    assert a is not None and a.status == AlertStatus.QUEUED
