"""Officer console for Shipyard inventory & fulfilment policy (SHIP-1 part 3).

Everything here is officer-only, audited, and served by the same authoritative
availability service as the buyer-facing Shipyard — the console never derives
its own stock math.
"""
from __future__ import annotations

import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max, Sum
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.doctrines.models import Doctrine, DoctrineFit
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from . import inventory as inv
from .availability import OfferState, availability_for_fits, manifest_hash
from .forms import FitOfferForm, ShipyardPolicyForm, StockAdjustForm, StockReceiptForm
from .models import (
    FitOffer,
    FitReservation,
    FitStock,
    FitSupplyNeed,
    FitWaitlistEntry,
    ShipyardPolicy,
    StoreOrder,
)
from .services import notify_stock_allocated
from .supply import (
    create_build_job_for_need,
    create_industry_project_for_need,
    create_task_for_need,
    notify_waitlist,
    recompute_supply_need,
    waiting_orders,
)

_STATE_RANK = {
    OfferState.READY: 0, OfferState.LIMITED: 1, OfferState.BACKORDER: 2,
    OfferState.UNAVAILABLE: 3, OfferState.NOT_OFFERED: 4,
}


def _paginate(request: HttpRequest, items: list, per_page: int = 50):
    page_obj = Paginator(items, per_page).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return page_obj, params.urlencode()


def _inventory_rows(policy: ShipyardPolicy) -> list[dict]:
    """One row per active doctrine fit: availability + planning data + alerts.

    Constant query count: availability is the batched service; names, losses,
    backorder/waitlist counts and stale flags are one grouped query each."""
    from apps.sde.models import SdeType

    from .forecast import recent_losses

    fits = list(
        DoctrineFit.objects.filter(doctrine__status=Doctrine.Status.ACTIVE)
        .select_related("doctrine").order_by("doctrine__name", "name")
    )
    avail = availability_for_fits(fits, policy=policy)
    names = dict(
        SdeType.objects.filter(type_id__in={f.ship_type_id for f in fits})
        .values_list("type_id", "name")
    )
    losses = recent_losses(30)
    backordered = {
        row["doctrine_fit"]: row["s"]
        for row in StoreOrder.objects.filter(
            kind=StoreOrder.Kind.DOCTRINE_FIT,
            status__in=(StoreOrder.Status.OPEN, StoreOrder.Status.CLAIMED,
                        StoreOrder.Status.IN_PRODUCTION),
            quantity_backordered__gt=0, doctrine_fit__isnull=False,
        ).values("doctrine_fit").annotate(s=Sum("quantity_backordered"))
    }
    waitlisted = {
        row["fit"]: row["n"]
        for row in FitWaitlistEntry.objects.values("fit").annotate(n=Count("id"))
    }
    last_recon = {
        row["doctrine_fit"]: row["m"]
        for row in FitStock.objects.values("doctrine_fit").annotate(m=Max("last_reconciled_at"))
    }

    rows = []
    for fit in fits:
        a = avail[fit.id]
        offer = a.offer
        loss_rate = losses.get(fit.ship_type_id, 0) / 30.0  # est. losses/day (30d killboard)
        days_cover = round(a.atp / loss_rate, 1) if loss_rate > 0 and a.atp > 0 else None
        reorder = offer.reorder_point if offer else None
        target = offer.target_stock if offer else None
        safety = offer.safety_stock if offer else 0
        alerts = []
        if a.stale_on_hand:
            alerts.append("stale")
        if reorder is not None and a.atp <= reorder:
            alerts.append("reorder")
        if safety and a.atp < safety:
            alerts.append("safety")
        if a.state in (OfferState.READY, OfferState.LIMITED) and a.location is None and a.on_hand:
            alerts.append("no_location")
        rows.append({
            "fit": fit,
            "ship_name": names.get(fit.ship_type_id, fit.name),
            "a": a,
            "offer": offer,
            "backordered": backordered.get(fit.id, 0),
            "waitlisted": waitlisted.get(fit.id, 0),
            "days_cover": days_cover,
            "reorder_point": reorder,
            "target_stock": target,
            "safety_stock": safety,
            "priority": offer.priority if offer else 0,
            "last_reconciled": last_recon.get(fit.id),
            "alerts": alerts,
        })
    return rows


@login_required
@role_required(rbac.ROLE_OFFICER)
def inventory(request: HttpRequest) -> HttpResponse:
    """The Shipyard inventory console: every offer's stock position at a glance."""
    policy = ShipyardPolicy.active()
    rows = _inventory_rows(policy)

    q = (request.GET.get("q") or "").strip().lower()
    f_state = (request.GET.get("state") or "").strip()
    f_doctrine = (request.GET.get("doctrine") or "").strip()
    f_alert = (request.GET.get("alert") or "").strip()
    sort = request.GET.get("sort", "doctrine")

    if q:
        rows = [r for r in rows
                if q in r["fit"].name.lower() or q in r["ship_name"].lower()
                or q in r["fit"].doctrine.name.lower()]
    if f_state:
        rows = [r for r in rows if r["a"].state == f_state]
    if f_doctrine:
        rows = [r for r in rows if str(r["fit"].doctrine_id) == f_doctrine]
    if f_alert:
        rows = [r for r in rows if f_alert in r["alerts"]]

    if sort == "atp":
        rows.sort(key=lambda r: (-r["a"].atp, r["fit"].name.lower()))
    elif sort == "state":
        rows.sort(key=lambda r: (_STATE_RANK.get(r["a"].state, 9), r["fit"].name.lower()))
    elif sort == "priority":
        rows.sort(key=lambda r: (-r["priority"], r["fit"].name.lower()))
    elif sort == "cover":
        rows.sort(key=lambda r: (r["days_cover"] if r["days_cover"] is not None else 10**6,
                                 r["fit"].name.lower()))
    # default: doctrine/name order from the query

    if request.GET.get("export") == "csv":
        return _export_csv(rows)

    doctrines = sorted({(r["fit"].doctrine_id, r["fit"].doctrine.name) for r in rows},
                       key=lambda t: t[1].lower())
    page_obj, base_qs = _paginate(request, rows)
    needs = list(
        FitSupplyNeed.objects.filter(
            status__in=(FitSupplyNeed.Status.OPEN, FitSupplyNeed.Status.IN_PROGRESS)
        ).select_related("doctrine_fit", "location", "industry_project", "build_job", "task")
        .order_by("required_by", "-quantity_required")[:50]
    )
    return render(request, "store/inventory.html", {
        "rows": page_obj.object_list, "page_obj": page_obj, "base_qs": base_qs,
        "total": len(rows), "policy": policy, "needs": needs,
        "doctrines": doctrines,
        "q": q, "f_state": f_state, "f_doctrine": f_doctrine, "f_alert": f_alert,
        "sort": sort, "states": OfferState.choices,
    })


def _export_csv(rows: list[dict]) -> HttpResponse:
    """The filtered console table as CSV (column keys stay machine-stable English)."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="shipyard-inventory.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "doctrine", "fit", "ship", "state", "location", "on_hand", "reserved",
        "atp", "stale", "incoming", "backordered", "waitlisted", "safety_stock",
        "reorder_point", "target_stock", "lead_days", "days_cover", "priority",
        "last_reconciled",
    ])
    for r in rows:
        a = r["a"]
        writer.writerow([
            r["fit"].doctrine.name, r["fit"].name, r["ship_name"], a.state,
            str(a.location) if a.location else "", a.on_hand, a.reserved, a.atp,
            a.stale_on_hand, a.incoming, r["backordered"], r["waitlisted"],
            r["safety_stock"], r["reorder_point"] or "", r["target_stock"] or "",
            a.lead_days, r["days_cover"] if r["days_cover"] is not None else "",
            r["priority"],
            r["last_reconciled"].isoformat() if r["last_reconciled"] else "",
        ])
    return response


@login_required
@role_required(rbac.ROLE_OFFICER)
def inventory_fit(request: HttpRequest, fit_id: int) -> HttpResponse:
    """Per-fit console: offer overrides, stock rows, ledger, orders, supply need."""
    fit = get_object_or_404(
        DoctrineFit.objects.select_related("doctrine"), pk=fit_id
    )
    policy = ShipyardPolicy.active()
    offer = FitOffer.objects.filter(fit=fit).first()

    if request.method == "POST":
        form = FitOfferForm(request.POST, instance=offer)
        if form.is_valid():
            saved = form.save(commit=False)
            saved.fit = fit
            saved.updated_by = request.user
            saved.save()
            audit_log(request.user, "store.offer_update", target_type="doctrine_fit",
                      target_id=str(fit.pk), metadata={"offered": saved.is_offered},
                      ip=client_ip(request))
            messages.success(request, _("Offer settings saved."))
            return redirect("store:inventory_fit", fit_id=fit.pk)
    else:
        form = FitOfferForm(instance=offer)

    avail = availability_for_fits([fit], policy=policy)[fit.id]
    current_hash = manifest_hash(fit)
    stocks = list(
        FitStock.objects.filter(doctrine_fit=fit).select_related("location").order_by("id")
    )
    reserved_by_stock = {
        row["stock_id"]: row["s"]
        for row in FitReservation.objects.filter(
            stock__in=stocks, status=FitReservation.Status.ACTIVE
        ).values("stock_id").annotate(s=Sum("quantity"))
    }
    stock_rows = [{
        "stock": s,
        "reserved": reserved_by_stock.get(s.pk, 0),
        "is_current": s.manifest_hash == current_hash,
    } for s in stocks]
    from .models import FitStockEntry

    entries = list(
        FitStockEntry.objects.filter(stock__doctrine_fit=fit)
        .select_related("stock__location", "actor", "order").order_by("-created_at")[:50]
    )
    need = FitSupplyNeed.objects.filter(
        doctrine_fit=fit,
        status__in=(FitSupplyNeed.Status.OPEN, FitSupplyNeed.Status.IN_PROGRESS),
    ).select_related("industry_project", "build_job", "task", "location").first()
    orders = list(
        StoreOrder.objects.filter(
            kind=StoreOrder.Kind.DOCTRINE_FIT, doctrine_fit=fit,
        ).exclude(status__in=(StoreOrder.Status.DELIVERED, StoreOrder.Status.CANCELLED))
        .select_related("buyer").order_by("created_at")
    )

    return render(request, "store/inventory_fit.html", {
        "fit": fit, "offer": offer, "form": form, "a": avail, "policy": policy,
        "stock_rows": stock_rows, "entries": entries, "need": need, "orders": orders,
        "need_orders": waiting_orders(need) if need else [],
        "receipt_form": StockReceiptForm(initial={
            "location": avail.location.pk if avail.location else None,
        }),
        "adjust_form": StockAdjustForm(),
        "esi": _esi_reconciliation(fit, avail.location),
        "waitlist_count": FitWaitlistEntry.objects.filter(fit=fit).count(),
    })


def _esi_reconciliation(fit, location) -> dict | None:
    """Advisory ESI cross-check: corp hulls seen at the location vs fitted claims.

    The corp asset mirror aggregates by type — it cannot see fittings — so it can
    only bound the claim (you cannot have more complete fitted ships than hulls).
    Clearly labelled advisory; never authoritative, never auto-applied."""
    if location is None:
        return None
    from django.conf import settings

    from apps.stockpile.models import Asset
    from apps.stockpile.services import _asset_location_ids_for

    loc_ids = _asset_location_ids_for(location)
    if not loc_ids:
        return {"covered": False}
    agg = Asset.objects.filter(
        owner_type=Asset.Owner.CORPORATION,
        owner_id=settings.FORCA_HOME_CORP_ID,
        location_id__in=loc_ids, type_id=fit.ship_type_id,
    ).aggregate(q=Sum("quantity"), latest=Max("as_of"))
    return {
        "covered": True,
        "esi_hulls": agg["q"] or 0,
        "last_sync": agg["latest"],
    }


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def inventory_receipt(request: HttpRequest, fit_id: int) -> HttpResponse:
    """Record assembled complete ships arriving; auto-allocates to backorders."""
    fit = get_object_or_404(DoctrineFit, pk=fit_id)
    form = StockReceiptForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Pick a location and a positive quantity."))
        return redirect("store:inventory_fit", fit_id=fit.pk)
    result = inv.receive_stock(
        fit, location=form.cleaned_data["location"],
        quantity=form.cleaned_data["quantity"], actor=request.user,
        reason=form.cleaned_data.get("reason", ""),
    )
    audit_log(request.user, "store.inventory_receipt", target_type="doctrine_fit",
              target_id=str(fit.pk),
              metadata={"quantity": form.cleaned_data["quantity"],
                        "location": form.cleaned_data["location"].pk,
                        "allocated": [(a.order.pk, a.quantity) for a in result.allocations]},
              ip=client_ip(request))
    for allocation in result.allocations:
        notify_stock_allocated(allocation.order, allocation.quantity)
    recompute_supply_need(fit, location=form.cleaned_data["location"])
    policy = ShipyardPolicy.active()
    after = availability_for_fits([fit], policy=policy)[fit.id]
    if after.can_order and policy.waitlist_enabled:
        notify_waitlist(fit)
    if result.allocations:
        messages.success(request, _(
            "%(qty)s received — %(n)s backordered order(s) got stock reserved."
        ) % {"qty": form.cleaned_data["quantity"], "n": len(result.allocations)})
    else:
        messages.success(request, _("%(qty)s received into stock.") % {
            "qty": form.cleaned_data["quantity"]})
    return redirect("store:inventory_fit", fit_id=fit.pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def inventory_adjust(request: HttpRequest, stock_id: int) -> HttpResponse:
    """Stocktake correction of one stock row (reason required, fully audited)."""
    stock = get_object_or_404(FitStock.objects.select_related("doctrine_fit"), pk=stock_id)
    form = StockAdjustForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("A corrected balance and a reason are required."))
        return redirect("store:inventory_fit", fit_id=stock.doctrine_fit_id)
    try:
        entry = inv.adjust_stock(
            stock, corrected_balance=form.cleaned_data["corrected_balance"],
            actor=request.user, reason=form.cleaned_data["reason"],
        )
    except ValueError as exc:
        if str(exc) == "reserved":
            messages.error(request, _(
                "That balance is below the actively reserved quantity — release or "
                "deliver the reservations first."
            ))
        else:
            messages.error(request, _("Invalid adjustment."))
        return redirect("store:inventory_fit", fit_id=stock.doctrine_fit_id)
    audit_log(request.user, "store.inventory_adjust", target_type="fit_stock",
              target_id=str(stock.pk),
              metadata={"balance": form.cleaned_data["corrected_balance"],
                        "delta": entry.delta if entry else 0,
                        "reason": form.cleaned_data["reason"][:100]},
              ip=client_ip(request))
    messages.success(request, _("Stock adjusted.") if entry else _("No change — balance already matched."))
    return redirect("store:inventory_fit", fit_id=stock.doctrine_fit_id)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def inventory_revalidate(request: HttpRequest, stock_id: int) -> HttpResponse:
    """Confirm stranded (pre-revision) ships as matching the current fit."""
    stock = get_object_or_404(FitStock.objects.select_related("doctrine_fit"), pk=stock_id)
    moved = inv.revalidate_stock(
        stock, actor=request.user, reason=request.POST.get("reason", "")[:300]
    )
    audit_log(request.user, "store.inventory_revalidate", target_type="fit_stock",
              target_id=str(stock.pk), metadata={"moved": moved}, ip=client_ip(request))
    if moved:
        messages.success(request, _(
            "%(n)s ship(s) revalidated against the current fit revision."
        ) % {"n": moved})
    else:
        messages.info(request, _("Nothing to revalidate on that row."))
    return redirect("store:inventory_fit", fit_id=stock.doctrine_fit_id)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def inventory_bulk(request: HttpRequest) -> HttpResponse:
    """Bulk enable/disable offers for the selected fits."""
    action = request.POST.get("action", "")
    fit_ids = [int(v) for v in request.POST.getlist("fit_ids") if str(v).isdigit()]
    if action not in ("enable", "disable") or not fit_ids:
        messages.error(request, _("Select at least one fit and a bulk action."))
        return redirect("store:inventory")
    fits = list(DoctrineFit.objects.filter(pk__in=fit_ids))
    for fit in fits:
        offer, _created = FitOffer.objects.get_or_create(fit=fit)
        offer.is_offered = action == "enable"
        offer.updated_by = request.user
        offer.save(update_fields=["is_offered", "updated_by", "updated_at"])
    audit_log(request.user, "store.inventory_bulk", target_type="doctrine_fit",
              target_id=",".join(str(f.pk) for f in fits),
              metadata={"action": action, "count": len(fits)}, ip=client_ip(request))
    if action == "enable":
        messages.success(request, _("%(n)s fit(s) are now offered for sale.") % {"n": len(fits)})
    else:
        messages.success(request, _("%(n)s fit(s) withdrawn from sale.") % {"n": len(fits)})
    return redirect("store:inventory")


@login_required
@role_required(rbac.ROLE_OFFICER)
def shipyard_policy(request: HttpRequest) -> HttpResponse:
    """Corp-wide Shipyard fulfilment policy (the per-fit pages override it)."""
    policy = ShipyardPolicy.active()
    if request.method == "POST":
        form = ShipyardPolicyForm(request.POST, instance=policy)
        if form.is_valid():
            form.save()
            audit_log(request.user, "store.policy_update", target_type="shipyard_policy",
                      target_id=str(policy.pk), ip=client_ip(request))
            messages.success(request, _("Shipyard policy updated."))
            return redirect("store:shipyard_policy")
    else:
        form = ShipyardPolicyForm(instance=policy)
    return render(request, "store/shipyard_policy.html", {"form": form, "policy": policy})


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def supply_action(request: HttpRequest, need_id: int) -> HttpResponse:
    """Attach a production vehicle to a supply need (idempotent per vehicle)."""
    need = get_object_or_404(
        FitSupplyNeed.objects.select_related("doctrine_fit"), pk=need_id
    )
    action = request.POST.get("action", "")
    if need.status not in (FitSupplyNeed.Status.OPEN, FitSupplyNeed.Status.IN_PROGRESS):
        messages.error(request, _("That supply need is closed."))
        return redirect("store:inventory_fit", fit_id=need.doctrine_fit_id)
    if action == "project":
        project = create_industry_project_for_need(need, actor=request.user)
        messages.success(request, _("Industry project “%(name)s” is linked.") % {
            "name": project.name})
    elif action == "build_job":
        create_build_job_for_need(need, actor=request.user)
        messages.success(request, _("ERP build job queued and linked."))
    elif action == "task":
        create_task_for_need(need, actor=request.user)
        messages.success(request, _("Claimable task created and linked."))
    else:
        messages.error(request, _("Unknown action."))
        return redirect("store:inventory_fit", fit_id=need.doctrine_fit_id)
    audit_log(request.user, "store.supply_vehicle", target_type="fit_supply_need",
              target_id=str(need.pk), metadata={"action": action}, ip=client_ip(request))
    return redirect("store:inventory_fit", fit_id=need.doctrine_fit_id)
