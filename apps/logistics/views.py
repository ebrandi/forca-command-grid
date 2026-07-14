"""Freight service views.

The rate calculator is public (no login) so prospective customers from other
corps and alliances can price a job — it's the shop window. Posting, claiming and
running contracts is for corp members (the haulers who earn the ISK); editing the
rate card is officer-only.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from apps.sde.search import search_systems
from core import pilots, rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from .forms import QuoteForm, RateCardForm
from .jumps import jump_range_ly
from .locations import resolve_location, search_locations
from .models import CourierContract, ShipClass
from .pricing import quote as price_quote
from .routing import RouteUnavailable, jf_route_facts, route_facts
from .services import (
    active_rate_card,
    can_access,
    create_contract_from_quote,
    current_audience,
    invalidate_audience_cache,
    poster_identity,
)
from .structures import has_structure_search

_UNRESOLVED_MSG = gettext_lazy("Couldn't resolve one of those locations — pick from the list or enter jumps manually.")


def _build_quote(form_data, card):
    """Resolve the endpoints + route and price it. Returns (quote, route_ctx, error)."""
    ship_class = form_data["ship_class"]
    volume = form_data["volume_m3"]
    collateral = form_data["collateral"] or 0
    rush = form_data.get("rush", False)

    origin = resolve_location(
        form_data.get("origin_kind", ""), form_data.get("origin_id", ""),
        form_data.get("origin_name", ""), form_data.get("origin_system_id"),
    )
    dest = resolve_location(
        form_data.get("dest_kind", ""), form_data.get("dest_id", ""),
        form_data.get("dest_name", ""), form_data.get("dest_system_id"),
    )
    route_ctx = {
        "origin": origin, "dest": dest,
        "origin_name": origin["name"] if origin else form_data.get("origin_name", ""),
        "dest_name": dest["name"] if dest else form_data.get("dest_name", ""),
    }

    manual_jumps = form_data.get("jumps")
    have_location = bool(form_data.get("origin_name") or form_data.get("dest_name"))

    # Jump Freighters route over the cyno proximity graph (not stargates): price
    # on the fewest cyno jumps at the corp's assumed JDC range.
    if ship_class == ShipClass.JF:
        range_ly = jump_range_ly(card.jf_assumed_jdc)
        route_ctx.update(jdc=card.jf_assumed_jdc, range_ly=round(range_ly, 1))
        hops = None
        sec_band = "nullsec"
        if origin and dest:
            try:
                facts = jf_route_facts(origin["system_id"], dest["system_id"], range_ly)
                hops = facts["jumps"]
                sec_band = facts["sec_band"]
                route_ctx.update(jumps=hops, jump_hops=hops, ly=facts["ly"],
                                 sec_band=sec_band, resolved=True)
            except RouteUnavailable as exc:
                if not manual_jumps:
                    return None, route_ctx, str(exc)
        elif have_location and not manual_jumps:
            return None, route_ctx, _UNRESOLVED_MSG
        if hops is None:  # no graphed route — use the manual jump count
            hops = manual_jumps
        if not hops:
            return None, route_ctx, _("Pick a route or enter a manual jump count.")
        q = price_quote(
            card, ship_class=ship_class, jumps=hops, jump_hops=hops,
            volume_m3=volume, collateral=collateral, sec_band=sec_band, rush=rush,
        )
        route_ctx.update(jumps=hops, jump_hops=hops, sec_band=sec_band)
        return q, route_ctx, ("" if q.ok else q.error)

    # Freighter / DST / Blockade Runner: stargate routing via ESI.
    jumps = manual_jumps
    lowsec_jumps = 0
    sec_band = "highsec"
    if origin and dest:
        try:
            facts = route_facts(origin["system_id"], dest["system_id"])
            jumps = facts["jumps"]
            lowsec_jumps = facts["lowsec_jumps"]
            sec_band = facts["sec_band"]
            route_ctx.update(jumps=jumps, sec_band=sec_band, lowsec_jumps=lowsec_jumps, resolved=True)
        except RouteUnavailable as exc:
            if not jumps:
                return None, route_ctx, str(exc)
    elif have_location and not jumps:
        msg = _("Couldn't resolve one of those locations — pick from the list or enter jumps manually.")
        return None, route_ctx, msg

    if not jumps:
        return None, route_ctx, _("Pick a route or enter a manual jump count.")

    q = price_quote(
        card, ship_class=ship_class, jumps=jumps, lowsec_jumps=lowsec_jumps,
        volume_m3=volume, collateral=collateral, sec_band=sec_band, rush=rush,
    )
    route_ctx.update(jumps=jumps, sec_band=sec_band, lowsec_jumps=lowsec_jumps)
    return q, route_ctx, ("" if q.ok else q.error)


def calculator(request: HttpRequest) -> HttpResponse:
    """Freight rate calculator. Visible per the leadership-set audience."""
    if not can_access(request.user):
        return render(request, "logistics/unavailable.html", {
            "audience": current_audience(),
            "authenticated": request.user.is_authenticated,
        }, status=403)

    card = active_rate_card()
    form = QuoteForm(request.POST or None)
    quote = route_ctx = error = None
    if request.method == "POST" and form.is_valid():
        quote, route_ctx, error = _build_quote(form.cleaned_data, card)

    can_post = request.user.is_authenticated and rbac.has_role(request.user, rbac.ROLE_MEMBER)
    return render(request, "logistics/calculator.html", {
        "form": form, "quote": quote, "route": route_ctx, "error": error, "card": card,
        "ship_classes": ShipClass.choices,
        "can_post": can_post,
        # The picker live-searches structures only if the pilot granted the scope;
        # surface a one-click opt-in when they haven't.
        "can_search_structures": can_post and has_structure_search(request.user),
        "post_as_self": poster_identity(request.user, "character") if can_post else None,
        "post_as_corp": poster_identity(request.user, "corporation") if can_post else None,
        "outstanding_count": CourierContract.objects.filter(
            status=CourierContract.Status.OUTSTANDING
        ).count(),
    })


def system_search(request: HttpRequest) -> JsonResponse:
    """Autocomplete for the route pickers ([{type_id,name}] shape)."""
    if not can_access(request.user):
        return JsonResponse([], safe=False, status=403)
    return JsonResponse(search_systems(request.GET.get("q", ""), limit=15), safe=False)


def location_search(request: HttpRequest) -> JsonResponse:
    """Freight location autocomplete: structures (ESI) + stations + systems."""
    if not can_access(request.user):
        return JsonResponse([], safe=False, status=403)
    results = search_locations(request.user, request.GET.get("q", ""), limit=12)
    return JsonResponse(results, safe=False)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def post_contract(request: HttpRequest) -> HttpResponse:
    """Re-price the submitted quote and post it as an outstanding contract."""
    card = active_rate_card()
    if not can_access(request.user):
        messages.error(request, _("The freight service isn't available to you right now."))
        return redirect("logistics:calculator")
    form = QuoteForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Check the quote details and try again."))
        return redirect("logistics:calculator")
    quote, route_ctx, error = _build_quote(form.cleaned_data, card)
    if not quote or not quote.ok:
        messages.error(request, error or _("Couldn't price that job."))
        return redirect("logistics:calculator")
    # A postable contract needs concrete pickup and drop-off points (so the
    # hauler knows the exact docks), not just a manual jump count.
    origin = route_ctx.get("origin")
    dest = route_ctx.get("dest")
    if not origin or not dest:
        messages.error(request, _("Pick a pickup and a drop-off location before posting."))
        return redirect("logistics:calculator")

    post_as = request.POST.get("post_as", "character")
    if post_as not in ("character", "corporation"):
        post_as = "character"
    identity = poster_identity(request.user, post_as)

    contract = create_contract_from_quote(
        quote=quote, card=card,
        origin=origin, dest=dest,
        ship_class=form.cleaned_data["ship_class"],
        volume_m3=form.cleaned_data["volume_m3"],
        collateral=form.cleaned_data["collateral"] or 0,
        rush=form.cleaned_data.get("rush", False),
        posted_as_kind=identity["kind"],
        posted_as_id=identity["id"],
        posted_as_name=identity["name"],
        created_by=request.user,
    )
    audit_log(request.user, "courier.post", target_type="courier_contract",
              target_id=str(contract.id), ip=client_ip(request))
    messages.success(request, _("Contract posted to the freight board."))
    return redirect("logistics:contracts")


@login_required
@role_required(rbac.ROLE_MEMBER)
def contracts(request: HttpRequest) -> HttpResponse:
    """The freight board: jobs to claim, plus the pilot's active and past hauls."""
    from apps.sso.models import EveCharacter

    my_char_ids = set(
        EveCharacter.objects.filter(user=request.user).values_list("character_id", flat=True)
    )
    outstanding = CourierContract.objects.filter(
        status=CourierContract.Status.OUTSTANDING
    ).order_by("-rush", "-created_at")
    mine = CourierContract.objects.filter(
        assigned_user=request.user,
        status__in=[CourierContract.Status.IN_PROGRESS],
    ).order_by("deadline")
    recent = CourierContract.objects.filter(
        status__in=[CourierContract.Status.DELIVERED, CourierContract.Status.FAILED]
    ).order_by("-updated_at")[:15]

    return render(request, "logistics/contracts.html", {
        "outstanding": outstanding,
        "mine": mine,
        "recent": recent,
        "my_char_ids": my_char_ids,
        "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
        "earned": sum(
            (c.reward for c in CourierContract.objects.filter(
                assigned_user=request.user, status=CourierContract.Status.DELIVERED
            )),
            start=0,
        ),
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def claim_contract(request: HttpRequest, pk: int) -> HttpResponse:

    contract = get_object_or_404(CourierContract, pk=pk)
    main = pilots.acting_pilot(request.user)  # LP-3: the pilot the user is FLYING, not the account's main.
    if not main:
        messages.error(request, _("Link an EVE character before claiming hauls."))
        return redirect("logistics:contracts")
    # Atomic claim: only one hauler can win the OUTSTANDING→IN_PROGRESS transition.
    # LOG-1 (3.2): give the hauler a fresh delivery window from now (and clear any prior
    # reminder), so a re-claimed haul isn't instantly past-deadline and re-swept.
    from datetime import timedelta

    from django.utils import timezone

    from .services import active_rate_card
    new_deadline = timezone.now() + timedelta(days=active_rate_card().contract_days)
    claimed = CourierContract.objects.filter(
        pk=pk, status=CourierContract.Status.OUTSTANDING
    ).update(
        status=CourierContract.Status.IN_PROGRESS,
        assigned_user=request.user,
        assigned_hauler_character_id=main.character_id,
        deadline=new_deadline,
        reminder_sent_at=None,
    )
    if not claimed:
        messages.error(request, _("That contract is no longer available."))
        return redirect("logistics:contracts")
    audit_log(request.user, "courier.claim", target_type="courier_contract",
              target_id=str(contract.id), ip=client_ip(request))
    issuer = contract.posted_as_name or _("the customer")
    messages.success(
        request,
        _("Haul claimed — in EVE, find and accept the courier contract from %(issuer)s for this route, then fly safe.")
        % {"issuer": issuer},
    )
    return redirect("logistics:contracts")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def transition_contract(request: HttpRequest, pk: int) -> HttpResponse:
    """Hauler advances their job: delivered / failed / release back to the pool."""
    contract = get_object_or_404(CourierContract, pk=pk)
    is_owner = contract.assigned_user_id == request.user.id
    if not (is_owner or rbac.has_role(request.user, rbac.ROLE_OFFICER)):
        messages.error(request, _("That isn't your haul."))
        return redirect("logistics:contracts")
    action = request.POST.get("action", "")
    # These transitions are only valid on a contract that is actually in progress;
    # a delivered/failed/cancelled contract can't be re-driven.
    if contract.status != CourierContract.Status.IN_PROGRESS:
        messages.error(request, _("That contract isn't in progress."))
        return redirect("logistics:contracts")
    if action == "delivered":
        contract.status = CourierContract.Status.DELIVERED
    elif action == "failed":
        contract.status = CourierContract.Status.FAILED
    elif action == "release":
        contract.status = CourierContract.Status.OUTSTANDING
        contract.assigned_user = None
        contract.assigned_hauler_character_id = None
    else:
        messages.error(request, _("Unknown action."))
        return redirect("logistics:contracts")
    contract.save(update_fields=["status", "assigned_user", "assigned_hauler_character_id"])
    # Credit the hauler for a completed run (idempotent per contract). 'delivered'
    # can't be re-driven, so this never double-counts; 'release' nulls the user
    # above, so the guard below only fires for a real delivery. When the weights
    # require ESI verification, the self-report logs the haul at 0 points
    # (provisional) and the reconcile task upgrades it to full points once CCP
    # confirms the contract finished in-game.
    if action == "delivered" and contract.assigned_user_id:
        from apps.pilots.models import ContributionEvent
        from apps.pilots.services import record_contribution
        from apps.pilots.weights import active_weights, points_for

        provisional = active_weights().haul_requires_verification
        record_contribution(
            contract.assigned_user, ContributionEvent.Kind.HAUL,
            magnitude=contract.volume_m3, unit="m³",
            description=f"{contract.origin_name} → {contract.dest_name}",
            ref_type="courier_contract", ref_id=str(contract.pk),
            points=0 if provisional else points_for("haul"),
        )
    audit_log(request.user, f"courier.{action}", target_type="courier_contract",
              target_id=str(contract.id), ip=client_ip(request))
    messages.success(request, _("Contract updated."))
    return redirect("logistics:contracts")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def cancel_contract(request: HttpRequest, pk: int) -> HttpResponse:
    contract = get_object_or_404(CourierContract, pk=pk)
    if contract.status in (CourierContract.Status.DELIVERED, CourierContract.Status.CANCELLED):
        messages.error(request, _("That contract can't be cancelled."))
        return redirect("logistics:contracts")
    contract.status = CourierContract.Status.CANCELLED
    contract.save(update_fields=["status"])
    audit_log(request.user, "courier.cancel", target_type="courier_contract",
              target_id=str(contract.id), ip=client_ip(request))
    messages.success(request, _("Contract cancelled."))
    return redirect("logistics:contracts")


@login_required
@role_required(rbac.ROLE_OFFICER)
def rates(request: HttpRequest) -> HttpResponse:
    """Officer rate-card management — the one place margins are tuned."""
    card = active_rate_card()
    if request.method == "POST":
        form = RateCardForm(request.POST, instance=card)
        if form.is_valid():
            form.save()
            invalidate_audience_cache()
            audit_log(request.user, "courier.rates_update", target_type="rate_card",
                      target_id=str(card.id), ip=client_ip(request))
            messages.success(request, _("Rate card updated."))
            return redirect("logistics:rates")
    else:
        form = RateCardForm(instance=card)
    from apps.admin_audit.models import AppSetting
    return render(request, "logistics/rates.html", {
        "form": form, "card": card,
        "benchmark": AppSetting.get("logistics.market_benchmark"),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def corp_contracts(request: HttpRequest) -> HttpResponse:
    """Officer oversight of all corp contracts (item exchange, courier, auction…)."""
    from .corp_contracts import sync_corp_contracts
    from .models import CorpContract

    if request.method == "POST":
        result = sync_corp_contracts()
        if result["status"] == "ok":
            messages.success(request, _("Synced %(count)d corp contract(s).") % {"count": result["count"]})
        elif result["status"] == "no_token":
            messages.warning(request, _("No director has granted the corp-contracts scope yet."))
        else:
            messages.error(request, _("Contract sync failed; try again later."))
        return redirect("logistics:corp_contracts")

    rows = list(CorpContract.objects.all())
    return render(request, "logistics/corp_contracts.html", {
        "contracts": rows,
        "open_count": sum(1 for c in rows if c.is_open),
    })
