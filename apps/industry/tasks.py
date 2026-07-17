"""Celery tasks for the Industry Center (P3: the MRP planning beat)."""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

log = logging.getLogger("forca.industry")


@shared_task(name="industry.run_mrp")
def run_mrp_beat() -> int:
    """Nightly planning run — SHIPS INERT (``MrpConfig.auto_run_enabled`` default
    False; one config read per firing, the ``store.expire_reservations``
    precedent). Only the ARMED beat pings officers about urgent shortfalls;
    manual runs never do (the officer is already looking at the page). The beat
    never mints vehicles — fan-out is an officer click, always.
    """
    from . import mrp
    from .models import MrpConfig, NetRequirement

    config = MrpConfig.active()
    if not config.auto_run_enabled:
        return 0
    try:
        run = mrp.run_mrp(actor=None)
    except mrp.MrpAlreadyRunning:
        log.info("mrp beat: a run is already in progress — skipped")
        return 0

    # Ping only NEW urgency: an open requirement whose required-by falls inside
    # its own lead time. Idempotent per (requirement, required-by day) — the
    # pingboard idempotency_key discipline, never trust fire-once.
    now = timezone.now()
    pinged = 0
    lead_by_suggestion = {
        "buy": int(config.buy_lead_days),
        "import": int(config.import_lead_days),
        "build": int(config.import_lead_days),  # honest default without capacity data
    }
    urgent = NetRequirement.objects.filter(
        status=NetRequirement.Status.OPEN, net_quantity__gt=0,
        required_by__isnull=False,
    ).select_related("location")
    for req in urgent:
        lead = lead_by_suggestion.get(req.suggestion, int(config.import_lead_days))
        if req.required_by > now + timedelta(days=lead):
            continue
        pinged += _ping_shortfall(req)
    log.info("mrp beat: run %s finished, %s shortfall ping(s)", run.pk, pinged)
    return pinged


def _ping_shortfall(req) -> int:
    """Officer-routed shortfall alert (best-effort, never breaks the beat)."""
    try:
        from apps.pingboard import services as pingboard
        from apps.pingboard.models import AlertCategory
        from apps.sde.models import SdeType

        name = SdeType.objects.filter(type_id=req.type_id).values_list(
            "name", flat=True).first() or str(req.type_id)
        day = req.required_by.date().isoformat()
        pingboard.emit_broadcast(
            category=AlertCategory.INDUSTRY_JOB,
            title="Material shortfall",
            body=(
                f"The material plan needs {req.net_quantity}x {name} by {day}. "
                "Review the Material Plan: /industry/mrp/"
            ),
            template="industry.mrp_shortfall",
            context={
                "industry_job_name": name,
                "quantity": req.net_quantity,
                "eta_date": day,
                "link": "/industry/mrp/",
            },
            source_service="industry",
            source_object_id=f"net_requirement:{req.pk}",
            idempotency_key=f"industry:mrp_shortfall:{req.pk}:{day}",
        )
        return 1
    except Exception:  # noqa: BLE001 — notification must never break the beat
        log.exception("mrp shortfall ping failed (requirement %s)", req.pk)
        return 0
