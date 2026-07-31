"""The dependency-CVE *response* loop — detection was never the weak link.

The weekly scan did its job: it found Pillow 11.3.0's 25 advisories and raised a
severity-80 director finding. The finding was then never read, and it went on claiming
25 open vulnerabilities for days after the fix shipped, because nothing pushed it at a
person and nothing re-scanned until the following Monday.

So these tests are about the loop, not the scanner (``test_dependency_audit.py`` covers
the scan itself). Acceptance:

* a fix clears its own finding at the end of the deploy that shipped it;
* a failed scan never clears anything and is never reported as news;
* a new or worsened finding reaches a human exactly once per change — a repeat is
  silent, because a daily "still 3 CVEs" ping is how a channel gets muted;
* leadership can silence the relay without disabling the scan;
* a pingboard fault can never lose a scan result;
* the stored result says how stale it is, so "clean" and "we haven't looked in eight
  days" are never rendered the same.

pip-audit is mocked at the subprocess boundary, so these are fast and need no network.
"""
from __future__ import annotations

import json
import subprocess
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.admin_audit import dependency_audit as da
from apps.admin_audit import tasks as audit_tasks
from apps.admin_audit.models import AppSetting
from apps.pingboard import config
from apps.pingboard.models import Alert
from apps.recommendations.models import Recommendation

pytestmark = pytest.mark.django_db

_OPEN = [Recommendation.State.NEW, Recommendation.State.ACKNOWLEDGED]


@pytest.fixture(autouse=True)
def _reset_notifications_config():
    """The relay rides the ADM-3 event policy; leave no override behind."""
    config.reset("notifications")
    config.reset("general")
    yield
    config.reset("notifications")
    config.reset("general")


# --- fixtures for the mocked scanner -----------------------------------------
def _proc(payload, returncode=0):
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess(args=["pip-audit"], returncode=returncode,
                                       stdout=stdout, stderr="")


def _deps(*vulns):
    """A pip-audit payload with one clean package plus the given advisory ids."""
    return {"dependencies": [
        {"name": "django", "version": "5.2.15", "vulns": []},
        {"name": "pillow", "version": "11.3.0", "vulns": [
            {"id": vid, "fix_versions": ["12.3.0"], "description": "bad"} for vid in vulns
        ]},
    ]}


def _scan_returns(monkeypatch, payload=None, exc=None, returncode=0):
    def fake(*a, **k):
        if exc is not None:
            raise exc
        return _proc(payload, returncode)
    monkeypatch.setattr(da, "_run_pip_audit", fake)


_ONE_CVE = _deps("CVE-2026-1111")
_TWO_CVE = _deps("CVE-2026-1111", "CVE-2026-2222")
_CLEAN = {"dependencies": [{"name": "django", "version": "5.2.15", "vulns": []}]}


def _finding():
    return Recommendation.objects.filter(
        subject_type="security", subject_id="dependency_audit", state__in=_OPEN
    ).first()


def _relayed_alerts():
    return Alert.objects.filter(source_service="admin_audit")


# --- 1. a fix clears its own finding -----------------------------------------
def test_deploy_run_clears_the_finding_the_deploy_fixed(monkeypatch):
    _scan_returns(monkeypatch, _ONE_CVE, returncode=1)
    assert audit_tasks.audit_dependencies()["transition"] == "opened"
    assert _finding() is not None

    # The release that bumps the package re-runs the loop on its way out.
    _scan_returns(monkeypatch, _CLEAN)
    result = audit_tasks.audit_dependencies(trigger=da.TRIGGER_DEPLOY)

    assert result["transition"] == "cleared"
    assert _finding() is None, "a fixed CVE must not keep claiming to be open"
    stored = AppSetting.get(da.AUDIT_SETTING_KEY)
    assert stored["status"] == "ok" and stored["trigger"] == "deploy"


def test_management_command_is_the_deploy_entry_point(monkeypatch, capsys):
    """The deploy hook is `manage.py audit_dependencies --exit-zero --trigger deploy`:
    it must run the whole loop (not just the scan) and must not fail the deploy."""
    from django.core.management import call_command

    _scan_returns(monkeypatch, _ONE_CVE, returncode=1)
    audit_tasks.audit_dependencies()
    assert _finding() is not None

    _scan_returns(monkeypatch, _CLEAN)
    call_command("audit_dependencies", "--exit-zero", "--trigger", "deploy")  # no SystemExit

    assert _finding() is None
    assert "Finding: cleared" in capsys.readouterr().out


def test_clean_scan_with_no_open_finding_is_a_noop(monkeypatch):
    _scan_returns(monkeypatch, _CLEAN)
    assert audit_tasks.audit_dependencies()["transition"] == "clean"
    assert not _relayed_alerts().exists()


# --- 2. a failed scan is never "all clear" -----------------------------------
def test_error_scan_never_clears_and_never_relays(monkeypatch):
    _scan_returns(monkeypatch, _ONE_CVE, returncode=1)
    audit_tasks.audit_dependencies()
    before = _relayed_alerts().count()

    _scan_returns(monkeypatch, exc=FileNotFoundError("pip-audit gone"))
    result = audit_tasks.audit_dependencies()

    assert result["transition"] == "error" and result["relayed"] is False
    assert _finding() is not None, "a failed scan must never be mistaken for a fix"
    assert _relayed_alerts().count() == before, "an error is not news"


def test_error_scan_reads_as_unknown_not_healthy(monkeypatch):
    _scan_returns(monkeypatch, exc=OSError("network down"))
    audit_tasks.audit_dependencies()
    fresh = da.audit_freshness()
    assert fresh["status"] == "error" and fresh["effective_status"] == "unknown"


# --- 3. the relay: once per change, silent otherwise -------------------------
def test_new_finding_reaches_a_human(monkeypatch):
    _scan_returns(monkeypatch, _ONE_CVE, returncode=1)
    result = audit_tasks.audit_dependencies()

    assert result["relayed"] is True
    assert _relayed_alerts().count() == 1
    assert "CVE-2026-1111" in _relayed_alerts().first().body


def test_repeat_finding_is_silent(monkeypatch):
    _scan_returns(monkeypatch, _ONE_CVE, returncode=1)
    audit_tasks.audit_dependencies()
    assert _relayed_alerts().count() == 1

    # Same CVE set tomorrow, and the day after: not news, no second ping.
    for _ in range(2):
        result = audit_tasks.audit_dependencies()
        assert result["transition"] == "unchanged" and result["relayed"] is False
    assert _relayed_alerts().count() == 1


def test_an_unchanged_scan_does_not_even_wake_the_relay(monkeypatch):
    """Belt and braces. The delivery fabric dedupes too, so "no second alert" alone
    would still pass if this guard rotted — assert the relay is not even consulted, and
    the daily beat therefore costs one scan, not a scan plus a health sweep."""
    from apps.admin_audit import health_alert

    calls = []
    monkeypatch.setattr(health_alert, "scan_integration_health",
                        lambda: calls.append(1) or {"status": "alerted"})

    _scan_returns(monkeypatch, _ONE_CVE, returncode=1)
    audit_tasks.audit_dependencies()   # opened  → relay
    audit_tasks.audit_dependencies()   # unchanged → silent
    audit_tasks.audit_dependencies()   # unchanged → silent
    assert calls == [1]

    _scan_returns(monkeypatch, _CLEAN)
    audit_tasks.audit_dependencies()   # cleared → relay (resets the dedup state)
    assert calls == [1, 1]


def test_worsened_finding_relays_again(monkeypatch):
    _scan_returns(monkeypatch, _ONE_CVE, returncode=1)
    audit_tasks.audit_dependencies()

    _scan_returns(monkeypatch, _TWO_CVE, returncode=1)
    result = audit_tasks.audit_dependencies()

    assert result["transition"] == "worsened" and result["relayed"] is True
    assert _relayed_alerts().count() == 2
    assert "CVE-2026-2222" in _relayed_alerts().order_by("-id").first().body


def test_a_repeat_does_not_reopen_an_acknowledged_finding(monkeypatch):
    """A director working the fix must not have it flipped back to NEW every night —
    that is the same cry-wolf failure as a nightly ping, just on-site."""
    _scan_returns(monkeypatch, _ONE_CVE, returncode=1)
    audit_tasks.audit_dependencies()
    rec = _finding()
    rec.state = Recommendation.State.ACKNOWLEDGED
    rec.save(update_fields=["state"])

    audit_tasks.audit_dependencies()
    rec.refresh_from_db()
    assert rec.state == Recommendation.State.ACKNOWLEDGED

    # A genuinely worse set does re-surface it.
    _scan_returns(monkeypatch, _TWO_CVE, returncode=1)
    audit_tasks.audit_dependencies()
    rec.refresh_from_db()
    assert rec.state == Recommendation.State.NEW


# --- 4. leadership control + fault isolation ---------------------------------
def test_leadership_can_silence_the_relay_without_disabling_the_scan(monkeypatch):
    config.set("notifications", {"events": {"admin_audit.integration_health": {"enabled": False}}})
    _scan_returns(monkeypatch, _ONE_CVE, returncode=1)

    result = audit_tasks.audit_dependencies()

    assert result["relayed"] is False
    assert not _relayed_alerts().exists()
    # The scan itself is untouched: result stored, finding raised.
    assert result["status"] == "vulnerable"
    assert AppSetting.get(da.AUDIT_SETTING_KEY)["vuln_count"] == 1
    assert _finding() is not None


def test_relay_can_be_skipped_for_ephemeral_databases(monkeypatch):
    _scan_returns(monkeypatch, _ONE_CVE, returncode=1)
    result = audit_tasks.audit_dependencies(relay=False)
    assert result["relayed"] is False and not _relayed_alerts().exists()
    assert _finding() is not None


def test_pingboard_failure_cannot_lose_the_scan_result(monkeypatch):
    """A notification fault is not allowed to cost us the finding."""
    from apps.admin_audit import health_alert

    def boom():
        raise RuntimeError("pingboard exploded")

    monkeypatch.setattr(health_alert, "scan_integration_health", boom)
    _scan_returns(monkeypatch, _ONE_CVE, returncode=1)

    result = audit_tasks.audit_dependencies()

    assert result["status"] == "vulnerable" and result["relayed"] is False
    assert AppSetting.get(da.AUDIT_SETTING_KEY)["vuln_count"] == 1
    assert _finding() is not None


def test_a_provider_crash_cannot_lose_the_scan_result(monkeypatch):
    """The same, one layer deeper: the channel provider itself blowing up."""
    from apps.pingboard import services as pingboard

    def boom(*a, **k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(pingboard, "emit_broadcast", boom)
    _scan_returns(monkeypatch, _ONE_CVE, returncode=1)

    result = audit_tasks.audit_dependencies()

    assert result["status"] == "vulnerable" and result["relayed"] is False
    assert _finding() is not None


# --- 5. the stored result is self-describing about staleness -----------------
def test_fresh_clean_scan_reads_as_clean(monkeypatch):
    _scan_returns(monkeypatch, _CLEAN)
    audit_tasks.audit_dependencies()

    fresh = da.audit_freshness()
    assert fresh["status"] == "ok" and fresh["effective_status"] == "ok"
    assert fresh["stale"] is False and fresh["age_seconds"] < 60
    assert fresh["trigger"] == "scheduled"


def test_an_eight_day_old_clean_scan_reads_as_unknown(monkeypatch):
    """The bug this exists to prevent: rendering a week-old row as if it were news."""
    _scan_returns(monkeypatch, _CLEAN)
    audit_tasks.audit_dependencies()
    row = AppSetting.objects.get(key=da.AUDIT_SETTING_KEY)
    row.value = {**row.value, "as_of": (timezone.now() - timedelta(days=8)).isoformat()}
    row.save(update_fields=["value"])

    fresh = da.audit_freshness()
    assert fresh["status"] == "ok", "the scan's own verdict is preserved"
    assert fresh["stale"] is True and fresh["effective_status"] == "unknown"
    assert fresh["age_seconds"] > 7 * 24 * 3600


def test_a_missed_run_is_not_immediately_stale(monkeypatch):
    """One skipped run (worker restart, OSV outage) must not flip the surface to
    unknown — otherwise the staleness signal itself becomes noise."""
    _scan_returns(monkeypatch, _CLEAN)
    audit_tasks.audit_dependencies()
    row = AppSetting.objects.get(key=da.AUDIT_SETTING_KEY)
    row.value = {**row.value, "as_of": (timezone.now() - timedelta(hours=30)).isoformat()}
    row.save(update_fields=["value"])

    assert da.audit_freshness()["stale"] is False


def test_never_scanned_is_unknown_not_healthy():
    fresh = da.audit_freshness()
    assert fresh["status"] == "never" and fresh["effective_status"] == "unknown"
    assert fresh["stale"] is True and fresh["as_of"] is None


def test_row_carries_its_own_staleness_contract(monkeypatch):
    """A consumer reading an old row judges it against the cadence that wrote it."""
    _scan_returns(monkeypatch, _CLEAN)
    stored = audit_tasks.audit_dependencies()
    assert stored["max_age_hours"] == da.FRESHNESS_MAX_AGE_HOURS
    assert stored["stale_after"] > stored["as_of"]


# --- 6. cadence ---------------------------------------------------------------
def test_dependency_audit_beat_runs_daily():
    """Six days is a long time to carry a known CVE; the beat must not be weekly."""
    from config.celery import app

    entry = app.conf.beat_schedule["audit-dependencies"]
    assert entry["task"] == "admin_audit.audit_dependencies"
    schedule = entry["schedule"]
    assert schedule.day_of_week == set(range(7)), "must fire every day, not one day a week"
    assert schedule.hour == {6} and schedule.minute == {43}, "off-peak, staggered slot"
