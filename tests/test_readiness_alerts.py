"""Phase 5 — alert evaluation (cooldown/escalation/resolution), weekly report, housekeeping."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.readiness import config
from apps.readiness.alerts import evaluate_alerts
from apps.readiness.models import (
    ExecutiveReport,
    ReadinessAlert,
    ReadinessFinding,
    ReadinessSnapshot,
)
from apps.sso.services import ensure_role
from core import rbac


def _finding(dimension="financial", kpi="financial.runway_months", **kw):
    defaults = dict(
        dimension_key=dimension, kpi_key=kpi, severity="critical", kind="risk",
        title="Runway critical", weight=40.0, owner_tag="finance_officer",
        status=ReadinessFinding.Status.OPEN,
    )
    defaults.update(kw)
    return ReadinessFinding.objects.create(**defaults)


def _rule(**kw):
    rule = {
        "key": "runway_critical",
        "match": {"dimension": "financial"},
        "severity": "critical",
        "channels": ["discord"],
        "cooldown_hours": 24,
    }
    rule.update(kw)
    config.set("alerts", {"rules": [rule]}, user=None)


# --- alert firing & dedupe ---------------------------------------------------
@pytest.mark.django_db
def test_no_rules_fires_nothing():
    _finding()
    assert evaluate_alerts() == 0
    assert ReadinessAlert.objects.count() == 0


@pytest.mark.django_db
def test_matching_rule_fires_once_then_cooldown(monkeypatch):
    sent = []
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord",
                        lambda msg, classification=None: sent.append(msg) or 1)
    _finding()
    _rule()
    assert evaluate_alerts() == 1
    alert = ReadinessAlert.objects.get()
    assert alert.rule_key == "runway_critical" and alert.channels == ["discord"]
    assert len(sent) == 1
    # Re-running over the same open state delivers nothing more (dedupe).
    assert evaluate_alerts() == 0
    assert ReadinessAlert.objects.count() == 1
    assert len(sent) == 1


@pytest.mark.django_db
def test_high_severity_message_includes_owner(monkeypatch):
    sent = []
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord",
                        lambda msg, classification=None: sent.append(msg) or 1)
    config.set("responsibilities", {
        "owner_tags": {"finance_officer": {"label": "Finance Officer", "users": []}},
        "dimension_owner": {"financial": "finance_officer"}, "kpi_owner": {},
    }, user=None)
    _finding()
    _rule()
    evaluate_alerts()
    assert "Finance Officer" in sent[0]
    assert "/readiness/d/financial/" in sent[0]


@pytest.mark.django_db
def test_escalation_fires_once_after_window(monkeypatch):
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord",
                        lambda msg, classification=None: 1)
    _finding()
    _rule(escalate_after_hours=24, escalate_channels=["discord"])
    evaluate_alerts()
    alert = ReadinessAlert.objects.get()
    # Backdate so it's older than the escalation window.
    ReadinessAlert.objects.filter(pk=alert.pk).update(
        created_at=timezone.now() - dt.timedelta(hours=25)
    )
    evaluate_alerts()
    alert.refresh_from_db()
    assert alert.escalated_at is not None
    first_escalation = alert.escalated_at
    # Escalation fires at most once.
    evaluate_alerts()
    alert.refresh_from_db()
    assert alert.escalated_at == first_escalation


@pytest.mark.django_db
def test_resolution_when_finding_clears(monkeypatch):
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord",
                        lambda msg, classification=None: 1)
    finding = _finding()
    _rule()
    evaluate_alerts()
    alert = ReadinessAlert.objects.get()
    assert alert.resolved_at is None
    # The gap clears → the rule no longer matches → alert resolves.
    finding.status = ReadinessFinding.Status.RESOLVED
    finding.save()
    evaluate_alerts()
    alert.refresh_from_db()
    assert alert.resolved_at is not None


@pytest.mark.django_db
def test_cooldown_suppresses_refire_after_resolution(monkeypatch):
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord",
                        lambda msg, classification=None: 1)
    finding = _finding()
    _rule(cooldown_hours=24)
    evaluate_alerts()
    # Resolve it.
    finding.status = ReadinessFinding.Status.RESOLVED
    finding.save()
    evaluate_alerts()
    assert ReadinessAlert.objects.get().resolved_at is not None
    # Gap returns immediately (within cooldown) → no new alert (flap rate-limited).
    finding.status = ReadinessFinding.Status.OPEN
    finding.save()
    assert evaluate_alerts() == 0
    assert ReadinessAlert.objects.count() == 1


# --- weekly report -----------------------------------------------------------
@pytest.mark.django_db
def test_weekly_report_idempotent_per_period():
    from apps.readiness.report import weekly_report

    ReadinessSnapshot.objects.create(index=72, dimensions={"doctrine": 80, "financial": 40})
    end = timezone.now().date()
    start = end - dt.timedelta(days=7)
    weekly_report(period_start=start, period_end=end)
    weekly_report(period_start=start, period_end=end)  # re-run updates in place
    assert ExecutiveReport.objects.filter(period_start=start, period_end=end).count() == 1
    report = ExecutiveReport.objects.get(period_start=start, period_end=end)
    assert report.index == 72
    assert "delivered_channels" in {f.name for f in ExecutiveReport._meta.fields}


# --- housekeeping ------------------------------------------------------------
@pytest.mark.django_db
def test_housekeeping_prunes_by_age():
    from apps.readiness.tasks import housekeeping

    old = timezone.now() - dt.timedelta(days=200)
    # An old resolved finding (>90d) and an old RESOLVED alert (>180d) are pruned.
    f = _finding(status=ReadinessFinding.Status.RESOLVED)
    ReadinessFinding.objects.filter(pk=f.pk).update(last_seen=old)
    a = ReadinessAlert.objects.create(rule_key="x", summary="old", resolved_at=old)
    ReadinessAlert.objects.filter(pk=a.pk).update(created_at=old)
    # An old but still-OPEN alert is KEPT (deleting it would re-fire the issue).
    open_old = ReadinessAlert.objects.create(rule_key="y", summary="still open")
    ReadinessAlert.objects.filter(pk=open_old.pk).update(created_at=old)
    # A recent open finding is kept.
    keep = _finding(kpi="financial.burn", status=ReadinessFinding.Status.OPEN)

    counts = housekeeping()
    assert counts["findings"] == 1 and counts["alerts"] == 1
    assert ReadinessFinding.objects.filter(pk=keep.pk).exists()
    assert ReadinessAlert.objects.filter(pk=open_old.pk).exists()  # open alert survives


@pytest.mark.django_db
def test_acknowledged_finding_does_not_resolve_its_alert(monkeypatch):
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord",
                        lambda msg, classification=None: 1)
    finding = _finding()
    _rule()
    evaluate_alerts()
    # Officer acknowledges (working it) but the gap is NOT fixed → alert stays open.
    finding.status = ReadinessFinding.Status.ACKNOWLEDGED
    finding.save()
    evaluate_alerts()
    assert ReadinessAlert.objects.get().resolved_at is None


@pytest.mark.django_db
def test_weekly_report_does_not_rebroadcast(monkeypatch):
    from apps.readiness.report import weekly_report

    sent = []
    monkeypatch.setattr("apps.recommendations.notify.broadcast_discord",
                        lambda msg, classification=None: sent.append(msg) or 1)
    ReadinessSnapshot.objects.create(index=72, dimensions={"doctrine": 80})
    end = timezone.now().date()
    start = end - dt.timedelta(days=7)
    weekly_report(period_start=start, period_end=end)
    weekly_report(period_start=start, period_end=end)  # re-run must NOT re-broadcast
    assert len(sent) == 1


# --- view --------------------------------------------------------------------
@pytest.mark.django_db
def test_alerts_log_is_officer_only(client, django_user_model):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get("/readiness/alerts/").status_code == 403


@pytest.mark.django_db
def test_alerts_log_renders(client, django_user_model):
    officer = django_user_model.objects.create(username="off")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    ReadinessAlert.objects.create(rule_key="runway_critical", severity="critical", summary="x")
    client.force_login(officer)
    html = client.get("/readiness/alerts/").content.decode()
    assert "Alerts &amp; executive reports" in html
    assert "runway_critical" in html
