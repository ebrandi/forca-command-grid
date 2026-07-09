"""Celery tasks — thin wrappers with lazy imports (the house convention).

``name=`` matches the beat entries in ``config/celery.py``. Tasks are idempotent:
ACKS_LATE re-delivers on a worker crash, and the dispatcher's deliver-once ledger
plus the SCHEDULED→QUEUED compare-and-set make a re-run safe.
"""
from __future__ import annotations

from celery import shared_task


@shared_task(name="pingboard.deliver_alert")
def deliver_alert(alert_id: int) -> dict:
    from .services import dispatch_alert

    return dispatch_alert(alert_id)


@shared_task(name="pingboard.dispatch_due")
def dispatch_due() -> dict:
    from .services import dispatch_due_alerts

    return dispatch_due_alerts()


@shared_task(name="pingboard.retry_failed")
def retry_failed() -> dict:
    from .services import retry_failed_deliveries

    return retry_failed_deliveries()


@shared_task(name="pingboard.sync_calendar")
def sync_calendar() -> dict:
    from .calendar import sync_calendar_sources

    return sync_calendar_sources()


@shared_task(name="pingboard.materialise_reminders")
def materialise_reminders() -> dict:
    from .calendar import materialise_due_reminders

    return materialise_due_reminders()


@shared_task(name="pingboard.evaluate_automation")
def evaluate_automation() -> dict:
    from .automation import evaluate_threshold_rules

    return evaluate_threshold_rules()


@shared_task(name="pingboard.housekeeping")
def housekeeping() -> dict:
    from .services import housekeeping as run

    return run()
