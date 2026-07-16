"""The one authoritative doctrine-fit availability service (SHIP-1).

Every surface that shows or checks whether a doctrine ship can be had — the
Shipyard cards, order validation, the officer inventory console, background
allocation — derives its answer here. Views and templates never recompute
availability themselves.

Definitions (see the admin handbook for the officer-facing version):

* on_hand     — complete, deliverable ships recorded at the effective location
                whose stocked manifest matches the CURRENT fit revision.
* reserved    — active reservations held by open orders against that stock.
* atp         — available to promise = on_hand − reserved. Never negative.
* stale       — stocked ships whose manifest hash no longer matches the fit
                (the fit was edited since they were assembled); they stop
                counting until an officer revalidates them.
* incoming    — quantity on a live supply need that has a linked production
                vehicle (Industry Project or ERP build job). Shown separately;
                NEVER added to atp.

The read side here takes no locks — it is for display and batching. The write
side (:mod:`apps.store.inventory`) re-derives everything under
``select_for_update`` before creating reservations, so a stale card can never
oversell.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Sum
from django.utils import timezone

from .models import (
    FitOffer,
    FitReservation,
    FitStock,
    FitSupplyNeed,
    OfferState,
    ShipyardPolicy,
)


def manifest_hash(fit) -> str:
    """Stable digest of the deliverable bundle: the hull plus the exact fitted
    modules (slot placement included — a differently-fitted hull is a different
    deliverable). Quantities aggregate by (type, slot) so cosmetic reordering of
    the stored JSON does not change the hash."""
    agg: dict[tuple[int, str], int] = {}
    for module in fit.modules or []:
        tid = module.get("type_id")
        if not tid:
            continue
        key = (int(tid), str(module.get("slot") or ""))
        agg[key] = agg.get(key, 0) + int(module.get("quantity", 1) or 1)
    payload = {
        "ship": fit.ship_type_id,
        "modules": sorted([tid, slot, qty] for (tid, slot), qty in agg.items()),
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


@dataclass
class Availability:
    """Everything a card, order form or console row needs about one fit offer."""

    fit_id: int
    state: str                       # OfferState value
    is_offered: bool
    on_hand: int
    reserved: int
    atp: int
    stale_on_hand: int
    incoming: int
    backorders_allowed: bool
    lead_days: int
    eta: datetime | None             # estimated (never guaranteed) backorder delivery
    eta_source: str                  # "lead_time" | "production"
    location: object | None          # MarketLocation, or None when unconfigured
    max_per_order: int
    max_backorder: int | None        # None = no per-order backorder cap
    buyer_notes: str
    offer: FitOffer | None
    manifest_hash: str

    @property
    def can_order(self) -> bool:
        return self.state in (OfferState.READY, OfferState.LIMITED, OfferState.BACKORDER)

    @property
    def max_orderable(self) -> int:
        """Largest quantity one order may request right now (0 = ordering closed)."""
        if not self.can_order:
            return 0
        ceiling = self.max_per_order
        if not self.backorders_allowed:
            ceiling = min(ceiling, self.atp)
        elif self.max_backorder is not None:
            ceiling = min(ceiling, self.atp + self.max_backorder)
        return max(ceiling, 0)


def effective_location(offer: FitOffer | None, policy: ShipyardPolicy):
    """The fit's fulfilment location: per-fit override, else the corp default.

    ``None`` (nothing configured yet) means stock at ANY location counts and
    reservations are taken FIFO across locations — deterministic, and honest for
    a corp that has not set up multi-location stocking."""
    if offer is not None and offer.delivery_location_id:
        return offer.delivery_location
    return policy.default_location


def derive_state(*, is_offered: bool, atp: int, backorders_allowed: bool,
                 limited_threshold: int) -> str:
    """The single place the customer-facing state is decided."""
    if not is_offered:
        return OfferState.NOT_OFFERED
    if atp > limited_threshold:
        return OfferState.READY
    if atp > 0:
        return OfferState.LIMITED
    if backorders_allowed:
        return OfferState.BACKORDER
    return OfferState.UNAVAILABLE


def availability_for_fit(fit, *, policy: ShipyardPolicy | None = None) -> Availability:
    return availability_for_fits([fit], policy=policy)[fit.id]


def availability_for_fits(fits, *, policy: ShipyardPolicy | None = None) -> dict[int, Availability]:
    """Batched availability for many fits in a constant number of queries.

    Query plan (independent of len(fits)): offers ×1, stock rows ×1, active
    reservation sums ×1, live supply needs ×1 (+ the policy singleton). The
    Shipyard calls this once per page render — never per card."""
    fits = list(fits)
    policy = policy or ShipyardPolicy.active()
    fit_ids = [f.id for f in fits]

    offers = {
        o.fit_id: o
        for o in FitOffer.objects.filter(fit_id__in=fit_ids).select_related("delivery_location")
    }
    stocks_by_fit: dict[int, list[FitStock]] = {}
    for s in FitStock.objects.filter(doctrine_fit_id__in=fit_ids).select_related("location"):
        stocks_by_fit.setdefault(s.doctrine_fit_id, []).append(s)
    reserved_by_stock = {
        row["stock_id"]: row["s"]
        for row in (
            FitReservation.objects.filter(
                stock__doctrine_fit_id__in=fit_ids, status=FitReservation.Status.ACTIVE
            ).values("stock_id").annotate(s=Sum("quantity"))
        )
    }
    needs_by_fit: dict[int, list[FitSupplyNeed]] = {}
    for n in (
        FitSupplyNeed.objects.filter(
            doctrine_fit_id__in=fit_ids,
            status__in=(FitSupplyNeed.Status.OPEN, FitSupplyNeed.Status.IN_PROGRESS),
        ).select_related("industry_project", "build_job")
    ):
        needs_by_fit.setdefault(n.doctrine_fit_id, []).append(n)

    now = timezone.now()
    out: dict[int, Availability] = {}
    for fit in fits:
        offer = offers.get(fit.id)
        current = manifest_hash(fit)
        location = effective_location(offer, policy)

        rows = stocks_by_fit.get(fit.id, [])
        if location is not None:
            rows = [r for r in rows if r.location_id == location.pk]
        on_hand = sum(r.quantity_on_hand for r in rows if r.manifest_hash == current)
        stale = sum(r.quantity_on_hand for r in rows if r.manifest_hash != current)
        reserved = sum(
            reserved_by_stock.get(r.pk, 0) for r in rows if r.manifest_hash == current
        )
        atp = max(on_hand - reserved, 0)

        is_offered = offer.is_offered if offer is not None else True
        backorders_allowed = (
            offer.backorders_allowed
            if offer is not None and offer.backorders_allowed is not None
            else policy.backorders_enabled
        )
        lead_days = (
            offer.lead_days
            if offer is not None and offer.lead_days is not None
            else policy.default_lead_days
        )

        # Incoming supply: only needs with a real production vehicle count, and it
        # informs the estimate — it is never available-to-promise.
        incoming = 0
        eta = None
        eta_source = "lead_time"
        for need in needs_by_fit.get(fit.id, []):
            if need.industry_project_id or need.build_job_id:
                incoming += max(int(need.quantity_required), 0)
                vehicle_due = (
                    getattr(need.build_job, "due_at", None)
                    or getattr(need.industry_project, "due_at", None)
                )
                if vehicle_due and vehicle_due > now and (eta is None or vehicle_due > eta):
                    eta = vehicle_due
                    eta_source = "production"

        state = derive_state(
            is_offered=is_offered, atp=atp, backorders_allowed=backorders_allowed,
            limited_threshold=policy.limited_stock_threshold,
        )
        if state == OfferState.BACKORDER and eta is None:
            eta = now + timedelta(days=lead_days)

        out[fit.id] = Availability(
            fit_id=fit.id,
            state=state,
            is_offered=is_offered,
            on_hand=on_hand,
            reserved=reserved,
            atp=atp,
            stale_on_hand=stale,
            incoming=incoming,
            backorders_allowed=backorders_allowed,
            lead_days=lead_days,
            eta=eta if state == OfferState.BACKORDER else None,
            eta_source=eta_source,
            location=location,
            max_per_order=(
                offer.max_per_order
                if offer is not None and offer.max_per_order is not None
                else policy.max_order_quantity
            ),
            max_backorder=(
                offer.max_backorder_quantity if offer is not None else None
            ),
            buyer_notes=offer.buyer_notes if offer is not None else "",
            offer=offer,
            manifest_hash=current,
        )
    return out
