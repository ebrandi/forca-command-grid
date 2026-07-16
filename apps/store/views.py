"""Corp Store views.

Shopping (browse + order) honours the leadership-set audience (default corp &
alliance). The fulfilment board, where members claim and build orders, is
corp-only. Editing the markups, deposit and audience is officer-only.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.doctrines.models import DoctrineFit
from core import pilots, rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from .forms import (
    ConfigForm,
    FitOrderForm,
    HullOrderForm,
    OrderEtaForm,
)
from .models import FitWaitlistEntry, ShipyardPolicy, StoreOrder
from .pricing import is_ship, price_doctrine_fit, price_hull_order
from .services import (
    active_config,
    advance_label,
    can_access,
    create_order,
    current_audience,
    invalidate_audience_cache,
    next_status,
    notify_order_status,
    place_fit_order,
    transition_order,
    update_order_eta,
)


def _gate(request):
    if not can_access(request.user):
        return render(request, "store/unavailable.html", {
            "audience": current_audience(),
            "authenticated": request.user.is_authenticated,
        }, status=403)
    return None


def storefront(request: HttpRequest) -> HttpResponse:
    """Browse ready-to-fly doctrine ships and order a made-to-order hull."""
    blocked = _gate(request)
    if blocked:
        return blocked

    # The storefront is the made-to-order shipyard: any hull, up to capitals and
    # supers, built on demand. Ready-to-fly doctrine ships are browsed and ordered
    # on the Shipyard (doctrines:ships), which prices every fit — so we no longer
    # duplicate that catalogue here.
    cfg = active_config()
    return render(request, "store/storefront.html", {
        "cfg": cfg,
        "hull_markup_pct": int(round((float(cfg.hull_markup) - 1) * 100)),
        "capital_markup_pct": int(round((float(cfg.capital_markup) - 1) * 100)),
        "supercap_markup_pct": int(round((float(cfg.supercap_markup) - 1) * 100)),
        "deposit_pct": int(round(float(cfg.deposit_pct) * 100)),
        "is_member": rbac.has_role(request.user, rbac.ROLE_MEMBER),
        "open_count": StoreOrder.objects.filter(status=StoreOrder.Status.OPEN).count(),
    })


def hull_search(request: HttpRequest) -> JsonResponse:
    """Autocomplete restricted to ship hulls, for the made-to-order picker."""
    if not can_access(request.user):
        return JsonResponse([], safe=False, status=403)
    from apps.sde.models import SdeType

    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse([], safe=False)
    rows = list(
        SdeType.objects.filter(name__icontains=q, published=True, group__category_id=6)
        .values("type_id", "name")[:40]
    )
    low = q.lower()
    rows.sort(key=lambda r: (not r["name"].lower().startswith(low), r["name"]))
    return JsonResponse(rows[:15], safe=False)


def system_search(request: HttpRequest) -> JsonResponse:
    """Autocomplete for the 'Deliver to' field — solar systems by name.

    Scoped to the store (gated by the store audience) so the delivery picker keeps
    working even if the Navigation feature is disabled or set to a narrower audience.
    Returns the {type_id,name} shape the shared typePicker component expects.
    """
    if not can_access(request.user):
        return JsonResponse([], safe=False, status=403)
    from apps.sde.search import search_systems

    return JsonResponse(search_systems(request.GET.get("q", ""), limit=15), safe=False)


@login_required
@require_POST
def order_fit(request: HttpRequest) -> HttpResponse:
    """Place an order for a doctrine ship, availability-aware (SHIP-1).

    The split between reserved stock and backorder, the price, the location and
    the delivery estimate are all server-derived under row locks — the form
    carries only (fit, quantity, notes, acknowledgement). When any part would be
    a backorder that the buyer has not yet acknowledged, the request pauses on a
    confirm page showing the authoritative split and estimated delivery."""
    blocked = _gate(request)
    if blocked:
        return blocked
    form = FitOrderForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Could not place that order."))
        return redirect("doctrines:ships")
    fit = get_object_or_404(DoctrineFit, pk=form.cleaned_data["fit_id"])

    placement = place_fit_order(
        fit=fit,
        quantity=form.cleaned_data["quantity"],
        buyer=request.user,
        buyer_character_id=_main_char_id(request.user),
        notes=form.cleaned_data.get("notes", ""),
        acknowledged=form.cleaned_data.get("acknowledge_backorder", False),
        force_backorder=form.cleaned_data.get("force_backorder", False),
    )
    if placement.order is not None:
        audit_log(request.user, "store.order", target_type="store_order",
                  target_id=str(placement.order.id),
                  metadata={
                      "reserved": placement.order.quantity_reserved,
                      "backordered": placement.order.quantity_backordered,
                  }, ip=client_ip(request))
        if placement.order.has_backorder:
            messages.success(request, _(
                "Order placed — %(ready)s reserved now, %(back)s on backorder."
            ) % {"ready": placement.order.quantity_reserved,
                 "back": placement.order.quantity_backordered})
        else:
            messages.success(request, _("Order placed — your ship is reserved from stock."))
        return redirect("store:order", pk=placement.order.pk)

    if not placement.needs_confirm:
        messages.error(request, placement.error or _("Could not place that order."))
        return redirect("doctrines:ships")

    # Confirm step: show the authoritative split + estimate; nothing is reserved yet.
    cfg = active_config()
    priced = price_doctrine_fit(fit, cfg.doctrine_markup)
    return render(request, "store/order_confirm.html", {
        "fit": fit,
        "placement": placement,
        "priced": priced,
        "quantity": placement.quantity,
        "notes": form.cleaned_data.get("notes", ""),
        "unit_price": priced.unit_price if priced.ok else None,
    })


@login_required
@require_POST
def order_hull(request: HttpRequest) -> HttpResponse:
    """Place a made-to-order hull order (subcap, capital or supercapital)."""
    blocked = _gate(request)
    if blocked:
        return blocked
    cfg = active_config()
    form = HullOrderForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Pick a hull and a quantity."))
        return redirect("store:storefront")
    type_id = form.cleaned_data["ship_type_id"]
    if not is_ship(type_id):
        messages.error(request, _("That isn't a ship hull."))
        return redirect("store:storefront")
    priced = price_hull_order(type_id, cfg)
    if not priced.ok:
        messages.error(request, priced.error)
        return redirect("store:storefront")

    order = create_order(
        priced=priced, kind=StoreOrder.Kind.HULL,
        quantity=form.cleaned_data["quantity"], cfg=cfg,
        buyer=request.user, buyer_character_id=_main_char_id(request.user),
        location_name=form.cleaned_data.get("location_name", ""),
        notes=form.cleaned_data.get("notes", ""),
    )
    audit_log(request.user, "store.order", target_type="store_order",
              target_id=str(order.id), ip=client_ip(request))
    messages.success(request, _("Build order placed — a corp member will claim it on the board."))
    return redirect("store:order", pk=order.pk)


def order_detail(request: HttpRequest, pk: int) -> HttpResponse:
    blocked = _gate(request)
    if blocked:
        return blocked
    order = get_object_or_404(StoreOrder, pk=pk)
    # Object-level scope: an order's details (buyer identity, frozen prices, location,
    # notes) are visible only to its buyer, its claimer, an officer, or — while it is
    # still OPEN on the claimable board — any corp member. Prevents pk-enumeration of
    # other pilots' orders by anyone who merely passes the store audience gate.
    # `uid` is None for an anonymous viewer (order_detail has no @login_required; under a
    # PUBLIC store audience the gate lets anyone in) — and buyer/claimer can also be NULL
    # (buyer FK is SET_NULL; unclaimed orders have no claimer), so the identity checks must
    # never let a None==None match grant access.
    uid = request.user.id if request.user.is_authenticated else None
    is_owner = uid is not None and (order.buyer_id == uid or order.claimed_by_id == uid)
    if not (is_owner
            or rbac.has_role(request.user, rbac.ROLE_OFFICER)
            or (order.is_open and rbac.has_role(request.user, rbac.ROLE_MEMBER))):
        raise PermissionDenied
    is_member = rbac.has_role(request.user, rbac.ROLE_MEMBER)
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    is_claimer = order.claimed_by_id == getattr(request.user, "id", None)
    reservations = list(order.fit_reservations.select_related("stock__location"))
    live_reserved = sum(
        r.quantity for r in reservations if r.status == r.Status.ACTIVE
    )
    return render(request, "store/order.html", {
        "order": order,
        "is_member": is_member,
        "is_officer": is_officer,
        "is_buyer": order.buyer_id == getattr(request.user, "id", None),
        "is_claimer": is_claimer,
        "next_status": next_status(order),
        "advance_label": advance_label(order),
        "reservations": reservations,
        "live_reserved": live_reserved,
        "eta_form": (
            OrderEtaForm(initial={
                "current_eta": order.current_eta.date() if order.current_eta else None,
            })
            if (is_claimer or is_officer) and order.has_backorder
            and order.status not in (StoreOrder.Status.DELIVERED, StoreOrder.Status.CANCELLED)
            else None
        ),
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def order_eta(request: HttpRequest, pk: int) -> HttpResponse:
    """Revise an order's living delivery estimate (claimer or officer only).

    The order-time promise stays frozen; the revision, its author and reason are
    tracked and the buyer is notified."""
    order = get_object_or_404(StoreOrder, pk=pk)
    if not (order.claimed_by_id == request.user.id
            or rbac.has_role(request.user, rbac.ROLE_OFFICER)):
        raise PermissionDenied
    if order.status in (StoreOrder.Status.DELIVERED, StoreOrder.Status.CANCELLED):
        messages.error(request, _("That order is closed."))
        return redirect("store:order", pk=order.pk)
    form = OrderEtaForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Pick a valid estimated delivery date."))
        return redirect("store:order", pk=order.pk)
    from datetime import datetime, time

    from django.utils import timezone as tz

    new_eta = tz.make_aware(
        datetime.combine(form.cleaned_data["current_eta"], time(20, 0))
    )
    update_order_eta(order, new_eta, actor=request.user,
                     reason=form.cleaned_data.get("delay_reason", ""))
    audit_log(request.user, "store.order_eta", target_type="store_order",
              target_id=str(order.id),
              metadata={"eta": new_eta.date().isoformat(),
                        "reason": form.cleaned_data.get("delay_reason", "")[:100]},
              ip=client_ip(request))
    messages.success(request, _("Estimated delivery updated — the buyer was notified."))
    return redirect("store:order", pk=order.pk)


@login_required
@require_POST
def waitlist_toggle(request: HttpRequest, fit_id: int) -> HttpResponse:
    """Join/leave the waitlist of a fit that can't currently be ordered."""
    blocked = _gate(request)
    if blocked:
        return blocked
    if not ShipyardPolicy.active().waitlist_enabled:
        messages.error(request, _("The waitlist is not enabled."))
        return redirect("doctrines:ships")
    fit = get_object_or_404(DoctrineFit, pk=fit_id)
    entry = FitWaitlistEntry.objects.filter(fit=fit, user=request.user).first()
    if entry is not None:
        entry.delete()
        messages.info(request, _("You left the waitlist for %(fit)s.") % {"fit": fit.name})
    else:
        FitWaitlistEntry.objects.get_or_create(fit=fit, user=request.user)
        messages.success(request, _(
            "You joined the waitlist for %(fit)s — you'll be pinged when it can be ordered."
        ) % {"fit": fit.name})
    from core.redirects import safe_next

    return redirect(safe_next(request, request.POST.get("next"), "doctrines:ships"))


@login_required
def my_orders(request: HttpRequest) -> HttpResponse:
    """The buyer's own orders — what they've ordered and where each one stands."""
    blocked = _gate(request)
    if blocked:
        return blocked
    orders = StoreOrder.objects.filter(buyer=request.user).order_by("-created_at")
    active_statuses = [
        StoreOrder.Status.OPEN, StoreOrder.Status.CLAIMED, StoreOrder.Status.DEPOSIT_PAID,
        StoreOrder.Status.IN_PRODUCTION, StoreOrder.Status.READY,
    ]
    active = [o for o in orders if o.status in active_statuses]
    history = [o for o in orders if o.status not in active_statuses]
    return render(request, "store/my_orders.html", {
        "active": active,
        "history": history,
        "outstanding_value": sum(
            (o.total_price for o in active if o.status != StoreOrder.Status.CANCELLED), start=0
        ),
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
def supply_forecast(request: HttpRequest) -> HttpResponse:
    """Builder/trader tool: which doctrine ships are worth stocking, by demand
    (recent losses) and the profit of supplying them vs build/import + freight cost."""
    from apps.admin_audit.models import AppSetting
    from apps.sde.models import SdeSolarSystem

    from .forecast import supply_forecast as build_forecast

    stored = AppSetting.get("store.staging_system_id", {}) or {}
    staging_id = stored.get("system_id") or 0
    # Staging is read from the GET filter (preview) or the officer POST "pin default" form.
    staging_q = (request.POST.get("staging") or request.GET.get("staging") or "").strip()
    if staging_q:
        sys = None
        if staging_q.isdigit():
            sys = SdeSolarSystem.objects.filter(system_id=int(staging_q)).first()
        if sys is None:
            sys = SdeSolarSystem.objects.filter(name__iexact=staging_q).first()
        if sys:
            staging_id = sys.system_id
            # Persisting the corp-wide default is a state change: require a POST (so the
            # CSRF token is enforced — a lure like <img src="...?staging=X"> can no longer
            # silently flip it) AND the officer role. A GET only previews.
            if request.method == "POST" and rbac.has_role(request.user, rbac.ROLE_OFFICER):
                AppSetting.objects.update_or_create(
                    key="store.staging_system_id",
                    defaults={"value": {"system_id": staging_id, "name": sys.name}},
                )
                messages.success(request, _("Corp default staging set to %(name)s.") % {"name": sys.name})

    try:
        window = max(7, min(int(request.POST.get("window") or request.GET.get("window") or 30), 180))
    except (TypeError, ValueError):
        window = 30

    staging_sys = (
        SdeSolarSystem.objects.filter(system_id=staging_id).first() if staging_id else None
    )
    data = build_forecast(window_days=window, staging_system_id=staging_id, limit=50)
    return render(request, "store/supply_forecast.html", {
        "data": data, "rows": data["rows"], "window": window,
        "staging_sys": staging_sys, "staging_q": staging_q,
        "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
def board(request: HttpRequest) -> HttpResponse:
    """Corp-only fulfilment board: open orders to claim, plus your active builds."""
    open_orders = StoreOrder.objects.filter(
        status=StoreOrder.Status.OPEN
    ).select_related("buyer").order_by("-created_at")
    mine = StoreOrder.objects.filter(claimed_by=request.user).exclude(
        status__in=[StoreOrder.Status.DELIVERED, StoreOrder.Status.CANCELLED]
    ).order_by("created_at")
    done = StoreOrder.objects.filter(
        claimed_by=request.user, status=StoreOrder.Status.DELIVERED
    ).order_by("-updated_at")[:10]
    return render(request, "store/board.html", {
        "open_orders": open_orders,
        "mine": mine,
        "done": done,
        "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def claim_order(request: HttpRequest, pk: int) -> HttpResponse:
    order = get_object_or_404(StoreOrder, pk=pk)
    if order.buyer_id == request.user.id:
        messages.error(request, _("You can't fulfil your own order."))
        return redirect("store:board")
    # Atomic claim: only one member can win the OPEN→CLAIMED transition, even if
    # two click "Claim" at the same moment.
    claimed = StoreOrder.objects.filter(pk=pk, status=StoreOrder.Status.OPEN).update(
        status=StoreOrder.Status.CLAIMED,
        claimed_by=request.user,
        claimed_by_character_id=_main_char_id(request.user),
    )
    if not claimed:
        messages.error(request, _("That order is no longer open."))
        return redirect("store:board")
    audit_log(request.user, "store.claim", target_type="store_order",
              target_id=str(order.id), ip=client_ip(request))
    order.refresh_from_db()  # .update() didn't touch the in-memory instance
    notify_order_status(order, actor=request.user)
    messages.success(request, _("Order claimed. Coordinate with the buyer and fulfil it."))
    return redirect("store:order", pk=order.pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def advance_order(request: HttpRequest, pk: int) -> HttpResponse:
    """Move a claimed order to its next status (claimer or officer)."""
    order = get_object_or_404(StoreOrder, pk=pk)
    if not (order.claimed_by_id == request.user.id or rbac.has_role(request.user, rbac.ROLE_OFFICER)):
        messages.error(request, _("That isn't your build."))
        return redirect("store:board")
    nxt = next_status(order)
    if not nxt:
        messages.error(request, _("Nothing to advance."))
        return redirect("store:order", pk=order.pk)
    # transition_order applies the availability side effects too: DELIVERED
    # consumes the order's stock reservations exactly once and stamps the
    # actual dates; READY stamps actual_ready_at. Compare-and-swap: if the
    # order changed under us (a concurrent cancel), nothing is applied.
    if not transition_order(order, nxt, actor=request.user):
        messages.error(request, _("That order just changed — review it and try again."))
        return redirect("store:order", pk=order.pk)
    audit_log(request.user, "store.advance", target_type="store_order",
              target_id=str(order.id), metadata={"to": nxt}, ip=client_ip(request))
    messages.success(request, _("Order moved to “%(status)s”.") % {"status": order.get_status_display()})
    return redirect("store:order", pk=order.pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def order_action(request: HttpRequest, pk: int) -> HttpResponse:
    """Release a claim back to the board, or cancel an order."""
    order = get_object_or_404(StoreOrder, pk=pk)
    action = request.POST.get("action", "")
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    is_claimer = order.claimed_by_id == request.user.id
    is_buyer = order.buyer_id == request.user.id

    if action == "release":
        if not (is_claimer or is_officer) or order.status != StoreOrder.Status.CLAIMED:
            messages.error(request, _("Can only release a freshly claimed order."))
            return redirect("store:board")
        # Status-guarded like claim_order: only the CLAIMED→OPEN transition can win.
        released = StoreOrder.objects.filter(
            pk=order.pk, status=StoreOrder.Status.CLAIMED
        ).update(status=StoreOrder.Status.OPEN, claimed_by=None, claimed_by_character_id=None)
        if not released:
            messages.error(request, _("That order just changed — review it and try again."))
            return redirect("store:order", pk=order.pk)
    elif action == "cancel":
        if not (is_buyer or is_officer):
            messages.error(request, _("Only the buyer or an officer can cancel."))
            return redirect("store:order", pk=order.pk)
        if order.status in (StoreOrder.Status.DELIVERED, StoreOrder.Status.CANCELLED):
            messages.error(request, _("That order can't be cancelled."))
            return redirect("store:order", pk=order.pk)
        # Releases the order's stock reservations and refreshes the supply need;
        # the buyer isn't pinged about their own cancellation. CAS: a concurrent
        # delivery wins and the cancel is refused instead of resurrecting it.
        if not transition_order(order, StoreOrder.Status.CANCELLED, actor=request.user):
            messages.error(request, _("That order just changed — review it and try again."))
            return redirect("store:order", pk=order.pk)
    else:
        messages.error(request, _("Unknown action."))
        return redirect("store:order", pk=order.pk)
    audit_log(request.user, f"store.{action}", target_type="store_order",
              target_id=str(order.id), ip=client_ip(request))
    messages.success(request, _("Order updated."))
    return redirect("store:order", pk=order.pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
def config(request: HttpRequest) -> HttpResponse:
    cfg = active_config()
    if request.method == "POST":
        form = ConfigForm(request.POST, instance=cfg)
        if form.is_valid():
            form.save()
            invalidate_audience_cache()
            audit_log(request.user, "store.config_update", target_type="store_config",
                      target_id=str(cfg.id), ip=client_ip(request))
            messages.success(request, _("Store settings updated."))
            return redirect("store:config")
    else:
        form = ConfigForm(instance=cfg)
    return render(request, "store/config.html", {"form": form, "cfg": cfg})


def _main_char_id(user):

    char = pilots.acting_pilot(user)  # LP-3: the pilot the user is FLYING, not the account's main.
    return char.character_id if char else None
