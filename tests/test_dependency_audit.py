"""Tests for the scheduled dependency-vulnerability audit (R-2 remediation).

pip-audit itself is mocked at the subprocess boundary so these are fast,
deterministic, and need no network or the tool installed.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from apps.admin_audit import dependency_audit as da
from apps.admin_audit.models import AppSetting
from apps.recommendations.models import Recommendation

_OPEN = [Recommendation.State.NEW, Recommendation.State.ACKNOWLEDGED]


def _fake_proc(payload, returncode=0, stderr=""):
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess(args=["pip-audit"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _patch_pip_audit(monkeypatch, proc=None, exc=None):
    def fake(*a, **k):
        if exc is not None:
            raise exc
        return proc
    monkeypatch.setattr(da, "_run_pip_audit", fake)


_CLEAN = {"dependencies": [
    {"name": "django", "version": "5.2.15", "vulns": []},
    {"name": "requests", "version": "2.34.0", "vulns": []},
]}
_VULN = {"dependencies": [
    {"name": "django", "version": "5.2.15", "vulns": []},
    {"name": "evilpkg", "version": "1.0.0", "vulns": [
        {"id": "GHSA-xxxx-yyyy", "fix_versions": ["1.0.1"], "description": "bad"},
    ]},
]}


# --- run_dependency_audit ----------------------------------------------------
@pytest.mark.django_db
def test_audit_clean(monkeypatch):
    _patch_pip_audit(monkeypatch, proc=_fake_proc(_CLEAN))
    summary = da.run_dependency_audit()
    assert summary["status"] == "ok"
    assert summary["vuln_count"] == 0 and summary["package_count"] == 2
    # Persisted for the ops surface.
    assert AppSetting.get(da.AUDIT_SETTING_KEY)["status"] == "ok"


@pytest.mark.django_db
def test_audit_vulnerable(monkeypatch):
    # pip-audit exits non-zero when it finds vulns — status must key off JSON, not rc.
    _patch_pip_audit(monkeypatch, proc=_fake_proc(_VULN, returncode=1))
    summary = da.run_dependency_audit()
    assert summary["status"] == "vulnerable" and summary["vuln_count"] == 1
    v = summary["vulns"][0]
    assert v["name"] == "evilpkg" and v["id"] == "GHSA-xxxx-yyyy" and v["fix_versions"] == ["1.0.1"]


@pytest.mark.django_db
def test_audit_tool_missing_is_error_not_crash(monkeypatch):
    _patch_pip_audit(monkeypatch, exc=FileNotFoundError("pip-audit not installed"))
    summary = da.run_dependency_audit()
    assert summary["status"] == "error" and summary["vuln_count"] == 0


@pytest.mark.django_db
def test_audit_unparseable_output_is_error(monkeypatch):
    _patch_pip_audit(monkeypatch, proc=_fake_proc("not json", returncode=2, stderr="boom"))
    summary = da.run_dependency_audit()
    assert summary["status"] == "error"


# --- Celery task: Recommendation surfacing ----------------------------------
def _open_finding():
    return Recommendation.objects.filter(
        subject_type="security", subject_id="dependency_audit", state__in=_OPEN
    ).first()


@pytest.mark.django_db
def test_task_creates_director_recommendation_on_findings(monkeypatch):
    from apps.admin_audit.tasks import audit_dependencies

    _patch_pip_audit(monkeypatch, proc=_fake_proc(_VULN, returncode=1))
    audit_dependencies()
    rec = _open_finding()
    assert rec is not None
    assert rec.required_permission == "director" and rec.severity >= 50
    assert "evilpkg" in rec.message


@pytest.mark.django_db
def test_task_is_idempotent_no_duplicate_findings(monkeypatch):
    from apps.admin_audit.tasks import audit_dependencies

    _patch_pip_audit(monkeypatch, proc=_fake_proc(_VULN, returncode=1))
    audit_dependencies()
    audit_dependencies()
    assert Recommendation.objects.filter(
        subject_type="security", subject_id="dependency_audit", state__in=_OPEN
    ).count() == 1


@pytest.mark.django_db
def test_task_clears_finding_when_clean(monkeypatch):
    from apps.admin_audit.tasks import audit_dependencies

    _patch_pip_audit(monkeypatch, proc=_fake_proc(_VULN, returncode=1))
    audit_dependencies()
    assert _open_finding() is not None
    # A later clean scan retires the finding.
    _patch_pip_audit(monkeypatch, proc=_fake_proc(_CLEAN))
    audit_dependencies()
    assert _open_finding() is None


@pytest.mark.django_db
def test_task_error_scan_does_not_clear_finding(monkeypatch):
    from apps.admin_audit.tasks import audit_dependencies

    _patch_pip_audit(monkeypatch, proc=_fake_proc(_VULN, returncode=1))
    audit_dependencies()
    # A failed scan must NOT be mistaken for "all clear".
    _patch_pip_audit(monkeypatch, exc=FileNotFoundError("gone"))
    audit_dependencies()
    assert _open_finding() is not None


# --- Management command (CI gate) -------------------------------------------
@pytest.mark.django_db
def test_command_exits_nonzero_on_findings(monkeypatch):
    from django.core.management import call_command

    _patch_pip_audit(monkeypatch, proc=_fake_proc(_VULN, returncode=1))
    with pytest.raises(SystemExit) as exc:
        call_command("audit_dependencies")
    assert exc.value.code == 1


@pytest.mark.django_db
def test_command_exit_zero_flag_never_fails(monkeypatch):
    from django.core.management import call_command

    _patch_pip_audit(monkeypatch, proc=_fake_proc(_VULN, returncode=1))
    call_command("audit_dependencies", "--exit-zero")  # must not raise


@pytest.mark.django_db
def test_command_clean_exits_zero(monkeypatch):
    from django.core.management import call_command

    _patch_pip_audit(monkeypatch, proc=_fake_proc(_CLEAN))
    call_command("audit_dependencies")  # no SystemExit
