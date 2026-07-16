"""Stockpile & logistics views: dashboards plus member-driven actions.

Members can claim and run hauling jobs, post new hauls, and keep the corp's
manual stocktake current. Officers can create stockpiles.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.sde.models import SdeType
from core import pilots, rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from .forms import HaulingTaskForm, StockEntryForm, StockpileForm
from .models import Asset, HaulingTask, Stockpile
from .services import reconcile_stockpile, record_manual_stock, shortfalls_against_targets


def _character_ids(user) -> set[int]:
    return set(user.characters.values_list("character_id", flat=True))


def _main_character_id(user):
    """The pilot whose assets 'my assets' means — the ACTIVE one (LP-3)."""
    char = pilots.acting_pilot(user)
    return char.character_id if char else None


def _owns_haul(user, task: HaulingTask) -> bool:
    return (
        rbac.has_role(user, rbac.ROLE_OFFICER)
        or task.claimed_by_character_id in _character_ids(user)
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
def stockpile_dashboard(request: HttpRequest) -> HttpResponse:
    stockpiles = (
        Stockpile.objects.select_related("location").prefetch_related("items").order_by("name")
    )
    # CORP-1 (2.14): cross-check each stockpile's targets against live corp ESI on-hand.
    reconciled = [{"sp": s, "recon": reconcile_stockpile(s)} for s in stockpiles]
    return render(
        request,
        "stockpile/dashboard.html",
        {
            "reconciled": reconciled,
            "shortfalls": shortfalls_against_targets(),
            "stock_form": StockEntryForm(),
            "stockpile_form": StockpileForm() if rbac.has_role(request.user, rbac.ROLE_OFFICER) else None,
            "can_create_stockpile": rbac.has_role(request.user, rbac.ROLE_OFFICER),
        },
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def record_stock(request: HttpRequest) -> HttpResponse:
    form = StockEntryForm(request.POST)
    if form.is_valid() and SdeType.objects.filter(type_id=form.cleaned_data["type_id"]).exists():
        # A blank target means "leave the target alone" — a count-only update must
        # not wipe an existing target (set it to 0 to disable it explicitly).
        kwargs = {}
        if form.cleaned_data.get("quantity_target") is not None:
            kwargs["quantity_target"] = form.cleaned_data["quantity_target"]
        record_manual_stock(
            form.cleaned_data["stockpile"],
            form.cleaned_data["type_id"],
            quantity_current=form.cleaned_data["quantity_current"],
            **kwargs,
        )
        audit_log(request.user, "stock.manual_update", target_type="type",
                  target_id=str(form.cleaned_data["type_id"]), ip=client_ip(request))
        messages.success(request, _("Stock updated."))
    else:
        messages.error(request, _("Could not update stock — pick an item from the list."))
    return redirect("stockpile:dashboard")


@login_required
@role_required(rbac.ROLE_MEMBER)
def assets_view(request: HttpRequest) -> HttpResponse:
    """Live assets grouped by location with per-location ISK value.

    Two clearly-separated owners: a pilot's own assets ('mine') and the corp's
    assets ('corp', officers only). The owner is chosen by ?owner= so personal
    and corp holdings can never be confused.
    """
    from .assets import assets_summary

    can_view_corp = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    owner = request.GET.get("owner", "mine")
    if owner == "corp" and not can_view_corp:
        owner = "mine"

    main_id = _main_character_id(request.user)
    # The page renders only the per-location summary (cached); each location's items load on
    # demand when it is expanded (see asset_location_items), so a pilot/corp with thousands
    # of items no longer ships a multi-MB page or recomputes it on every load.
    if owner == "corp":
        from django.conf import settings
        data = assets_summary(Asset.Owner.CORPORATION, settings.FORCA_HOME_CORP_ID)
        scope_ok = True  # corp visibility is governed by the Director token at sync time
    else:
        data = (
            assets_summary(Asset.Owner.CHARACTER, main_id)
            if main_id else {"locations": [], "total_value": 0, "as_of": None}
        )
        scope_ok = bool(main_id)

    return render(
        request,
        "stockpile/assets.html",
        {
            "owner": owner,
            "data": data,
            "can_view_corp": can_view_corp,
            "can_sync_corp": rbac.has_role(request.user, rbac.ROLE_OFFICER),
            "main_character_id": main_id,
            "scope_ok": scope_ok,
        },
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_GET
def asset_location_items(request: HttpRequest) -> HttpResponse:
    """Item detail for ONE location — loaded on demand (htmx) when a pilot expands it.

    Owner scope is enforced here, not trusted from the request: corp items require officer,
    and personal items are always scoped to the requester's own main character, so a
    ``location`` id can never surface another pilot's holdings."""
    from .assets import location_items

    owner = request.GET.get("owner", "mine")
    try:
        location_id = int(request.GET.get("location") or 0)
    except (TypeError, ValueError):
        location_id = 0

    if owner == "corp" and rbac.has_role(request.user, rbac.ROLE_OFFICER):
        from django.conf import settings
        items = location_items(Asset.Owner.CORPORATION, settings.FORCA_HOME_CORP_ID, location_id)
    else:
        main_id = _main_character_id(request.user)
        items = location_items(Asset.Owner.CHARACTER, main_id, location_id) if main_id else []
    return render(request, "stockpile/_asset_items.html", {"items": items})


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def sync_corp_assets(request: HttpRequest) -> HttpResponse:
    """Pull corp assets via a Director token (graceful if not yet granted)."""
    from django.conf import settings

    from .assets import import_corporation_assets

    result = import_corporation_assets()
    if result["status"] == "ok":
        audit_log(request.user, "assets.sync", target_type="corporation",
                  target_id=str(settings.FORCA_HOME_CORP_ID), metadata={"types": result.get("types")},
                  ip=client_ip(request))
        messages.success(request, result["message"])
    elif result["status"] == "no_scope":
        messages.warning(request, result["message"] + _(" Use “Grant corp-asset access” on the ESI Scopes page."))
    else:
        messages.error(request, result["message"])
    return redirect(f"{reverse('stockpile:assets')}?owner=corp")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def sync_my_assets(request: HttpRequest) -> HttpResponse:
    """Pull the active pilot's personal assets via their own token."""
    from .assets import import_character_assets

    # The pilot you are flying, not the account's main (LP-3). The assets page shows the active
    # pilot's assets (via _main_character_id, which now resolves the active pilot); syncing must
    # pull for that same pilot — otherwise "Sync my assets" while flying an alt would import the
    # main's assets under the alt's view, spending the main's token.
    character = pilots.acting_pilot(request.user)
    if not character:
        messages.error(request, _("Link an EVE character first."))
        return redirect("stockpile:assets")
    result = import_character_assets(character)
    if result["status"] == "ok":
        messages.success(request, result["message"])
    elif result["status"] == "no_scope":
        messages.warning(request, result["message"])
    else:
        messages.error(request, result["message"])
    return redirect(f"{reverse('stockpile:assets')}?owner=mine")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def create_stockpile(request: HttpRequest) -> HttpResponse:
    form = StockpileForm(request.POST)
    if form.is_valid():
        form.save()
        messages.success(request, _("Stockpile created."))
    else:
        messages.error(request, _("Could not create stockpile."))
    return redirect("stockpile:dashboard")


@login_required
@role_required(rbac.ROLE_MEMBER)
def logistics_board(request: HttpRequest) -> HttpResponse:
    tasks = (
        HaulingTask.objects.exclude(status=HaulingTask.Status.DONE)
        .select_related("source_location", "dest_location")
        .order_by("status", "-created_at")
    )
    my_ids = _character_ids(request.user)
    return render(
        request,
        "stockpile/logistics.html",
        {
            "tasks": tasks,
            "my_character_ids": my_ids,
            "haul_form": HaulingTaskForm(),
            "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
        },
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def create_haul(request: HttpRequest) -> HttpResponse:
    form = HaulingTaskForm(request.POST)
    if form.is_valid() and SdeType.objects.filter(type_id=form.cleaned_data["type_id"]).exists():
        task = form.save(commit=False)
        sde = SdeType.objects.filter(type_id=task.type_id).first()
        task.volume_m3 = (sde.volume if sde else 0.0) * (task.quantity or 0)
        task.status = HaulingTask.Status.OPEN
        task.save()
        messages.success(request, _("Hauling job posted."))
    else:
        messages.error(request, _("Could not post job — pick an item and two locations."))
    return redirect("stockpile:logistics")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def claim_haul(request: HttpRequest, pk: int) -> HttpResponse:
    task = get_object_or_404(HaulingTask, pk=pk)
    if task.status != HaulingTask.Status.OPEN:
        raise PermissionDenied(_("This job is no longer open."))
    char_id = _main_character_id(request.user)
    if not char_id:
        messages.error(request, _("Link an EVE character before claiming jobs."))
        return redirect("stockpile:logistics")
    task.status = HaulingTask.Status.CLAIMED
    task.claimed_by_character_id = char_id
    task.save(update_fields=["status", "claimed_by_character_id"])
    audit_log(request.user, "haul.claim", target_type="hauling_task", target_id=str(task.id),
              ip=client_ip(request))
    messages.success(request, _("Job claimed — fly safe."))
    return redirect("stockpile:logistics")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def haul_transition(request: HttpRequest, pk: int) -> HttpResponse:
    """Advance a claimed job: start / complete / release back to the pool."""
    task = get_object_or_404(HaulingTask, pk=pk)
    if not _owns_haul(request.user, task):
        raise PermissionDenied(_("Not your job."))
    action = request.POST.get("action", "")
    if action == "start":
        task.status = HaulingTask.Status.IN_PROGRESS
    elif action == "done":
        task.status = HaulingTask.Status.DONE
    elif action == "release":
        task.status = HaulingTask.Status.OPEN
        task.claimed_by_character_id = None
    else:
        raise PermissionDenied(_("Unknown action."))
    task.save(update_fields=["status", "claimed_by_character_id"])
    audit_log(request.user, f"haul.{action}", target_type="hauling_task", target_id=str(task.id),
              ip=client_ip(request))
    messages.success(request, _("Job updated."))
    return redirect("stockpile:logistics")


@login_required
@role_required(rbac.ROLE_OFFICER)
def asset_search(request: HttpRequest) -> HttpResponse:
    """Corp-wide asset search: find where a type sits and who holds it."""
    from apps.corporation.models import EveName
    from apps.sde.models import SdeType

    q = (request.GET.get("q") or "").strip()
    rows: list[dict] = []
    truncated = False
    if len(q) >= 2:
        type_names = dict(
            SdeType.objects.filter(name__icontains=q).values_list("type_id", "name")[:80]
        )
        assets = list(
            Asset.objects.filter(type_id__in=type_names)
            .select_related("location")
            .order_by("type_id", "-quantity")[:501]
        )
        truncated = len(assets) > 500
        assets = assets[:500]
        char_ids = {a.owner_id for a in assets if a.owner_type == Asset.Owner.CHARACTER}
        names = dict(EveName.objects.filter(entity_id__in=char_ids).values_list("entity_id", "name"))
        for a in assets:
            owner = (_("Corp") if a.owner_type == Asset.Owner.CORPORATION
                     else names.get(a.owner_id, f"#{a.owner_id}"))
            rows.append({
                "type": type_names.get(
                    a.type_id, _("Type %(type_id)s") % {"type_id": a.type_id}
                ),
                "owner": owner,
                "is_corp": a.owner_type == Asset.Owner.CORPORATION,
                "location": str(a.location) if a.location else _("Unknown"),
                "quantity": a.quantity,
            })
    return render(request, "stockpile/asset_search.html",
                  {"q": q, "rows": rows, "truncated": truncated})
