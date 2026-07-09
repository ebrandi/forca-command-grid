"""Background sync of the corp mining ledger."""
from __future__ import annotations

import logging

from celery import shared_task

log = logging.getLogger("forca.mining")


@shared_task(name="mining.sync_ledger")
def sync_mining_ledger() -> str:
    """Refresh the corp mining ledger (Station-Manager/Director mining scope)."""
    from .sync import sync_mining_ledger as _sync

    result = _sync()
    log.info("mining ledger sync: %s — %s entries", result["status"], result.get("entries", 0))
    return result["status"]


@shared_task(name="mining.scan_milestones")
def scan_mining_milestones() -> dict:
    """MIN-4 (3.10): award recognition for newly-crossed cumulative-m³ mining milestones."""
    from .services import scan_mining_milestones as _scan

    return _scan()
