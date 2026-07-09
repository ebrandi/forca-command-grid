"""Background sync of the corp roster (member tracking) via a Director token."""
from __future__ import annotations

import logging

from celery import shared_task

log = logging.getLogger("forca.corporation")


@shared_task(name="corporation.scan_infrastructure_alerts")
def scan_infrastructure_alerts() -> dict:
    """CORP-3 (2.3): fire a deduped officer digest when a structure crosses the low-fuel
    threshold or a sov system drops below the ADM floor. Deduped + no-op when disabled."""
    from .alerts import scan_infrastructure_alerts as _scan

    return _scan()


@shared_task(name="corporation.sync_members")
def sync_corp_members() -> str:
    """Refresh the corp roster. Member-tracking is cached ~1h server-side."""
    from .roster import import_corp_members

    result = import_corp_members()
    log.info("corp roster sync: %s — %s", result["status"], result.get("message", ""))
    return result["status"]


@shared_task(name="corporation.sync_wallets")
def sync_corp_wallets() -> str:
    """Refresh corp wallet balances + recent journal (Accountant/Director token)."""
    from .finance import sync_corp_wallets as _sync

    result = _sync()
    log.info("corp wallet sync: %s — %s entries", result["status"], result.get("entries", 0))
    return result["status"]


@shared_task(name="corporation.sync_contacts")
def sync_corp_contacts() -> str:
    """Refresh corp standings/contacts (role-holder token)."""
    from .contacts import sync_corp_contacts as _sync

    result = _sync()
    log.info("corp contacts sync: %s — %s contacts", result["status"], result.get("count", 0))
    return result["status"]


@shared_task(name="corporation.sync_extractions")
def sync_moon_extractions() -> str:
    """Refresh scheduled moon extractions (Station-Manager/Director token)."""
    from .extractions import sync_moon_extractions as _sync

    result = _sync()
    log.info("moon extractions sync: %s — %s", result["status"], result.get("count", 0))
    return result["status"]


@shared_task(name="corporation.sweep_chunk_reminders")
def sweep_chunk_reminders() -> int:
    """Fire opt-in chunk-arrival reminders ahead of each upcoming fracture (MIN-3 / 3.13)."""
    from .extractions import sweep_chunk_reminders as _sweep

    fired = _sweep()
    if fired:
        log.info("fired %s chunk-arrival reminder(s)", fired)
    return fired


@shared_task(name="corporation.warm_finance")
def warm_finance_dashboard() -> str:
    """Keep the default Corp Finance dashboard warm in cache."""
    from .finance_analytics import default_dashboard

    data = default_dashboard(refresh=True)
    return f"net={int(data['net_total'])}"


@shared_task(name="corporation.sync_structures")
def sync_corp_structures() -> str:
    """Refresh corp structures — fuel/state/timers (Station-Manager/Director token)."""
    from .structures_esi import sync_corp_structures as _sync

    result = _sync()
    log.info("corp structures sync: %s — %s structures", result["status"], result.get("count", 0))
    return result["status"]
