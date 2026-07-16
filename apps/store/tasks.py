"""Celery tasks for Shipyard availability control (SHIP-1)."""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

log = logging.getLogger("forca.store")


@shared_task(name="store.expire_reservations")
def expire_reservations() -> int:
    """Release reservations of doctrine-fit orders nobody claimed in time.

    Governed by ``ShipyardPolicy.reservation_expiry_days`` (0 = feature off, the
    shipped default). Only orders still OPEN are touched — once a member claims
    an order the hold is honoured indefinitely. Per policy, the released
    quantity becomes backorder demand (when backorders are allowed for the fit)
    or the order is cancelled with a notification. Idempotent: reservations flip
    through a status-guarded UPDATE and an already-processed order is skipped.
    """
    from .inventory import release_order_reservations
    from .models import FitReservation, ShipyardPolicy, StoreOrder
    from .services import (
        _effective_offer_terms,
        notify_reservation_expired,
        transition_order,
    )
    from .supply import recompute_supply_need

    policy = ShipyardPolicy.active()
    days = policy.reservation_expiry_days
    if not days:
        return 0

    cutoff = timezone.now() - timedelta(days=days)
    stale_ids = list(
        StoreOrder.objects.filter(
            kind=StoreOrder.Kind.DOCTRINE_FIT,
            status=StoreOrder.Status.OPEN,
            created_at__lt=cutoff,
            fit_reservations__status=FitReservation.Status.ACTIVE,
        ).distinct().values_list("pk", flat=True)
    )
    processed = 0
    for pk in stale_ids:
        with transaction.atomic():
            order = StoreOrder.objects.select_for_update().get(pk=pk)
            if order.status != StoreOrder.Status.OPEN:
                continue
            released = release_order_reservations(order, expired=True)
            if not released:
                continue
            fit = order.doctrine_fit
            allowed = True
            if fit is not None:
                _offer, _is_offered, allowed, *_rest = _effective_offer_terms(fit, policy)
            if allowed:
                # The released units become backorder demand; the frozen order-time
                # snapshot (quantity_reserved etc.) is deliberately NOT rewritten.
                if fit is not None:
                    recompute_supply_need(fit, location=order.delivery_location)
                notify_reservation_expired(order)
            else:
                transition_order(order, StoreOrder.Status.CANCELLED, actor=None)
            processed += 1
    if processed:
        log.info("reservation expiry: %s order(s) processed", processed)
    return processed


@shared_task(name="store.snapshot_demand")
def snapshot_demand() -> int:
    """Weekly composed-demand snapshot per fit (P2) + 26-week retention prune.

    Ships ARMED — a deliberate, stated deviation from the inert-by-default beat
    convention: this is pure internal data collection (one weekly write batch, no
    member-visible effect, no notifications), and demand *history* only exists if
    collection starts before anyone wants it. Everything officer-visible that P2
    adds (the suggested-reorder alert) follows the convention and ships disarmed.
    Idempotent per (fit, week): re-runs upsert the same rows.
    """
    from .availability import availability_for_fits
    from .demand import demand_for_fits, planning_universe
    from .models import DemandSnapshot, ShipyardPolicy

    fits = planning_universe()
    if not fits:
        return 0
    availability = availability_for_fits(fits, policy=ShipyardPolicy.active())
    demand = demand_for_fits(fits, availability=availability)

    today = timezone.localdate()
    week_start = today - timedelta(days=today.weekday())
    written = 0
    for fit in fits:
        d = demand[fit.id]
        DemandSnapshot.objects.update_or_create(
            fit=fit, week_start=week_start,
            defaults={
                "rate_week_mean": d.rate_week_mean,
                "rate_week_hi": d.rate_week_hi,
                "sigma_week": d.sigma_week,
                "sources": {
                    s.key: {"rate_week": str(s.rate_week), "units": str(s.units)}
                    for s in d.sources
                },
            },
        )
        written += 1
    pruned, _detail = DemandSnapshot.objects.filter(
        week_start__lt=week_start - timedelta(weeks=26)
    ).delete()
    log.info("demand snapshot: %s fit(s) written, %s old row(s) pruned", written, pruned)
    return written
