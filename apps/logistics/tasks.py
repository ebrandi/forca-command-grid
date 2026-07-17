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


@shared_task(name="logistics.sweep_freight_batches")
def sweep_freight_batches() -> dict:
    """P6: flip batches ARRIVED from a verified courier contract or a completed member
    haul, and flag late ones — arrival visibility for member-flown batches and a late
    nudge, never an auto-receipt (stock posts only on the deliberate human receipt).

    SHIPS INERT: gated on ``FreightConfig.eta_sweep_enabled`` (default False → one
    config read per firing, the ``industry.run_mrp`` precedent). Every alert is
    event-gated + idempotency-keyed, so re-runs never double-notify.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.stockpile.models import HaulingTask

    from . import freight
    from .models import CourierContract, FreightBatch, FreightConfig

    cfg = FreightConfig.active()
    if not cfg.eta_sweep_enabled:
        return {"arrived": 0, "late": 0}

    now = timezone.now()
    active = (FreightBatch.Status.ASSIGNED, FreightBatch.Status.IN_TRANSIT)
    arrived = late = 0

    # (a) A linked courier contract verified in-game → the goods are delivered.
    for batch in (
        FreightBatch.objects.filter(status__in=active, courier_contract__isnull=False)
        .select_related("courier_contract")
    ):
        if batch.courier_contract.verification_state == CourierContract.Verification.VERIFIED:
            try:
                freight.mark_arrived(batch, from_sweep=True)
                arrived += 1
            except freight.FreightError:
                pass

    # (c) A batch-linked member haul went DONE (haul_transition flips status only —
    # nothing else tells the batch) → arrival visibility, never an auto-receipt.
    for batch in (
        FreightBatch.objects.filter(status__in=active, hauling_task__isnull=False)
        .select_related("hauling_task")
    ):
        if batch.hauling_task.status == HaulingTask.Status.DONE:
            try:
                freight.mark_arrived(batch, from_sweep=True)
                arrived += 1
            except freight.FreightError:
                pass

    # (b) In-transit batches past their ETA + grace, not yet flagged → flag once.
    grace = timedelta(hours=int(cfg.late_grace_hours))
    for batch in FreightBatch.objects.filter(
        status=FreightBatch.Status.IN_TRANSIT, late_flagged_at__isnull=True,
        eta_planned__isnull=False, eta_planned__lt=now - grace,
    ):
        stamped = FreightBatch.objects.filter(
            pk=batch.pk, late_flagged_at__isnull=True
        ).update(late_flagged_at=now)
        if not stamped:
            continue
        freight._emit_batch_alert(
            batch, template="logistics.batch_late", event_key="logistics.batch_late",
            idempotency_key=f"freight:late:{batch.pk}:{int(batch.eta_planned.timestamp())}",
            title="Freight batch overdue",
            body=(f"Freight batch {batch.label} is past its ETA "
                  f"({batch.eta_planned.date().isoformat()}) and has not arrived."),
            context=freight._alert_context(batch),
        )
        late += 1

    return {"arrived": arrived, "late": late}
