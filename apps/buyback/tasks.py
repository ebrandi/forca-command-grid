"""Celery tasks for the buyback service."""
from __future__ import annotations

from celery import shared_task


@shared_task(name="buyback.reconcile_guaranteed")
def reconcile_guaranteed() -> dict:
    """4.20: match approved guaranteed buyouts to a corp-wallet donation carrying the payout
    token → mark settled (read-only). No-op unless the feature is armed for ESI reconcile."""
    from .guaranteed import reconcile_settlements

    return reconcile_settlements()
