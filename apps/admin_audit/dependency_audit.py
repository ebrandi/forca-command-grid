"""Scheduled dependency-vulnerability audit — the *scan* half of the CVE response loop.

Runs ``pip-audit`` against the installed environment and records a structured
summary so a newly-disclosed CVE in a dependency surfaces on-site (and in the
logs) without waiting for a manual review — the recurring control for the
"dependencies go stale between one-off scans" residual risk.

Deliberately audits the **installed environment of the running container**, not a
freshly-resolved requirements file: a lockfile says what we *intended* to deploy, and
a stale image is exactly the failure mode this control exists to catch.

Pure-ish: shells out to pip-audit and writes one ``AppSetting`` row. Everything that
*responds* to the result lives one layer up in :mod:`apps.admin_audit.tasks` — the
director Recommendation and the human relay — so there is one scan authority and one
response authority. The ``audit_dependencies`` management command drives the whole loop
for CI, manual and end-of-deploy use.

The stored row is **self-describing about staleness** (``as_of`` + ``max_age_hours`` +
``stale_after`` + ``trigger``), because a result is only as trustworthy as it is fresh:
a consumer must be able to tell "scanned 20 minutes ago, clean" from "scanned 8 days
ago, so we simply do not know". :func:`audit_freshness` is that read model.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import AppSetting

log = logging.getLogger("forca.security")

# Where the latest scan result is stored (read by the ops/health surface).
AUDIT_SETTING_KEY = "security:dependency_audit"
_TIMEOUT = 300  # seconds — pip-audit queries the OSV/PyPI advisory DB over the network
_MAX_REPORTED = 100  # cap stored/relayed vulns so a pathological result can't bloat the row

# How long a scan result stays trustworthy. The beat runs daily (06:43 UTC), so 36h
# leaves a full 12h grace: one missed run (worker restart, OSV outage) does NOT flip the
# surface to "unknown", two consecutive misses do. Stamped into every row so a consumer
# reading an old row judges it against the cadence that produced it, not today's.
FRESHNESS_MAX_AGE_HOURS = 36.0

# Who asked for this scan. Recorded so ops can tell a routine nightly result from the
# one a deploy just produced ("scanned 20 minutes ago, right after the fix shipped").
TRIGGER_SCHEDULED = "scheduled"
TRIGGER_DEPLOY = "deploy"
TRIGGER_MANUAL = "manual"


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


def run_dependency_audit(*, trigger: str = TRIGGER_SCHEDULED) -> dict:
    """Run the scan, persist a summary to ``AppSetting``, and return it.

    ``trigger`` records *why* this scan ran (``scheduled`` / ``deploy`` / ``manual``);
    it is stored, not acted on, so the ops surface can say "scanned right after the
    deploy" rather than leaving a reader to guess from the timestamp.

    Never raises — a missing tool, network failure, or unparseable output is
    reported as ``status='error'`` so the scheduler keeps running and an error can be
    distinguished from a clean result (we must not falsely clear an open finding just
    because a scan failed).
    """
    now = timezone.now()
    summary: dict = {
        "as_of": now.isoformat(),
        "tool": "pip-audit",
        "trigger": trigger,
        # Staleness contract, stamped at write time so the row is self-describing.
        "max_age_hours": FRESHNESS_MAX_AGE_HOURS,
        "stale_after": (now + timedelta(hours=FRESHNESS_MAX_AGE_HOURS)).isoformat(),
    }
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


def result_freshness(
    value: dict | None, *, default_max_age_hours: float, extra: dict | None = None
) -> dict:
    """How much a stored scan result is worth *right now* — the generic ops read model.

    A security control that reports "clean" from a scan nobody has run for eight days is
    lying by omission: the honest answer is "unknown since". This turns a raw stored row
    into that distinction so every surface makes it the same way instead of each
    re-deriving an age threshold (one authority per concept).

    Deliberately generic and shared: the Python dependency audit and the container-image
    scan (:mod:`apps.admin_audit.image_scan`) both store a timestamped ``status`` row and
    both need the same "is this still worth believing" judgement. Two copies of an age
    threshold is exactly how one of them quietly starts painting a week-old row green.

    Returns ``status`` (the scan's own verdict, or ``never`` when nothing has ever run),
    ``as_of`` / ``age_seconds`` / ``stale`` / ``max_age_hours``, and ``effective_status``
    — which collapses "stale" and "error" into ``unknown``, because in both cases we
    genuinely do not know the state of the deployed system and must not paint it green.
    ``extra`` is merged in for the per-control detail a surface wants beside the verdict.
    """
    value = value or {}
    payload = dict(extra or {})

    status = value.get("status") or "never"
    max_age = float(value.get("max_age_hours") or default_max_age_hours)
    as_of = parse_datetime(value.get("as_of") or "") if value.get("as_of") else None
    if as_of is not None and timezone.is_naive(as_of):
        as_of = timezone.make_aware(as_of, timezone.get_default_timezone())

    if as_of is None:
        # No scan on record (or an unreadable stamp) — never claim health from silence.
        payload.update(
            status=status, effective_status="unknown", stale=True,
            as_of=None, age_seconds=None, max_age_hours=max_age,
        )
        return payload

    age = (timezone.now() - as_of).total_seconds()
    stale = age > max_age * 3600
    payload.update(
        status=status,
        effective_status="unknown" if (stale or status == "error") else status,
        stale=stale,
        as_of=as_of,
        age_seconds=int(age),
        max_age_hours=max_age,
    )
    return payload


def audit_freshness(value: dict | None = None) -> dict:
    """:func:`result_freshness` for the dependency audit, plus its own detail fields.

    Reads the persisted row by default; pass ``value`` to classify a summary in hand
    (e.g. the one a scan just returned) without a second query.
    """
    if value is None:
        value = AppSetting.get(AUDIT_SETTING_KEY) or {}

    return result_freshness(
        value,
        default_max_age_hours=FRESHNESS_MAX_AGE_HOURS,
        extra={
            "vuln_count": int(value.get("vuln_count") or 0),
            "package_count": value.get("package_count"),
            "trigger": value.get("trigger") or "",
            "error": value.get("error") or "",
        },
    )
