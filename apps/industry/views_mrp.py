"""The Material Plan (P3) — officer views over MRP net requirements.

Everything here is officer-only and audited. The page never computes its own
numbers: it renders :class:`NetRequirement` rows the planning run wrote, with
every number decomposable into its demand/supply provenance.
"""
from __future__ import annotations

import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from . import mrp
from .models import MrpConfig, MrpRun, NetRequirement

_LIVE = (NetRequirement.Status.OPEN, NetRequirement.Status.IN_PROGRESS)


def _ship_level(row: NetRequirement) -> bool:
    """Ship-level rows (fit/need demand) are the Shipyard console's to fulfil —
    MRP never mints ship vehicles (§3.4 boundary)."""
    return any(
        s.get("kind") in ("fit_demand", "supply_need") for s in (row.sources or [])
    )


def _rows_context(rows: list[NetRequirement]) -> list[dict]:
    from apps.doctrines.models import DoctrineFit
    from apps.sde.models import SdeType
    from apps.store.models import FitSupplyNeed

    type_ids = {r.type_id for r in rows}
    names = dict(SdeType.objects.filter(type_id__in=type_ids).values_list("type_id", "name"))
    fit_ids = {
        s.get("id") for r in rows for s in (r.sources or []) if s.get("kind") == "fit_demand"
    }
    fit_names = dict(DoctrineFit.objects.filter(pk__in=fit_ids).values_list("pk", "name"))
    need_ids = {
        s.get("id") for r in rows for s in (r.sources or []) if s.get("kind") == "supply_need"
    }
    need_fits = dict(
        FitSupplyNeed.objects.filter(pk__in=need_ids).values_list("pk", "doctrine_fit_id")
    )
    out = []
    for row in rows:
        out.append({
            "row": row,
            "name": names.get(row.type_id, str(row.type_id)),
            "ship_level": _ship_level(row),
            "suggestion_label": mrp.suggestion_label(row.suggestion),
            "feasible_label": mrp.FEASIBLE_SOURCE_LABELS.get(
                row.feasible_source, row.feasible_source
            ),
            "fit_links": [
                {"id": s["id"], "name": fit_names.get(s["id"], s["id"]), "qty": s["qty"]}
                for s in (row.sources or []) if s.get("kind") == "fit_demand"
            ],
            "need_links": [
                {"id": s["id"], "fit_id": need_fits.get(s["id"]), "qty": s["qty"]}
                for s in (row.sources or []) if s.get("kind") == "supply_need"
            ],
            "parent_links": [s for s in (row.sources or []) if s.get("kind") == "parent"],
            "vehicle_sources": [s for s in (row.sources or []) if s.get("kind") == "vehicle"],
        })
    return out


@login_required
@role_required(rbac.ROLE_OFFICER)
def mrp_board(request: HttpRequest) -> HttpResponse:
    """The Material Plan: live requirements by depth, provenance, fan-out, runs."""
    config = MrpConfig.active()
    rows = list(
        NetRequirement.objects.filter(status__in=_LIVE)
        .select_related("location", "industry_project", "build_job", "hauling_task", "task")
        .order_by("depth", "type_id")
    )
    if request.GET.get("export") == "csv":
        return _export_csv(rows)

    last_runs = list(MrpRun.objects.order_by("-started_at")[:10])
    last_done = next((r for r in last_runs if r.status == MrpRun.Status.DONE), None)
    ctx_rows = _rows_context(rows)

    # One copy-paste multibuy block over every open buy/import requirement
    # (the jobs.html/prep.py precedent — nothing reads ShoppingList).
    multibuy = "\n".join(
        f"{c['name']} x{c['row'].net_quantity}"
        for c in ctx_rows
        if c["row"].suggestion in ("buy", "import") and c["row"].net_quantity > 0
        and not c["ship_level"]
    )
    from apps.market.pricing import price_for

    unpriced = sorted({
        c["name"] for c in ctx_rows
        if c["row"].suggestion in ("buy", "import") and c["row"].net_quantity > 0
        and price_for(c["row"].type_id) == 0
    })
    return render(request, "industry/mrp.html", {
        "rows": ctx_rows,
        "config": config,
        "runs": last_runs,
        "last_done": last_done,
        "beyond_window": (last_done.stats or {}).get("beyond_window", []) if last_done else [],
        "multibuy": multibuy,
        "unpriced": unpriced,
        "no_changes": bool(
            last_done and len(last_runs) > 1
            and [r for r in last_runs if r.status == MrpRun.Status.DONE][1:]
            and [r for r in last_runs if r.status == MrpRun.Status.DONE][1].inputs_digest
            == last_done.inputs_digest
        ),
    })


def _export_csv(rows) -> HttpResponse:
    """The plan as CSV (keys machine-stable English, the house convention)."""
    from apps.sde.models import SdeType

    names = dict(
        SdeType.objects.filter(type_id__in={r.type_id for r in rows})
        .values_list("type_id", "name")
    )
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="material-plan.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "type_id", "type_name", "location", "status", "depth", "gross", "available",
        "incoming", "net", "required_by", "feasible_at", "feasible_source",
        "suggestion", "diverged",
    ])
    for r in rows:
        writer.writerow([
            r.type_id, names.get(r.type_id, ""), str(r.location) if r.location else "",
            r.status, r.depth, r.gross_quantity, r.available_quantity,
            r.incoming_quantity, r.net_quantity,
            r.required_by.isoformat() if r.required_by else "",
            r.feasible_at.isoformat() if r.feasible_at else "",
            r.feasible_source, r.suggestion, r.diverged,
        ])
    return response


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mrp_run_now(request: HttpRequest) -> HttpResponse:
    """Manual planning trigger — the v1 workflow (the beat ships inert)."""
    try:
        run = mrp.run_mrp(actor=request.user)
    except mrp.MrpAlreadyRunning:
        messages.info(request, _("A planning run is already in progress — try again shortly."))
        return redirect("industry:mrp")
    audit_log(request.user, "industry.mrp_run", target_type="mrp_run",
              target_id=str(run.pk), metadata=dict(run.stats or {}, digest=run.inputs_digest[:12]),
              ip=client_ip(request))
    stats = run.stats or {}
    if not stats.get("rows_written") and not stats.get("rows_closed"):
        messages.info(request, _("Planning run finished — no changes since the last run."))
    else:
        messages.success(request, _(
            "Planning run finished: %(written)s row(s) updated, %(closed)s closed."
        ) % {"written": stats.get("rows_written", 0), "closed": stats.get("rows_closed", 0)})
    return redirect("industry:mrp")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mrp_fan_out(request: HttpRequest, pk: int) -> HttpResponse:
    """Attach a vehicle to a requirement (idempotent per vehicle FK)."""
    requirement = get_object_or_404(NetRequirement, pk=pk)
    if requirement.status not in _LIVE:
        messages.error(request, _("That requirement is closed."))
        return redirect("industry:mrp")
    if _ship_level(requirement):
        messages.error(request, _(
            "Ship-level demand is fulfilled from the Shipyard inventory console."
        ))
        return redirect("industry:mrp")
    action = request.POST.get("action", "")
    if action == "project":
        vehicle = mrp.create_project_for_requirement(requirement, actor=request.user)
        messages.success(request, _("Industry plan “%(name)s” is linked.") % {"name": vehicle.name})
    elif action == "build_job":
        mrp.create_build_job_for_requirement(requirement, actor=request.user)
        messages.success(request, _("ERP build job queued and linked."))
    elif action == "haul":
        mrp.create_hauling_task_for_requirement(requirement, actor=request.user)
        messages.success(request, _("Hauling job posted and linked."))
    elif action == "buy_task":
        mrp.create_buy_task_for_requirement(requirement, actor=request.user)
        messages.success(request, _("Claimable BUY task created and linked."))
    else:
        messages.error(request, _("Unknown action."))
        return redirect("industry:mrp")
    audit_log(request.user, "industry.mrp_fan_out", target_type="net_requirement",
              target_id=str(requirement.pk), metadata={"action": action},
              ip=client_ip(request))
    return redirect("industry:mrp")
