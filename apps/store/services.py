"""Store services: config, access control, order creation, and the status flow."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import (
    Audience,
    FitOffer,
    OrderAvailability,
    ShipyardPolicy,
    StoreConfig,
    StoreOrder,
)
from .pricing import Priced

_AUDIENCE_CACHE_KEY = "store:audience"


def active_config() -> StoreConfig:
    cfg = StoreConfig.objects.filter(is_active=True).order_by("-updated_at").first()
    if cfg is None:
        cfg = StoreConfig.objects.create(name="Standard", is_active=True)
    return cfg


def current_audience() -> str:
    cached = cache.get(_AUDIENCE_CACHE_KEY)
    if cached is None:
        cached = (
            StoreConfig.objects.filter(is_active=True)
            .order_by("-updated_at")
            .values_list("audience", flat=True)
            .first()
            or StoreConfig._meta.get_field("audience").default
        )
        cache.set(_AUDIENCE_CACHE_KEY, cached, 300)
    return cached


def invalidate_audience_cache() -> None:
    cache.delete(_AUDIENCE_CACHE_KEY)


def can_access(user) -> bool:
    """Whether ``user`` may shop (browse + order) under the current audience."""
    audience = current_audience()
    if audience == Audience.PUBLIC:
        return True
    if audience == Audience.DISABLED:
        return False
    from core import rbac

    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or rbac.has_role(user, rbac.ROLE_MEMBER):
        return True
    if audience == Audience.ALLIANCE:
        from apps.corporation.access import is_service_alliance_pilot

        return is_service_alliance_pilot(user)
    return False


def _q(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def create_order(*, priced: Priced, kind: str, quantity: int, cfg: StoreConfig,
                 buyer, buyer_character_id=None, doctrine_fit=None, fit_name="",
                 location_name="", notes="", freeze: dict | None = None) -> StoreOrder:
    """Persist a priced order. Hulls require a build deposit; fits don't.

    ``freeze`` carries the order-time availability snapshot (reserved/backordered
    quantities, delivery location, manifest hash, promise dates) written by
    :func:`place_fit_order`; the hull path passes nothing and keeps its legacy
    blank/zero values."""
    quantity = max(int(quantity or 1), 1)
    requires_build = kind == StoreOrder.Kind.HULL
    total = _q(priced.unit_price * quantity)
    deposit_pct = cfg.deposit_pct if requires_build else Decimal("0.000")
    deposit = _q(total * deposit_pct) if requires_build else Decimal("0.00")
    order = StoreOrder.objects.create(
        buyer=buyer,
        buyer_character_id=buyer_character_id,
        kind=kind,
        doctrine_fit=doctrine_fit,
        fit_name=fit_name,
        ship_type_id=priced.ship_type_id,
        ship_name=priced.ship_name,
        hull_class=priced.hull_class,
        manifest=priced.manifest,
        quantity=quantity,
        unit_jita=priced.unit_jita,
        unit_price=priced.unit_price,
        total_price=total,
        markup_pct=priced.markup,
        price_basis=priced.price_basis,
        unit_cost=priced.unit_cost,
        deposit_pct=deposit_pct,
        deposit_amount=deposit,
        requires_build=requires_build,
        location_name=location_name,
        notes=notes,
        **(freeze or {}),
    )
    from apps.pingboard import hooks

    hooks.fire("store.new", source_object_id=order.id, dedup_suffix="new",
               context={"ship_name": priced.ship_name or ""})
    return order


# --- Shipyard availability-aware placement (SHIP-1) -------------------------- #


@dataclass
class Placement:
    """Outcome of a placement attempt. ``order`` is set only on success; otherwise
    ``needs_confirm`` carries the server-computed split for the confirm page, or
    ``error`` explains the rejection."""

    order: StoreOrder | None = None
    needs_confirm: bool = False
    error: str = ""
    # The authoritative split/terms at decision time (also what the confirm page shows).
    quantity: int = 0
    ready_quantity: int = 0
    backordered_quantity: int = 0
    atp: int = 0
    eta: object | None = None
    lead_days: int = 0
    location: object | None = None
    partial_allowed: bool = True
    backorders_allowed: bool = False
    max_per_order: int = 0


def _effective_offer_terms(fit, policy: ShipyardPolicy):
    """(offer, is_offered, backorders_allowed, lead_days, location, max_per_order,
    max_backorder) with per-fit overrides folded over the global policy."""
    from .availability import effective_location

    offer = (
        FitOffer.objects.select_related("delivery_location").filter(fit=fit).first()
    )
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
    location = effective_location(offer, policy)
    max_per_order = (
        offer.max_per_order
        if offer is not None and offer.max_per_order is not None
        else policy.max_order_quantity
    )
    max_backorder = offer.max_backorder_quantity if offer is not None else None
    return offer, is_offered, backorders_allowed, lead_days, location, max_per_order, max_backorder


@transaction.atomic
def place_fit_order(*, fit, quantity: int, buyer, buyer_character_id=None,
                    notes: str = "", acknowledged: bool = False,
                    force_backorder: bool = False) -> Placement:
    """Server-authoritative doctrine-fit order placement.

    Everything that matters is re-derived here — price, availability, split,
    location, lead time — under ``select_for_update`` on the fit's stock rows, so
    a stale card can never oversell and no client-posted value beyond
    (fit, quantity, notes, acknowledgement) is ever trusted. Two buyers racing
    for the last ship serialize on the row locks; the loser's split is
    recomputed and, when anything would be backordered without an explicit
    acknowledgement, placement pauses (``needs_confirm``) instead of promising
    stock that is no longer there.
    """
    from .availability import manifest_hash
    from .inventory import locked_atp, reserve_for_order
    from .pricing import price_doctrine_fit
    from .supply import recompute_supply_need

    cfg = active_config()
    policy = ShipyardPolicy.active()
    (offer, is_offered, backorders_allowed, lead_days, location,
     max_per_order, max_backorder) = _effective_offer_terms(fit, policy)

    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        quantity = 0
    result = Placement(
        quantity=quantity, lead_days=lead_days, location=location,
        partial_allowed=policy.allow_partial_fulfilment,
        backorders_allowed=backorders_allowed, max_per_order=max_per_order,
    )

    # Only fits of ACTIVE doctrines are for sale — the Shipyard lists only those,
    # and a hand-crafted POST with a draft/retired fit id must not bypass that.
    from apps.doctrines.models import Doctrine

    if fit.doctrine.status != Doctrine.Status.ACTIVE:
        result.error = _("That ship is not offered for sale right now.")
        return result
    if not is_offered:
        result.error = _("That ship is not offered for sale right now.")
        return result
    if quantity < 1:
        result.error = _("Pick a quantity of at least 1.")
        return result
    if quantity > max_per_order:
        result.error = _(
            "Orders for this ship are limited to %(max)s at a time."
        ) % {"max": max_per_order}
        return result

    # Authoritative split, derived under row locks.
    atp, rows, reserved_map = locked_atp(fit, location=location)
    take = 0 if force_backorder else min(quantity, atp)
    backordered = quantity - take
    result.atp = atp
    result.ready_quantity = take
    result.backordered_quantity = backordered

    if backordered > 0:
        if not backorders_allowed:
            if atp > 0:
                result.needs_confirm = True  # offer the remaining ready units instead
                result.error = _(
                    "Only %(count)s can be delivered right now and backorders are "
                    "closed for this ship."
                ) % {"count": atp}
            else:
                result.error = _(
                    "That ship is out of stock and backorders are closed for it."
                )
            return result
        if force_backorder and not acknowledged:
            result.needs_confirm = True
            return result
        if take > 0 and not policy.allow_partial_fulfilment and not force_backorder:
            # Mixing is disabled: the buyer chooses ready-only or all-backorder.
            result.needs_confirm = True
            return result
        if max_backorder is not None and backordered > max_backorder:
            result.error = _(
                "At most %(max)s of this ship can be backordered per order."
            ) % {"max": max_backorder}
            return result
        if not acknowledged:
            result.needs_confirm = True
            return result

    priced = price_doctrine_fit(fit, cfg.doctrine_markup)
    if not priced.ok:
        result.error = priced.error
        return result
    if priced.unit_price <= 0:
        # A cold/partial market snapshot prices missing types at 0 — refuse to
        # freeze a (near-)zero quote rather than sell a ship for nothing.
        result.error = _("No reliable price is available for that fit right now — try again later.")
        return result

    eta = timezone.now() + timedelta(days=lead_days) if backordered > 0 else None
    result.eta = eta
    if backordered == 0:
        state = OrderAvailability.READY
    elif take == 0:
        state = OrderAvailability.BACKORDER
    else:
        state = OrderAvailability.PARTIAL

    order = create_order(
        priced=priced, kind=StoreOrder.Kind.DOCTRINE_FIT, quantity=quantity, cfg=cfg,
        buyer=buyer, buyer_character_id=buyer_character_id,
        doctrine_fit=fit, fit_name=fit.name,
        location_name=str(location) if location is not None else "",
        notes=notes,
        freeze={
            "availability_state": state,
            "quantity_reserved": take,
            "quantity_backordered": backordered,
            "delivery_location": location,
            "manifest_hash": manifest_hash(fit),
            "lead_days_assumed": lead_days if backordered > 0 else None,
            "promised_date": eta,
            "current_eta": eta,
            "backorder_acknowledged": acknowledged and backordered > 0,
        },
    )
    if take > 0:
        reserved = reserve_for_order(
            order, fit, take, location=location, _rows=rows, _reserved=reserved_map
        )
        if reserved != take:  # cannot happen while the locks hold; hard-fail loudly
            raise RuntimeError(
                f"reservation shortfall on order {order.pk}: {reserved} != {take}"
            )
    if backordered > 0:
        # In-transaction pass gives immediate feedback off this order's own view;
        # the post-commit pass re-counts under the need's row lock so concurrent
        # placements converge on the true total (each sees at least every order
        # committed before it runs).
        recompute_supply_need(fit, location=location)
        transaction.on_commit(
            lambda: recompute_supply_need(fit, location=location)
        )
    result.order = order
    return result


def transition_order(order: StoreOrder, to_status: str, *, actor) -> bool:
    """Apply a status change plus its availability side effects, then notify.

    Compare-and-swap: the UPDATE only lands if the order still has the status
    the caller saw, so a cancel racing an advance can never resurrect a closed
    order — the loser gets ``False`` and re-reads. READY/DELIVERED stamp their
    actual dates; DELIVERED consumes the order's reservations (exactly once);
    CANCELLED releases them and refreshes the supply need. Always call this
    instead of poking ``order.status``."""
    from .inventory import consume_order_reservations, release_order_reservations
    from .supply import recompute_supply_need

    now = timezone.now()
    updates: dict = {"status": to_status, "updated_at": now}
    if to_status == StoreOrder.Status.READY:
        updates["actual_ready_at"] = order.actual_ready_at or now
    if to_status == StoreOrder.Status.DELIVERED:
        updates["delivered_at"] = order.delivered_at or now
    changed = StoreOrder.objects.filter(pk=order.pk, status=order.status).update(**updates)
    if not changed:
        return False  # raced with another transition; caller re-reads and reports
    order.refresh_from_db()

    if to_status == StoreOrder.Status.DELIVERED:
        consume_order_reservations(order, actor=actor)
    elif to_status == StoreOrder.Status.CANCELLED:
        release_order_reservations(order)
        if order.doctrine_fit_id:
            recompute_supply_need(order.doctrine_fit, location=order.delivery_location)
    notify_order_status(order, actor=actor)
    return True


def update_order_eta(order: StoreOrder, new_eta, *, actor, reason: str = "") -> None:
    """Revise the living delivery estimate. The order-time ``promised_date`` is
    immutable; who changed what, when and why is kept for the audit trail."""
    order.current_eta = new_eta
    order.eta_changed_by = actor
    order.eta_changed_at = timezone.now()
    if reason:
        order.delay_reason = reason[:300]
    order.save(update_fields=["current_eta", "eta_changed_by", "eta_changed_at", "delay_reason"])
    notify_order_eta_changed(order, actor=actor)


# The status path each order kind walks, in order. Each step is advanced by the
# claiming member (or an officer); the buyer/officer confirms delivery.
def _flow(order: StoreOrder) -> list[str]:
    S = StoreOrder.Status
    if order.requires_build:
        return [S.CLAIMED, S.DEPOSIT_PAID, S.IN_PRODUCTION, S.READY, S.DELIVERED]
    if order.has_backorder:
        # A backordered fit is produced/procured before it is ready — no deposit,
        # but the board should show it is being worked, not imply it sits on a shelf.
        return [S.CLAIMED, S.IN_PRODUCTION, S.READY, S.DELIVERED]
    return [S.CLAIMED, S.READY, S.DELIVERED]


def next_status(order: StoreOrder) -> str | None:
    """The status that follows the current one for this order's kind."""
    flow = _flow(order)
    if order.status not in flow:
        return None
    i = flow.index(order.status)
    return flow[i + 1] if i + 1 < len(flow) else None


_STATUS_BODY = {
    StoreOrder.Status.CLAIMED: "{ship} was claimed — a member is fulfilling it.",
    StoreOrder.Status.DEPOSIT_PAID: "Deposit received for {ship}.",
    StoreOrder.Status.IN_PRODUCTION: "{ship} is in production.",
    StoreOrder.Status.READY: "{ship} is ready — coordinate pickup.",
    StoreOrder.Status.DELIVERED: "{ship} was delivered. Fly safe!",
    StoreOrder.Status.CANCELLED: "Your order for {ship} was cancelled.",
}

# The per-status message scaffold that re-renders this DM in the buyer's own language
# (apps.pingboard.messages.SCAFFOLDS). ``_STATUS_BODY`` above stays the frozen English
# audit/fallback column for a status with no scaffold.
_STATUS_TEMPLATE = {
    StoreOrder.Status.CLAIMED: "store.order_status.claimed",
    StoreOrder.Status.DEPOSIT_PAID: "store.order_status.deposit_paid",
    StoreOrder.Status.IN_PRODUCTION: "store.order_status.in_production",
    StoreOrder.Status.READY: "store.order_status.ready",
    StoreOrder.Status.DELIVERED: "store.order_status.delivered",
    StoreOrder.Status.CANCELLED: "store.order_status.cancelled",
}

# The CANONICAL ENGLISH audit title per status — deliberately plain strings and NOT
# ``get_status_display()``: that is a gettext_lazy proxy which resolves under whatever locale is
# active at emit time, i.e. the *acting officer's* language on a request path. Every word of it
# already exists translatably inside the matching scaffold's ``subject`` msgid, which is what the
# buyer actually reads; this map only freezes ``Alert.title`` (the English audit column).
_STATUS_TITLE = {
    StoreOrder.Status.CLAIMED: "Order update: Claimed",
    StoreOrder.Status.DEPOSIT_PAID: "Order update: Deposit paid",
    StoreOrder.Status.IN_PRODUCTION: "Order update: In production",
    StoreOrder.Status.READY: "Order update: Ready",
    StoreOrder.Status.DELIVERED: "Order update: Delivered",
    StoreOrder.Status.CANCELLED: "Order update: Cancelled",
}


def notify_order_status(order: StoreOrder, *, actor=None) -> None:
    """MKT-5 (3.18): DM the buyer their order's new status — one non-spammy ping per change.

    Skips the initial/reopened OPEN state and any change the buyer triggered themselves; gated
    by the ``store.order_status`` event; deduped per (order, status). Best-effort: a
    notification failure never breaks the store action.
    """
    if order.status == StoreOrder.Status.OPEN or not order.buyer_id:
        return
    if actor is not None and getattr(actor, "id", None) == order.buyer_id:
        return  # don't ping the buyer about their own action
    # The whole notification path is best-effort — a config read, render or emit failure
    # must never break the store action (the state is already committed by the caller).
    try:
        from apps.pingboard import services as pingboard
        from apps.pingboard.models import AlertCategory
        from apps.pingboard.notifications import is_enabled

        if not is_enabled("store.order_status"):
            return
        ship = order.ship_name or order.fit_name or "your order"
        body = _STATUS_BODY.get(order.status, "Your order status changed.").format(ship=ship)
        pingboard.emit_broadcast(
            category=AlertCategory.ANNOUNCEMENT,
            title=_STATUS_TITLE.get(order.status, "Order update"), body=body,
            # The scaffold + raw context re-render this line in the buyer's language; ``body`` and
            # ``title`` above stay the frozen English audit columns. The status LABEL is chrome and
            # lives inside the per-status scaffold msgids — never in ``context``, whose slots are
            # interpolated raw and would otherwise freeze the acting officer's locale.
            template=_STATUS_TEMPLATE.get(order.status),
            context={"ship_name": ship},
            audience={"kind": "user", "id": order.buyer_id},
            source_service="store", source_object_id=f"order_status:{order.id}:{order.status}",
            idempotency_key=f"store:order_status:{order.id}:{order.status}",
        )
    except Exception:  # noqa: BLE001 — a notification must never break the store action
        import logging

        logging.getLogger("forca.store").exception("order-status DM failed (order %s)", order.id)


def _notify_buyer(order: StoreOrder, *, event: str, title: str, body: str,
                  template: str, context: dict) -> None:
    """Best-effort buyer DM (frozen-English audit columns + per-recipient scaffold),
    mirroring :func:`notify_order_status`. Never raises into the caller."""
    if not order.buyer_id:
        return
    try:
        from apps.pingboard import services as pingboard
        from apps.pingboard.models import AlertCategory
        from apps.pingboard.notifications import is_enabled

        if not is_enabled("store.order_status"):
            return
        pingboard.emit_broadcast(
            category=AlertCategory.ANNOUNCEMENT,
            title=title, body=body, template=template, context=context,
            audience={"kind": "user", "id": order.buyer_id},
            source_service="store", source_object_id=f"{event}:{order.id}",
            idempotency_key=f"store:{event}:{order.id}",
        )
    except Exception:  # noqa: BLE001 — a notification must never break the store action
        import logging

        logging.getLogger("forca.store").exception("%s DM failed (order %s)", event, order.id)


def notify_order_eta_changed(order: StoreOrder, *, actor=None) -> None:
    if actor is not None and getattr(actor, "id", None) == order.buyer_id:
        return
    ship = order.ship_name or order.fit_name or "your order"
    eta = order.current_eta.date().isoformat() if order.current_eta else "unknown"
    _notify_buyer(
        order,
        event=f"order_eta:{eta}",
        title="Order update: New delivery estimate",
        body=f"The estimated delivery for {ship} is now {eta}.",
        template="store.order_eta_changed",
        context={"ship_name": ship, "eta_date": eta},
    )


def notify_stock_allocated(order: StoreOrder, quantity: int) -> None:
    ship = order.ship_name or order.fit_name or "your order"
    _notify_buyer(
        order,
        event=f"order_allocated:{quantity}:{order.quantity_backordered}",
        title="Order update: Stock reserved",
        body=f"{quantity}× {ship} from a new delivery is now reserved for your order.",
        template="store.order_stock_allocated",
        context={"ship_name": ship, "quantity": quantity},
    )


def notify_reservation_expired(order: StoreOrder) -> None:
    ship = order.ship_name or order.fit_name or "your order"
    _notify_buyer(
        order,
        event="order_reservation_expired",
        title="Order update: Reservation released",
        body=(
            f"Your reserved {ship} was released after waiting unclaimed; "
            "the order is now backordered."
        ),
        template="store.order_reservation_expired",
        context={"ship_name": ship},
    )


def advance_label(order: StoreOrder) -> str:
    """Friendly label for the 'move to next status' button."""
    return {
        StoreOrder.Status.DEPOSIT_PAID: _("Confirm deposit paid"),
        StoreOrder.Status.IN_PRODUCTION: _("Start production"),
        StoreOrder.Status.READY: _("Mark ready (contract up)"),
        StoreOrder.Status.DELIVERED: _("Mark delivered"),
    }.get(next_status(order) or "", _("Advance"))
