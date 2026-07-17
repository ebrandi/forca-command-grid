"""Celery tasks for procurement (P4).

All four ship INERT behind ``ProcurementConfig`` flags: the beat runs on schedule
but each task is a single config read until its flag arms. Each stamps sync
freshness only on an armed ("ok") run so a disarmed feature never fakes a green
chip on the board.
"""
from __future__ import annotations

from celery import shared_task


def _stamp(key: str, result: dict) -> None:
    if result.get("status") != "ok":
        return
    from apps.admin_audit.health import record_sync

    record_sync(key, **{k: v for k, v in result.items() if k != "status"})


@shared_task(name="procurement.match_contracts")
def match_contracts() -> dict:
    """Match approved POs to the corp-contracts snapshot. No-op until armed."""
    from .contracts import match_contracts as _run

    result = _run()
    _stamp("procurement_match", result)
    return result


@shared_task(name="procurement.reconcile_payments")
def reconcile_payments() -> dict:
    """Settle contract-linked POs on an exact wallet context_id match. No-op until armed."""
    from .payments import reconcile_payments as _run

    result = _run()
    _stamp("procurement_reconcile", result)
    return result


@shared_task(name="procurement.sweep_overdue")
def sweep_overdue() -> dict:
    """Flag past-promise POs overdue and expire lapsed agreements. No-op until armed."""
    from .payments import sweep_overdue as _run

    result = _run()
    _stamp("procurement_sweep", result)
    return result


@shared_task(name="procurement.rollup_reliability")
def rollup_reliability() -> dict:
    """Recompute supplier reliability over the last N complete weeks. No-op until armed."""
    from .metrics import rollup_reliability as _run

    result = _run()
    _stamp("procurement_reliability", result)
    return result
