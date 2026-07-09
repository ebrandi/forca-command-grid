"""Scheduled-report delivery (P5, doc 14 §6): disarmed default, classification routing,
deliver-once, the min-severity gate, and the load-bearing guarantee that a
director-eyes-only report is NEVER broadcast to a corp-wide Discord channel."""
from __future__ import annotations

import pytest
from django.core.cache import cache

from apps.command_intel import config, notify
from apps.command_intel.models import (
    Classification,
    IntelligenceReport,
    IntelligenceSnapshot,
    OperationalConstraint,
)


@pytest.fixture(autouse=True)
def _clear_config_cache():
    cache.clear()
    yield
    cache.clear()


def _report(classification, *, severity="high"):
    snap = IntelligenceSnapshot.objects.create(slices={"doctrine": {"doctrines": []}})
    report = IntelligenceReport.objects.create(
        snapshot=snap, classification=classification,
        status=IntelligenceReport.Status.READY, title="Posture", summary="Tightening.",
    )
    OperationalConstraint.objects.create(
        snapshot=snap, key="fleet_size.ferox", category="combat", label="Ferox fleet size",
        binding_metric=18, unit="pilots", headroom=-4, severity=severity, status="computed",
    )
    return report


class _Spy:
    def __init__(self, ret=1):
        self.calls = 0
        self.ret = ret
        self.last_classification = None

    def __call__(self, content, *, classification=None):
        self.calls += 1
        self.last_classification = classification
        return self.ret


@pytest.mark.django_db
def test_disarmed_by_default_delivers_nothing(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord", spy)
    report = _report(Classification.HIGH_COMMAND)
    assert notify.deliver_report(report) == {}
    assert spy.calls == 0


@pytest.mark.django_db
def test_broadcasts_when_armed_and_cleared(monkeypatch):
    spy = _Spy(ret=2)
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord", spy)
    config.set("notifications", {"deliver_discord": True,
                                 "discord_classifications": ["corp_internal", "high_command"]})
    report = _report(Classification.HIGH_COMMAND)
    out = notify.deliver_report(report)
    assert out["discord"] == 2
    assert spy.calls == 1
    # The report's classification is passed through so the broadcast honours each
    # channel's max_classification ceiling across every armed chat channel.
    assert spy.last_classification == Classification.HIGH_COMMAND
    report.refresh_from_db()
    assert report.delivered_channels == {"discord": 2}


@pytest.mark.django_db
def test_director_eyes_only_is_never_broadcast(monkeypatch):
    # Even armed, a director tier is not in the (validator-limited) discord list ⇒ no broadcast.
    spy = _Spy()
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord", spy)
    config.set("notifications", {"deliver_discord": True,
                                 "discord_classifications": ["corp_internal", "high_command"]})
    report = _report(Classification.DIRECTOR_EYES_ONLY)
    out = notify.deliver_report(report)
    assert spy.calls == 0
    assert "discord" not in out


@pytest.mark.django_db
def test_deliver_once(monkeypatch):
    spy = _Spy(ret=1)
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord", spy)
    config.set("notifications", {"deliver_discord": True,
                                 "discord_classifications": ["high_command"]})
    report = _report(Classification.HIGH_COMMAND)
    notify.deliver_report(report)
    notify.deliver_report(report)  # second call must not re-post
    assert spy.calls == 1


@pytest.mark.django_db
def test_poisoned_config_still_never_broadcasts_a_director_report(monkeypatch):
    # Defense in depth (MED-1): even if the stored config were poisoned out-of-band to
    # list a forbidden tier for Discord, the delivery sink refuses to broadcast it.
    spy = _Spy()
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord", spy)
    poisoned = {"deliver_discord": True,
                "discord_classifications": ["director_eyes_only", "high_command"],
                "min_severity_to_deliver": "watch"}
    real_get = config.get
    monkeypatch.setattr(config, "get", lambda d: poisoned if d == "notifications" else real_get(d))
    report = _report(Classification.DIRECTOR_EYES_ONLY)
    assert "discord" not in notify.deliver_report(report)
    assert spy.calls == 0


@pytest.mark.django_db
def test_failed_delivery_is_not_recorded_as_delivered(monkeypatch):
    # MED-3: broadcast_discord returning 0 (no webhook / down) must leave the report
    # retriable, not mark it delivered-to-nobody.
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord", lambda content: 0)
    config.set("notifications", {"deliver_discord": True,
                                 "discord_classifications": ["high_command"]})
    report = _report(Classification.HIGH_COMMAND)
    assert "discord" not in notify.deliver_report(report)
    report.refresh_from_db()
    assert report.delivered_channels == {}


@pytest.mark.django_db
def test_min_severity_suppresses_a_quiet_report(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord", spy)
    config.set("notifications", {"deliver_discord": True,
                                 "discord_classifications": ["high_command"],
                                 "min_severity_to_deliver": "high"})
    report = _report(Classification.HIGH_COMMAND, severity="watch")  # below the 'high' floor
    assert notify.deliver_report(report) == {}
    assert spy.calls == 0
