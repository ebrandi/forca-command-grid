"""Celery tasks for comms-access sync.

Thin wrappers with lazy imports (the ``apps.pingboard.tasks`` idiom). Both tasks are cheap
no-ops until the feature is enabled *and* at least one platform is armed, so the beat entry
costs nothing while the subsystem ships inert.
"""
from __future__ import annotations

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

# Non-overlap lock (the raffle.draw_due idiom): a slow sweep (many accounts × external
# HTTP round-trips) must never overlap the next beat tick and stampede the provider API.
# TTL tracks the hard time limit so a dead run's lock auto-expires.
_SWEEP_LOCK_KEY = "commsaccess:reconcile_all:lock"
_SWEEP_LOCK_TTL = 660


@shared_task(name="commsaccess.reconcile_all", soft_time_limit=600, time_limit=660)
def reconcile_all():
    """Periodic full reconcile. Catches grant expiries (no push signal fires at expiry).

    Bounded by soft/hard time limits and a cache lock so it can't run away or overlap.
    """
    from . import config

    if not config.feature_active():
        return {"skipped": "feature disabled"}

    from django.core.cache import cache

    if not cache.add(_SWEEP_LOCK_KEY, "1", _SWEEP_LOCK_TTL):
        return {"skipped": "already running"}
    try:
        from collections import defaultdict

        from .models import EntitlementMapping
        from .reconcile import iter_reconcilable_accounts, reconcile_account

        # Resolve the managed mappings ONCE per platform (not once per account).
        maps: dict[str, list] = defaultdict(list)
        for m in EntitlementMapping.objects.filter(enabled=True):
            maps[m.platform].append(m)

        changed = failed = seen = 0
        for account in iter_reconcilable_accounts():
            seen += 1
            try:
                res = reconcile_account(account, mappings=maps.get(account.platform, []))
            except SoftTimeLimitExceeded:
                # Let the time limit end the sweep cleanly; the next tick resumes it.
                raise
            except Exception:  # noqa: BLE001 - one bad account must not stop the sweep
                failed += 1
                continue
            if res.changed:
                changed += 1
            if res.failed:
                failed += 1
        return {"seen": seen, "changed": changed, "failed": failed}
    finally:
        cache.delete(_SWEEP_LOCK_KEY)


@shared_task(name="commsaccess.reconcile_user", soft_time_limit=120, time_limit=150)
def reconcile_user_task(user_id: int, source_ref: str = ""):
    """Targeted reconcile for one user — the fast revoke path wired to access-change events."""
    from . import config

    if not config.feature_active():
        return {"skipped": "feature disabled"}
    from django.contrib.auth import get_user_model

    from .reconcile import reconcile_user

    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        return {"skipped": "no user"}
    results = reconcile_user(user, source_ref=source_ref)
    return {"platforms": {p: (not r.skipped) for p, r in results.items()}}
