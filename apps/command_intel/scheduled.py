"""Scheduled (unattended) report generation — the P5 automation spine (doc 18 P5).

The weekly Celery beat fires ``command_intel.scheduled_report``, which lands here. It is
the manual generate path with a different *trigger*: dedupe against a recent scheduled run
(so a flapping/retried beat never burns tokens twice), generate with the **same** pipeline
(narrated when the LLM is up, deterministic ``ready_degraded`` when it is down), then hand a
ready report to the classification-aware notifier. Gated by ``notifications.scheduled_enabled``
so leadership can pause automation without a deploy.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.utils import timezone

from . import config, notify
from .models import IntelligenceReport, Trigger

logger = logging.getLogger("forca.command_intel")

# Don't regenerate if a (non-failed) scheduled report already landed within this window —
# a weekly cadence with slack for a missed or retried beat. Just under a week.
_DEDUPE_DAYS = 6
# A cross-worker lock so a redelivered/retried beat can't run two generations at once
# (the dedupe only protects *sequential* re-runs). Sized well above a slow LLM report.
_LOCK_KEY = "command_intel:scheduled:lock"
_LOCK_TTL = 900  # 15 min


def run_scheduled_report() -> str:
    """Generate + deliver the unattended report. Returns a short status string."""
    cfg = config.get("notifications")
    if not cfg.get("scheduled_enabled", False):
        return "disabled"

    from django.core.cache import cache

    # cache.add is atomic on the prod (Redis) backend: only one concurrent execution
    # acquires the lock; the rest bail so we never double-generate / double-deliver.
    if not cache.add(_LOCK_KEY, "1", _LOCK_TTL):
        return "locked"
    try:
        return _run_locked(cfg)
    finally:
        cache.delete(_LOCK_KEY)


def _run_locked(cfg: dict) -> str:
    since = timezone.now() - dt.timedelta(days=_DEDUPE_DAYS)
    recent = (
        IntelligenceReport.objects.filter(trigger=Trigger.SCHEDULED, created_at__gte=since)
        .exclude(status=IntelligenceReport.Status.FAILED)
        .order_by("-created_at")
        .first()
    )
    if recent is not None:
        # A scheduled report already exists this period. Re-attempt delivery only (the
        # notifier is deliver-once), in case a prior run generated but couldn't deliver.
        notify.deliver_report(recent)
        return f"deduped:{recent.pk}"

    templates = config.get("report_templates")
    template_key = templates.get("default", "posture")
    tmpl = templates["templates"].get(template_key) or {}
    classification = tmpl.get("default_classification") or config.get("classification")["default"]

    report = IntelligenceReport.objects.create(
        template_key=template_key,
        classification=classification,
        status=IntelligenceReport.Status.QUEUED,
        trigger=Trigger.SCHEDULED,
    )

    from .report import run_generation

    run_generation(report)
    report.refresh_from_db()
    if report.status in (IntelligenceReport.Status.READY, IntelligenceReport.Status.READY_DEGRADED):
        notify.deliver_report(report)
    return f"{report.status}:{report.pk}"
