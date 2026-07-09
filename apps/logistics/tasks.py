"""Celery tasks for the freight service."""
from __future__ import annotations

from celery import shared_task

from . import contracts_esi


@shared_task(name="logistics.reconcile_courier_contracts")
def reconcile_courier_contracts() -> dict:
    """Verify self-reported courier deliveries against the in-game contracts."""
    return contracts_esi.reconcile_courier_contracts()


@shared_task(name="logistics.sync_corp_contracts")
def sync_corp_contracts() -> dict:
    """Snapshot all corp contracts for the oversight board. No-op until granted."""
    from .corp_contracts import sync_corp_contracts as _sync

    return _sync()


@shared_task(name="logistics.sweep_hauls")
def sweep_hauls() -> dict:
    """LOG-1 (3.2): remind haulers before their deadline and auto-release overdue hauls."""
    from .services import sweep_overdue_hauls

    return sweep_overdue_hauls()
