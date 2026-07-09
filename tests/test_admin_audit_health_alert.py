"""ADM-3 (roadmap 2.2) — proactive integration-health & CVE alerting.

Acceptance: a stopped background sync, a stale SDE, or a new dependency CVE fires
exactly one deduped director alert; an unchanged problem set is a no-op; a return to
healthy resets the dedup so a recurrence re-alerts; leadership can switch it off.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.admin_audit.health_alert import scan_integration_health
from apps.admin_audit.models import AppSetting
from apps.pingboard import config
from apps.pingboard.models import Alert
from apps.recommendations.models import Recommendation

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_notifications_config():
    config.reset("notifications")
    config.reset("general")
    yield
    config.reset("notifications")
    config.reset("general")


def _alerts():
    return Alert.objects.filter(source_service="admin_audit")


def _stale_beat(key="corp_assets", days=2):
    at = (timezone.now() - timedelta(days=days)).isoformat()
    AppSetting.objects.update_or_create(
        key=f"sync:{key}", defaults={"value": {"at": at, "by": "test"}}
    )


def _fresh_beat(key="corp_assets"):
    AppSetting.objects.update_or_create(
        key=f"sync:{key}", defaults={"value": {"at": timezone.now().isoformat(), "by": "test"}}
    )


def _open_cve(ids=("CVE-2024-0001",)):
    Recommendation.objects.create(
        type=Recommendation.Type.OFFICER_ACTION,
        subject_type="security", subject_id="dependency_audit",
        state=Recommendation.State.NEW, message="deps vulnerable",
        inputs={"vulns": [
            {"id": i, "name": "pkg", "version": "1.0", "fix_versions": []} for i in ids
        ]},
    )


def test_healthy_is_a_noop():
    _fresh_beat()
    assert scan_integration_health()["status"] == "ok"
    assert not _alerts().exists()


def test_stale_beat_fires_one_alert_then_dedupes():
    _stale_beat("corp_assets", days=2)
    assert scan_integration_health()["status"] == "alerted"
    assert _alerts().count() == 1
    body = _alerts().first().body.lower()
    assert "corp assets" in body and "stopped" in body
    # Same problem set on the next run → no second alert.
    assert scan_integration_health()["status"] == "unchanged"
    assert _alerts().count() == 1


def test_recovery_resets_dedup_and_recurrence_realerts():
    _stale_beat("corp_assets", days=2)
    scan_integration_health()
    assert _alerts().count() == 1
    # Recover: the stored dedup signature is cleared.
    _fresh_beat("corp_assets")
    assert scan_integration_health()["status"] == "ok"
    assert not AppSetting.objects.filter(key="health_alert:sig").exists()
    # A later recurrence of the same problem alerts again (not swallowed).
    _stale_beat("corp_assets", days=2)
    assert scan_integration_health()["status"] == "alerted"
    assert _alerts().count() == 2


def test_a_new_problem_changes_the_signature_and_realerts():
    _stale_beat("corp_assets", days=2)
    scan_integration_health()
    assert _alerts().count() == 1
    # A second sync goes stale → the problem set changed → one more alert.
    _stale_beat("corp_members", days=2)
    assert scan_integration_health()["status"] == "alerted"
    assert _alerts().count() == 2


def test_open_cve_fires_alert():
    _open_cve(("CVE-2024-0001", "CVE-2024-0002"))
    assert scan_integration_health()["status"] == "alerted"
    body = _alerts().first().body
    assert "vulnerab" in body.lower()
    assert "CVE-2024-0001" in body


def test_suppressed_emit_does_not_burn_dedup_and_retries():
    # Pingboard globally off → emit is suppressed (returns None). The dedup slot must
    # NOT be burned, so the next run retries once pingboard is back — a stuck sync is
    # never silently forgotten.
    _stale_beat("corp_assets", days=2)
    config.set("general", {"enabled": False})
    assert scan_integration_health()["status"] == "alert_failed"
    assert not _alerts().exists()
    assert not AppSetting.objects.filter(key="health_alert:sig").exists()
    # Re-enable → the retry actually alerts (not swallowed by a burned dedup slot).
    config.reset("general")
    assert scan_integration_health()["status"] == "alerted"
    assert _alerts().count() == 1


def test_disabled_event_is_a_noop():
    _stale_beat("corp_assets", days=2)
    config.set("notifications", {"events": {"admin_audit.integration_health": {"enabled": False}}})
    assert scan_integration_health()["status"] == "disabled"
    assert not _alerts().exists()
    assert not AppSetting.objects.filter(key="health_alert:sig").exists()
