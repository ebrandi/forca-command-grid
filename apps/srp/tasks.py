"""Celery tasks for the Ship Replacement Program."""
from __future__ import annotations

from celery import shared_task


@shared_task(name="srp.scan_sla")
def scan_sla() -> dict:
    """SRP-2 (2.7): fire one deduped SRP-officer digest when the SRP queue breaches its
    configured SLA/solvency bounds. Deduped + no-op when the event is disabled."""
    from .alerts import scan_srp_health

    return scan_srp_health()


@shared_task(name="srp.auto_draft_claims")
def auto_draft_claims() -> dict:
    """SRP-4 (4.6): auto-draft SUBMITTED claims for eligible attended-op losses. Never
    auto-pays; no-op unless the programme has auto-draft armed."""
    from .auto_draft import auto_draft_claims as _run

    return _run()
