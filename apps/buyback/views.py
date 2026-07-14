"""Buyback & appraisal views.

The appraisal tool and offer board honour the leadership-set audience (default
corp & alliance members). Any user who can access the service can paste items for
an instant appraisal, post a lot for buyback, and buy another member's lot.
Editing the config (rates + audience) is officer-only.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import pilots, rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from .appraisal import appraise
from .forms import AppraisalForm, ConfigForm, GuaranteedConfigForm
from .models import BuybackOffer, SecBand
from .services import (
    active_config,
    can_access,
    current_audience,
    invalidate_audience_cache,
)

# Absolute ceiling for the offer/jita DecimalField(max_digits=20, decimal_places=2).
_MAX_OFFER_TOTAL = Decimal(10) ** 18


def _gate(request):
    """Return an 'unavailable' response if the user can't use the service, else None."""
    if not can_access(request.user):
        return render(request, "buyback/unavailable.html", {
            "audience": current_audience(),
            "authenticated": request.user.is_authenticated,
        }, status=403)
    return None


def appraisal(request: HttpRequest) -> HttpResponse:
    """Live appraisal: paste items, see the instant buyback offer."""
    blocked = _gate(request)
    if blocked:
        return blocked

    cfg = active_config()
    form = AppraisalForm(request.POST or None)
    result = None
    if request.method == "POST" and form.is_valid():
        sec_band = form.cleaned_data["sec_band"]
        # Ore mode only when the config allows it AND the seller ticked it.
        ore_mode = cfg.ore_mode_enabled and form.cleaned_data.get("ore", False)
        result = appraise(
            form.cleaned_data["items"], sec_band=sec_band, rate=cfg.rate_for(sec_band),
            ore_mode=ore_mode, reprocessing_pct=cfg.reprocessing_pct,
        )
        request.session["buyback_pending"] = {
            "sec_band": sec_band,
            "location_name": form.cleaned_data.get("location_name", ""),
            "notes": form.cleaned_data.get("notes", ""),
            "items": result.manifest(),
            "jita_total": str(result.jita_total),
            "offer_total": str(result.offer_total),
            "volume_m3": result.volume_m3,
            "item_count": result.item_count,
            "rate": str(result.rate),
        }

    return render(request, "buyback/appraisal.html", {
        "form": form, "result": result, "cfg": cfg,
        "rates": [
            (SecBand.HIGHSEC.label, cfg.highsec_pct),
            (SecBand.LOWSEC.label, cfg.lowsec_pct),
            (SecBand.NULLSEC.label, cfg.nullsec_pct),
        ],
        "can_submit": request.user.is_authenticated and bool(result and result.lines),
        "open_count": BuybackOffer.objects.filter(status=BuybackOffer.Status.OPEN).count(),
        "guaranteed_available": _guaranteed_available(request.user),
    })


def _guaranteed_available(user) -> bool:
    from . import guaranteed as gb

    return gb.can_request(user)


@login_required
@require_POST
def submit_offer(request: HttpRequest) -> HttpResponse:
    """Post the last appraisal as an open buyback offer on the board."""
    blocked = _gate(request)
    if blocked:
        return blocked

    pending = request.session.get("buyback_pending")
    if not pending or not pending.get("items"):
        messages.error(request, _("Run an appraisal first, then post it."))
        return redirect("buyback:appraisal")


    main = pilots.acting_pilot(request.user)  # LP-3: the pilot the user is FLYING, not the account's main.
    jita_total = Decimal(pending.get("jita_total", "0"))
    offer_total = Decimal(pending.get("offer_total", "0"))
    # jita_total/offer_total are DecimalField(max_digits=20, decimal_places=2), so
    # the absolute value must stay below 1e18. Reject an out-of-range appraisal with
    # a friendly error rather than letting the create() raise an unhandled 500.
    if max(abs(jita_total), abs(offer_total)) >= _MAX_OFFER_TOTAL:
        messages.error(request, _("That appraisal is too large to post as a single lot."))
        return redirect("buyback:appraisal")
    offer = BuybackOffer.objects.create(
        seller=request.user,
        seller_character_id=main.character_id if main else None,
        location_name=pending.get("location_name", ""),
        sec_band=pending.get("sec_band", SecBand.HIGHSEC),
        rate_pct=Decimal(pending.get("rate", "0.9")),
        items=pending.get("items", []),
        item_count=pending.get("item_count", 0),
        volume_m3=pending.get("volume_m3", 0.0),
        jita_total=jita_total,
        offer_total=offer_total,
        notes=pending.get("notes", ""),
    )
    request.session.pop("buyback_pending", None)
    audit_log(request.user, "buyback.post", target_type="buyback_offer",
              target_id=str(offer.id), ip=client_ip(request))
    messages.success(request, _("Lot posted to the buyback board."))
    return redirect("buyback:board")


@login_required
def board(request: HttpRequest) -> HttpResponse:
    """The buyback board: open lots to buy, your lots, and recent deals."""
    blocked = _gate(request)
    if blocked:
        return blocked

    open_offers = BuybackOffer.objects.filter(
        status=BuybackOffer.Status.OPEN
    ).select_related("seller").order_by("-created_at")
    mine = BuybackOffer.objects.filter(seller=request.user).order_by("-created_at")[:20]
    bought = BuybackOffer.objects.filter(buyer=request.user).order_by("-purchased_at")[:20]
    return render(request, "buyback/board.html", {
        "open_offers": open_offers,
        "mine": mine,
        "bought": bought,
        "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
    })


@login_required
def offer_detail(request: HttpRequest, pk: int) -> HttpResponse:
    blocked = _gate(request)
    if blocked:
        return blocked
    offer = get_object_or_404(BuybackOffer, pk=pk)
    # Object-level scope (mirror store.order_detail): a lot's item manifest, ISK
    # valuation and buyer<->seller pairing are visible only to its seller, its buyer,
    # an officer, or — while it is still OPEN on the board — any corp member. Without
    # this, any gated user could pk-enumerate every other member's settled/cancelled
    # lots. buyer/seller can be NULL, so the identity checks must never None==None-match.
    uid = request.user.id if request.user.is_authenticated else None
    is_party = uid is not None and (offer.seller_id == uid or offer.buyer_id == uid)
    if not (is_party
            or rbac.has_role(request.user, rbac.ROLE_OFFICER)
            or (offer.status == BuybackOffer.Status.OPEN
                and rbac.has_role(request.user, rbac.ROLE_MEMBER))):
        raise PermissionDenied
    return render(request, "buyback/offer.html", {
        "offer": offer,
        "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
        "is_seller": offer.seller_id == getattr(request.user, "id", None),
    })


@login_required
@require_POST
def buy_offer(request: HttpRequest, pk: int) -> HttpResponse:
    """Buy a corpmate's lot: claim it, then settle in-game."""
    blocked = _gate(request)
    if blocked:
        return blocked

    offer = get_object_or_404(BuybackOffer, pk=pk)
    if offer.seller_id == request.user.id:
        messages.error(request, _("You can't buy your own lot."))
        return redirect("buyback:board")


    main = pilots.acting_pilot(request.user)  # LP-3: the pilot the user is FLYING, not the account's main.
    # Atomic reserve: only one buyer can win the OPEN→PURCHASED transition.
    bought = BuybackOffer.objects.filter(pk=pk, status=BuybackOffer.Status.OPEN).update(
        status=BuybackOffer.Status.PURCHASED,
        buyer=request.user,
        buyer_character_id=main.character_id if main else None,
        purchased_at=timezone.now(),
    )
    if not bought:
        messages.error(request, _("That lot is no longer available."))
        return redirect("buyback:board")
    audit_log(request.user, "buyback.buy", target_type="buyback_offer",
              target_id=str(offer.id), ip=client_ip(request))
    messages.success(
        request,
        _("Lot reserved. Pay the seller and have them contract the items to you in-game."),
    )
    return redirect("buyback:offer", pk=offer.pk)


@login_required
@require_POST
def offer_action(request: HttpRequest, pk: int) -> HttpResponse:
    """Seller cancels an open lot; buyer/officer marks a purchase paid."""
    blocked = _gate(request)
    if blocked:
        return blocked
    offer = get_object_or_404(BuybackOffer, pk=pk)
    action = request.POST.get("action", "")
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)

    if action == "cancel":
        if not (offer.seller_id == request.user.id or is_officer):
            messages.error(request, _("Not your lot."))
            return redirect("buyback:board")
        if offer.status not in (BuybackOffer.Status.OPEN, BuybackOffer.Status.PURCHASED):
            messages.error(request, _("That lot can't be cancelled."))
            return redirect("buyback:board")
        offer.status = BuybackOffer.Status.CANCELLED
    elif action == "paid":
        if not (offer.buyer_id == request.user.id or offer.seller_id == request.user.id or is_officer):
            messages.error(request, _("Only the buyer, seller, or an officer can settle this."))
            return redirect("buyback:board")
        if offer.status != BuybackOffer.Status.PURCHASED:
            messages.error(request, _("That lot isn't awaiting payment."))
            return redirect("buyback:board")
        offer.status = BuybackOffer.Status.PAID
    else:
        messages.error(request, _("Unknown action."))
        return redirect("buyback:board")

    offer.save(update_fields=["status"])
    audit_log(request.user, f"buyback.{action}", target_type="buyback_offer",
              target_id=str(offer.id), ip=client_ip(request))
    messages.success(request, _("Lot updated."))
    return redirect("buyback:board")


@login_required
@role_required(rbac.ROLE_OFFICER)
def config(request: HttpRequest) -> HttpResponse:
    """Officer config: per-location rates + audience, and the corp-funded guaranteed
    buyback (4.20) arming + safety rails (two forms, one page)."""
    from .guaranteed import active_config as gb_active_config

    cfg = active_config()
    gb_cfg = gb_active_config()
    form = ConfigForm(instance=cfg)
    gb_form = GuaranteedConfigForm(instance=gb_cfg)
    if request.method == "POST":
        if "save_guaranteed" in request.POST:
            gb_form = GuaranteedConfigForm(request.POST, instance=gb_cfg)
            if gb_form.is_valid():
                gb_form.save()
                audit_log(request.user, "guaranteed_buyback.config_update",
                          target_type="guaranteed_buyback_config", target_id=str(gb_cfg.id),
                          ip=client_ip(request))
                messages.success(request, _("Guaranteed buyback settings updated."))
                return redirect("buyback:config")
        else:
            form = ConfigForm(request.POST, instance=cfg)
            if form.is_valid():
                form.save()
                invalidate_audience_cache()
                audit_log(request.user, "buyback.config_update", target_type="buyback_config",
                          target_id=str(cfg.id), ip=client_ip(request))
                messages.success(request, _("Buyback settings updated."))
                return redirect("buyback:config")
    return render(request, "buyback/config.html",
                  {"form": form, "cfg": cfg, "gb_form": gb_form, "gb_cfg": gb_cfg})


# --- Corp-funded guaranteed buyback (4.20) -----------------------------------
@login_required
@require_POST
def guaranteed_request(request: HttpRequest) -> HttpResponse:
    """A member asks the corp to guarantee-buy the last appraised lot. Inert unless the
    feature is armed + the member is in its audience + the lot is within the per-lot cap."""

    from . import guaranteed as gb

    if not gb.can_request(request.user):
        messages.error(request, _("Guaranteed buyback isn't available to you right now."))
        return redirect("buyback:appraisal")
    pending = request.session.get("buyback_pending")
    if not pending or not pending.get("items"):
        messages.error(request, _("Run an appraisal first, then request the corp buyout."))
        return redirect("buyback:appraisal")
    main = pilots.acting_pilot(request.user)  # LP-3: the pilot the user is FLYING, not the account's main.
    jita = Decimal(pending.get("jita_total", "0"))
    quoted = Decimal(pending.get("offer_total", "0"))
    if max(abs(jita), abs(quoted)) >= _MAX_OFFER_TOTAL:
        messages.error(request, _("That appraisal is too large for a single guaranteed lot."))
        return redirect("buyback:appraisal")
    buyout = gb.request_buyout(
        request.user, seller_character_id=main.character_id if main else None,
        items=pending.get("items", []), item_count=pending.get("item_count", 0),
        volume_m3=pending.get("volume_m3", 0.0), jita_value=jita, quoted_value=quoted,
        location_name=pending.get("location_name", ""), notes=pending.get("notes", ""),
    )
    if buyout is None:
        messages.error(
            request, _("That lot is above the guaranteed-buyback per-lot cap — post it to the board instead.")
        )
        return redirect("buyback:appraisal")
    request.session.pop("buyback_pending", None)
    audit_log(request.user, "guaranteed_buyback.request", target_type="guaranteed_buyout",
              target_id=str(buyout.id), ip=client_ip(request))
    messages.success(request, _("Requested — an officer will review the corp buyout. No ISK moves through the app."))
    return redirect("buyback:appraisal")


@login_required
@require_POST
def guaranteed_cancel(request: HttpRequest, pk: int) -> HttpResponse:
    """The seller withdraws their own still-pending request."""
    from . import guaranteed as gb

    if gb.cancel_buyout(pk, request.user):
        messages.success(request, _("Request withdrawn."))
    else:
        messages.error(request, _("You can't withdraw that request."))
    return redirect("buyback:appraisal")


@login_required
@role_required(rbac.ROLE_OFFICER)
def guaranteed_queue(request: HttpRequest) -> HttpResponse:
    """Officer review of guaranteed-buyout requests + those awaiting corp payment."""
    from . import guaranteed as gb
    from .models import GuaranteedBuyout

    config = gb.active_config()
    pending = list(
        GuaranteedBuyout.objects.filter(status=GuaranteedBuyout.Status.REQUESTED).select_related("seller")
    )
    for b in pending:
        b.blocker = gb.approval_blocker(b, request.user, config)
    approved = list(
        GuaranteedBuyout.objects.filter(status=GuaranteedBuyout.Status.APPROVED).select_related("seller")
    )
    recent = list(
        GuaranteedBuyout.objects.filter(
            status__in=[GuaranteedBuyout.Status.SETTLED, GuaranteedBuyout.Status.REJECTED,
                        GuaranteedBuyout.Status.CANCELLED]
        ).select_related("seller")[:15]
    )
    return render(request, "buyback/guaranteed_queue.html", {
        "config": config, "pending": pending, "approved": approved, "recent": recent,
        "committed_24h": gb.committed_last_24h(),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def guaranteed_decide(request: HttpRequest, pk: int) -> HttpResponse:
    from . import guaranteed as gb

    approve = request.POST.get("decision") == "approve"
    reason = request.POST.get("reason", "")
    if approve:
        ok, msg = gb.approve_buyout(pk, request.user, reason)
    else:
        ok = gb.reject_buyout(pk, request.user, reason)
        msg = _("Rejected.") if ok else _("Couldn't reject that request.")
    (messages.success if ok else messages.error)(request, msg)
    if ok:
        audit_log(request.user, f"guaranteed_buyback.{'approve' if approve else 'reject'}",
                  target_type="guaranteed_buyout", target_id=str(pk), ip=client_ip(request))
    return redirect("buyback:guaranteed_queue")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def guaranteed_settle(request: HttpRequest, pk: int) -> HttpResponse:
    """Officer confirms an out-of-band corp payment (only when ESI reconcile is off)."""
    from . import guaranteed as gb

    ok, msg = gb.mark_settled_manual(pk, request.user, request.POST.get("reference", ""))
    (messages.success if ok else messages.error)(request, msg)
    if ok:
        audit_log(request.user, "guaranteed_buyback.settle_manual", target_type="guaranteed_buyout",
                  target_id=str(pk), ip=client_ip(request))
    return redirect("buyback:guaranteed_queue")
