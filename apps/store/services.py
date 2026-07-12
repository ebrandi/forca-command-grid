"""Store services: config, access control, order creation, and the status flow."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.core.cache import cache
from django.utils.translation import gettext as _

from .models import Audience, StoreConfig, StoreOrder
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
                 location_name="", notes="") -> StoreOrder:
    """Persist a priced order. Hulls require a build deposit; fits don't."""
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
        deposit_pct=deposit_pct,
        deposit_amount=deposit,
        requires_build=requires_build,
        location_name=location_name,
        notes=notes,
    )
    from apps.pingboard import hooks

    hooks.fire("store.new", source_object_id=order.id, dedup_suffix="new",
               context={"ship_name": priced.ship_name or ""})
    return order


# The status path each order kind walks, in order. Each step is advanced by the
# claiming member (or an officer); the buyer/officer confirms delivery.
def _flow(order: StoreOrder) -> list[str]:
    S = StoreOrder.Status
    if order.requires_build:
        return [S.CLAIMED, S.DEPOSIT_PAID, S.IN_PRODUCTION, S.READY, S.DELIVERED]
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
        label = order.get_status_display()
        body = _STATUS_BODY.get(order.status, f"Your order is now “{label}”.").format(ship=ship)
        pingboard.emit_broadcast(
            category=AlertCategory.ANNOUNCEMENT, title=f"Order update: {label}", body=body,
            audience={"kind": "user", "id": order.buyer_id},
            source_service="store", source_object_id=f"order_status:{order.id}:{order.status}",
            idempotency_key=f"store:order_status:{order.id}:{order.status}",
        )
    except Exception:  # noqa: BLE001 — a notification must never break the store action
        import logging

        logging.getLogger("forca.store").exception("order-status DM failed (order %s)", order.id)


def advance_label(order: StoreOrder) -> str:
    """Friendly label for the 'move to next status' button."""
    return {
        StoreOrder.Status.DEPOSIT_PAID: _("Confirm deposit paid"),
        StoreOrder.Status.IN_PRODUCTION: _("Start production"),
        StoreOrder.Status.READY: _("Mark ready (contract up)"),
        StoreOrder.Status.DELIVERED: _("Mark delivered"),
    }.get(next_status(order) or "", _("Advance"))
