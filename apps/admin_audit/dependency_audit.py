"""Scheduled dependency-vulnerability audit.

Runs ``pip-audit`` against the installed environment and records a structured
summary so a newly-disclosed CVE in a dependency surfaces on-site (and in the
logs) without waiting for a manual review — the recurring control for the
"dependencies go stale between one-off scans" residual risk.

Pure-ish: shells out to pip-audit and writes one ``AppSetting`` row. The Celery
task (``admin_audit.audit_dependencies``) calls this and raises a director
Recommendation on findings; the ``audit_dependencies`` management command wraps it
for CI / manual use.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys

from django.utils import timezone

from .models import AppSetting

log = logging.getLogger("forca.security")

# Where the latest scan result is stored (read by the ops/health surface).
AUDIT_SETTING_KEY = "security:dependency_audit"
_TIMEOUT = 300  # seconds — pip-audit queries the OSV/PyPI advisory DB over the network
_MAX_REPORTED = 100  # cap stored/relayed vulns so a pathological result can't bloat the row


def _run_pip_audit() -> subprocess.CompletedProcess:
    """Audit the installed environment, JSON to stdout. pip-audit exits non-zero
    when it finds vulnerabilities, so the caller keys off parsed JSON, not the code."""
    # S603: the argument vector is entirely static literals plus sys.executable —
    # no shell, no user/request-derived input — so there is nothing to inject.
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pip_audit", "--format", "json", "--progress-spinner", "off"],
        capture_output=True, text=True, timeout=_TIMEOUT, check=False,
    )


def _parse(stdout: str) -> tuple[list[dict], int]:
    """Return (vulnerable-package list, total package count) from pip-audit JSON."""
    data = json.loads(stdout)
    deps = data.get("dependencies", []) if isinstance(data, dict) else data
    vulns: list[dict] = []
    for dep in deps:
        for v in dep.get("vulns", []) or []:
            vulns.append({
                "name": dep.get("name", ""),
                "version": dep.get("version", ""),
                "id": v.get("id", ""),
                "fix_versions": v.get("fix_versions", []) or [],
            })
    return vulns, len(deps)


def _persist(summary: dict) -> None:
    AppSetting.objects.update_or_create(key=AUDIT_SETTING_KEY, defaults={"value": summary})


def run_dependency_audit() -> dict:
    """Run the scan, persist a summary to ``AppSetting``, and return it.

    Never raises — a missing tool, network failure, or unparseable output is
    reported as ``status='error'`` so the weekly scheduler keeps running and an
    error can be distinguished from a clean result (we must not falsely clear an
    open finding just because a scan failed).
    """
    summary: dict = {"as_of": timezone.now().isoformat(), "tool": "pip-audit"}
    try:
        proc = _run_pip_audit()
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        summary.update(status="error", error=str(exc)[:300], vuln_count=0, vulns=[])
        log.error("dependency audit could not run: %s", exc)
        _persist(summary)
        return summary

    try:
        vulns, pkg_count = _parse(proc.stdout)
    except (ValueError, AttributeError, TypeError) as exc:
        summary.update(
            status="error", vuln_count=0, vulns=[],
            error=f"unparseable pip-audit output: {exc}"[:300],
        )
        log.error("dependency audit output not parseable (rc=%s): %s", proc.returncode,
                  (proc.stderr or "")[:300])
        _persist(summary)
        return summary

    summary.update(
        status="vulnerable" if vulns else "ok",
        vuln_count=len(vulns),
        package_count=pkg_count,
        vulns=vulns[:_MAX_REPORTED],
    )
    if vulns:
        log.error(
            "dependency audit found %d vulnerabilit%s: %s",
            len(vulns), "y" if len(vulns) == 1 else "ies",
            ", ".join(f"{v['name']} {v['version']} ({v['id']})" for v in vulns[:20]),
        )
    else:
        log.info("dependency audit clean: %d packages, no known vulnerabilities", pkg_count)
    _persist(summary)
    return summary
