"""Background sync of our alliance's sovereignty structures (public ESI)."""
from __future__ import annotations

import logging

from celery import shared_task

log = logging.getLogger("forca.operations")


@shared_task(name="operations.sync_sovereignty")
def sync_sovereignty() -> str:
    """Refresh our alliance's sov structures (ADM). No-op unless we hold sov."""
    from .sov_esi import sync_sovereignty as _sync

    result = _sync()
    log.info("sov sync: %s — %s structures", result["status"], result.get("count", 0))
    return result["status"]


@shared_task(name="operations.formup_reminders")
def formup_reminders() -> int:
    """Send one T-minus form-up reminder to each YES-committed pilot before their op.

    Frequent cadence; only fires inside the lead window and once per commitment."""
    from .services import send_formup_reminders

    sent = send_formup_reminders()
    if sent:
        log.info("sent %s form-up reminder(s)", sent)
    return sent


@shared_task(name="operations.auto_cancel_expired")
def auto_cancel_expired() -> int:
    """Cancel scheduled ops whose sign-up deadline passed without enough pilots.

    Records a structured cancellation snapshot for each. No-op when nothing is due.
    """
    from .services import auto_cancel_due

    cancelled = auto_cancel_due()
    if cancelled:
        log.info("auto-cancelled %s operation(s): %s", len(cancelled), cancelled)
    return len(cancelled)


@shared_task(name="operations.materialize_recurring_ops")
def materialize_recurring_ops() -> int:
    """Spawn upcoming op instances from active recurring templates (OPS-4 / 3.12). Idempotent."""
    from .services import materialize_recurring_ops as _run

    result = _run()
    if result["created"]:
        log.info("materialised %s recurring operation instance(s)", result["created"])
    return result["created"]
