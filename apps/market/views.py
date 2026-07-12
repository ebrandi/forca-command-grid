"""Market views: stocking dashboard, margin finder, and location management."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.stockpile.services import shortfalls_against_targets
from core import rbac
from core.rbac import role_required

from .forms import MarketLocationForm
from .models import MarketHistory, MarketLocation, MarketPrice

THE_FORGE = 10000002  # Jita's region, the corp's price reference


@login_required
@role_required(rbac.ROLE_MEMBER)
def market_dashboard(request: HttpRequest) -> HttpResponse:
    from django.core.paginator import Paginator

    from .models import MarketWatch
    from .services import dashboard_signals, price_trends

    can_manage = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    # Officers see inactive locations too (so they can reactivate them); members only
    # see active ones. Attach a bound edit form per row for the inline edit modal.
    locations = list(
        MarketLocation.objects.all().order_by("name") if can_manage
        else MarketLocation.objects.filter(active=True).order_by("name")
    )
    if can_manage:
        for loc in locations:
            loc.edit_form = MarketLocationForm(instance=loc)

    # Item lookup: filter tracked prices by name (trigram-backed search_types), paginated.
    q = (request.GET.get("q") or "").strip()
    price_qs = MarketPrice.objects.select_related("location")
    if q:
        from apps.sde.search import search_types

        matched = [t["type_id"] for t in search_types(q, limit=200)]
        price_qs = price_qs.filter(type_id__in=matched) if matched else price_qs.none()
    page_obj = Paginator(price_qs.order_by("type_id"), 50).get_page(request.GET.get("page"))
    prices = list(page_obj.object_list)

    # Personal watchlist — the pilot's pinned items, shown at the top with their trend.
    watched_ids = set(
        MarketWatch.objects.filter(user=request.user).values_list("type_id", flat=True)
    )
    watch_prices = list(
        MarketPrice.objects.select_related("location")
        .filter(type_id__in=watched_ids).order_by("type_id")
    ) if watched_ids else []

    # One batched trend query for everything on screen (page rows + watchlist).
    trends = price_trends([p.type_id for p in prices + watch_prices], THE_FORGE, days=30)
    for p in prices + watch_prices:
        p.trend = trends.get(p.type_id)
        p.watched = p.type_id in watched_ids

    # Margin + build opportunities are expensive — served from a warmed cache.
    signals = dashboard_signals()
    return render(
        request,
        "market/dashboard.html",
        {
            "locations": locations,
            "prices": prices,
            "page_obj": page_obj,
            "q": q,
            "watch_prices": watch_prices,
            "needs": shortfalls_against_targets(),
            "margins": signals["margins"],
            "build_ops": signals["build_ops"],
            "has_history": MarketHistory.objects.exists(),
            "can_manage": can_manage,
            "location_form": MarketLocationForm() if can_manage else None,
        },
    )


_WATCHLIST_CAP = 200  # per-pilot ceiling — keeps one pilot's watchlist from bloating


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def toggle_watch(request: HttpRequest) -> HttpResponse:
    """Pin/unpin an item on the pilot's personal market watchlist."""
    from core.redirects import safe_next

    from .models import MarketWatch

    dest = safe_next(request, request.POST.get("next"), "market:dashboard")
    try:
        type_id = int(request.POST.get("type_id", ""))
    except (TypeError, ValueError):
        return redirect(dest)
    # Range-check to the int4 column bounds so an out-of-range value redirects rather
    # than escaping the parse guard and 500-ing on the INSERT.
    if not 0 < type_id < 2_147_483_648:
        return redirect(dest)

    existing = MarketWatch.objects.filter(user=request.user, type_id=type_id).first()
    if existing:
        existing.delete()  # toggle off
    elif MarketWatch.objects.filter(user=request.user).count() < _WATCHLIST_CAP:
        MarketWatch.objects.get_or_create(user=request.user, type_id=type_id)
    else:
        messages.warning(
            request, _("Your watchlist is full (%(cap)s items).") % {"cap": _WATCHLIST_CAP}
        )
    return redirect(dest)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def create_location(request: HttpRequest) -> HttpResponse:
    form = MarketLocationForm(request.POST)
    if form.is_valid():
        form.save()
        messages.success(request, _("Market location added."))
    else:
        messages.error(request, _("Could not add location — check the fields."))
    return redirect("market:dashboard")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def edit_location(request: HttpRequest, pk: int) -> HttpResponse:
    loc = get_object_or_404(MarketLocation, pk=pk)
    form = MarketLocationForm(request.POST, instance=loc)
    if form.is_valid():
        form.save()
        messages.success(request, _("Market location updated."))
    else:
        messages.error(request, _("Could not update location — check the fields."))
    return redirect("market:dashboard")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def toggle_location(request: HttpRequest, pk: int) -> HttpResponse:
    loc = get_object_or_404(MarketLocation, pk=pk)
    loc.active = not loc.active
    loc.save(update_fields=["active"])
    messages.success(
        request, _("Location activated.") if loc.active else _("Location deactivated.")
    )
    return redirect("market:dashboard")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def refresh_market(request: HttpRequest) -> HttpResponse:
    """Ingest public Jita history for the most-traded tracked types."""
    from .services import tracked_history_type_ids

    type_ids = tracked_history_type_ids(limit=80)
    if not type_ids:
        messages.warning(request, _("No tracked prices yet — run the price import first."))
        return redirect("market:dashboard")
    # Dispatch to the worker instead of running ~80 sequential ESI history calls inside the
    # request (which would pin a gunicorn thread for 30 s-2 min and, on a double-click, several).
    from .tasks import sync_market_history

    sync_market_history.delay(max_types=len(type_ids))
    messages.success(
        request,
        _("Market history refresh queued for %(count)s items — it'll update shortly.")
        % {"count": len(type_ids)},
    )
    return redirect("market:dashboard")
