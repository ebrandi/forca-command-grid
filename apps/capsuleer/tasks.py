"""Capsuleer Path Celery tasks (doc 12).

Thin ``@shared_task`` wrappers; all logic lives in ``apps.capsuleer.services`` so it is unit-testable
without a broker. Every task shares the campaigns prologue (doc 12 §2): the **feature check first**
(a disabled subsystem costs one cache read per tick), the config kill-switch second where one
applies, the ``cache.add`` cross-worker lock third (its TTL a hard upper bound on runtime, strictly
below the beat cadence, so a crashed worker cannot wedge the schedule), the lazy service import
last. There is deliberately no retry API — every task is a convergent sweep (``acks_late`` +
idempotency); the ``finally`` release is a token-guarded compare-and-delete so an overrun run never
deletes its successor's lock.

Three beats ship, all scheduled in ``config/celery.py``: the hourly evidence reconcile, the nightly
housekeeping, and the daily ``capsuleer.generate_suggestions`` generation sweep.
"""
from __future__ import annotations

import uuid

from celery import shared_task

_RECONCILE_LOCK = "capsuleer:lock:reconcile_progress"
_RECONCILE_LOCK_TTL = 1800  # 30 min — bounds the hourly evidence sweep
_SUGGEST_LOCK = "capsuleer:lock:generate_suggestions"
_SUGGEST_LOCK_TTL = 3600  # 60 min — bounds the daily generation sweep
_HOUSEKEEPING_LOCK = "capsuleer:lock:housekeeping"
_HOUSEKEEPING_LOCK_TTL = 1800  # 30 min


def _release_lock(key: str, token: str) -> None:
    """Release a beat lock only if we still own it — a compare-and-delete so a run that overran its
    TTL (whose lock a successor has re-acquired) never deletes the successor's lock and lets two
    sweeps run concurrently (the campaigns ``_release_lock`` idiom)."""
    from django.core.cache import cache

    if cache.get(key) == token:
        cache.delete(key)


@shared_task(name="capsuleer.reconcile_progress")
def reconcile_progress():
    """Credit pending auto-verified milestones from non-skill evidence stores (doc 12 §4.1)."""
    from core.features import feature_enabled

    if not feature_enabled("capsuleer"):
        return {"status": "feature_disabled"}

    from apps.capsuleer import config

    if not config.get("reconcile")["enabled"]:
        return {"status": "disabled"}

    from django.core.cache import cache

    token = uuid.uuid4().hex
    if not cache.add(_RECONCILE_LOCK, token, _RECONCILE_LOCK_TTL):
        return {"status": "already_running"}
    try:
        from apps.admin_audit.health import record_sync
        from apps.capsuleer.services import run_reconcile_sweep

        result = run_reconcile_sweep()
        record_sync("capsuleer_reconcile", users=result.get("users", 0),
                    credited=result.get("credited", 0), unknown=result.get("unknown", 0))
        return result
    finally:
        _release_lock(_RECONCILE_LOCK, token)


@shared_task(name="capsuleer.generate_suggestions")
def generate_suggestions():
    """Daily pilot-scoped suggestion generation (doc 08 §3). Gated on the feature flag and the
    ``capsuleer.suggestions.enabled`` config kill-switch so leadership can stop it without disabling
    the feature."""
    from core.features import feature_enabled

    if not feature_enabled("capsuleer"):
        return {"status": "feature_disabled"}

    from apps.capsuleer import config

    if not config.get("suggestions")["enabled"]:
        return {"status": "disabled"}

    from django.core.cache import cache

    token = uuid.uuid4().hex
    if not cache.add(_SUGGEST_LOCK, token, _SUGGEST_LOCK_TTL):
        return {"status": "already_running"}
    try:
        from apps.admin_audit.health import record_sync
        from apps.capsuleer.suggest import run_generation

        result = run_generation()
        record_sync("capsuleer_suggestions", users=result.get("users", 0),
                    admitted=result.get("admitted", 0), capped=result.get("capped", 0),
                    expired=result.get("expired", 0))
        return result
    finally:
        _release_lock(_SUGGEST_LOCK, token)


@shared_task(name="capsuleer.housekeeping")
def housekeeping():
    """Retention pruning + stalled/review-due flagging (doc 12 §4.3). Gated on the feature flag
    only — retention has no separate kill switch (disabling the feature freezes data in place)."""
    from core.features import feature_enabled

    if not feature_enabled("capsuleer"):
        return {"status": "feature_disabled"}

    from django.core.cache import cache

    token = uuid.uuid4().hex
    if not cache.add(_HOUSEKEEPING_LOCK, token, _HOUSEKEEPING_LOCK_TTL):
        return {"status": "already_running"}
    try:
        from apps.admin_audit.health import record_sync
        from apps.capsuleer.services import run_housekeeping

        result = run_housekeeping()
        record_sync("capsuleer_housekeeping",
                    snapshots_pruned=result.get("snapshots_pruned", 0),
                    suggestions_pruned=result.get("suggestions_pruned", 0),
                    activity_pruned=result.get("activity_pruned", 0),
                    reviews_flagged=result.get("reviews_flagged", 0))
        return result
    finally:
        _release_lock(_HOUSEKEEPING_LOCK, token)
