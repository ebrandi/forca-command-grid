"""Campaign Command Celery tasks (design doc 08).

Three thin ``@shared_task`` wrappers; all logic lives in ``apps.campaigns.services`` /
``apps.campaigns.metrics`` so it is unit-testable without a broker. Every task shares the same
prologue (doc 08 §2): the **feature check first** (a disabled subsystem costs one cache read per
tick, nothing more — acceptance criterion 33), the ``cache.add`` cross-worker lock second (its TTL
a hard upper bound on runtime so a crashed worker cannot wedge the schedule), the lazy service
import third. There is deliberately no retry API — every task is a sweep over a due-table and a
re-run converges (the house idiom, ``acks_late`` + idempotency keys); ``cache.delete`` in
``finally`` releases the lock early on normal completion.
"""
from __future__ import annotations

import uuid

from celery import shared_task

_REFRESH_LOCK = "campaigns:lock:refresh_metrics"
_REFRESH_LOCK_TTL = 600  # 10 min — bounds the metric sweep's runtime
_SWEEP_LOCK = "campaigns:lock:sweep_deadlines"
_SWEEP_LOCK_TTL = 300  # 5 min
_HOUSEKEEPING_LOCK = "campaigns:lock:housekeeping"
_HOUSEKEEPING_LOCK_TTL = 1800  # 30 min


def _release_lock(key: str, token: str) -> None:
    """Release a beat lock only if we still own it — a compare-and-delete so a run that overran its
    TTL (and whose lock a successor has since re-acquired) never deletes the successor's lock and
    lets two sweeps run concurrently, duplicating samples/activity. The small get→delete race is
    acceptable versus a blind ``cache.delete`` (#33)."""
    from django.core.cache import cache

    if cache.get(key) == token:
        cache.delete(key)


@shared_task(name="campaigns.refresh_metrics")
def refresh_metrics():
    """Re-measure ACTIVE campaigns' due auto objectives; recompute progress/health (doc 08 §2.1)."""
    from core.features import feature_enabled

    if not feature_enabled("campaigns"):
        return {"status": "feature_disabled"}

    from django.core.cache import cache

    token = uuid.uuid4().hex
    if not cache.add(_REFRESH_LOCK, token, _REFRESH_LOCK_TTL):
        return {"status": "already_running"}
    try:
        from apps.admin_audit.health import record_sync
        from apps.campaigns.services import run_metric_refresh

        result = run_metric_refresh()
        record_sync("campaigns_refresh_metrics", campaigns=result.get("campaigns", 0),
                    refreshed=result.get("refreshed", 0))
        return result
    finally:
        _release_lock(_REFRESH_LOCK, token)


@shared_task(name="campaigns.sweep_deadlines")
def sweep_deadlines():
    """Due-soon / overdue / stale-manual notification sweep over ACTIVE campaigns (doc 08 §2.2)."""
    from core.features import feature_enabled

    if not feature_enabled("campaigns"):
        return {"status": "feature_disabled"}

    from django.core.cache import cache

    token = uuid.uuid4().hex
    if not cache.add(_SWEEP_LOCK, token, _SWEEP_LOCK_TTL):
        return {"status": "already_running"}
    try:
        from apps.admin_audit.health import record_sync
        from apps.campaigns.services import run_deadline_sweep

        result = run_deadline_sweep()
        record_sync("campaigns_sweep_deadlines", due_soon=result.get("due_soon", 0),
                    overdue=result.get("overdue", 0))
        return result
    finally:
        _release_lock(_SWEEP_LOCK, token)


@shared_task(name="campaigns.housekeeping")
def housekeeping():
    """Retention pruning of ObjectiveSample + archived-campaign CampaignActivity (doc 08 §2.3)."""
    from core.features import feature_enabled

    if not feature_enabled("campaigns"):
        return {"status": "feature_disabled"}

    from django.core.cache import cache

    token = uuid.uuid4().hex
    if not cache.add(_HOUSEKEEPING_LOCK, token, _HOUSEKEEPING_LOCK_TTL):
        return {"status": "already_running"}
    try:
        from apps.admin_audit.health import record_sync
        from apps.campaigns.services import run_housekeeping

        result = run_housekeeping()
        record_sync("campaigns_housekeeping", samples_pruned=result.get("samples_pruned", 0),
                    activity_pruned=result.get("activity_pruned", 0))
        return result
    finally:
        _release_lock(_HOUSEKEEPING_LOCK, token)
