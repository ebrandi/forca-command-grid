"""ERP board: build jobs (claim/build/deliver), blueprints, coverage."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import rbac
from core.rbac import role_required

from . import services
from .models import Blueprint, BuildJob


@login_required
@role_required(rbac.ROLE_MEMBER)
def board(request: HttpRequest) -> HttpResponse:
    # Consolidation: the production board now lives inside the Industry Center's Job
    # Tracker. Redirect unless leadership has turned the redirect off (config).
    from apps.industry.models import IndustryEconomyConfig

    if IndustryEconomyConfig.active().erp_redirects:
        return redirect("industry:jobs")

    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    queued = list(
        BuildJob.objects.filter(
            status__in=[BuildJob.Status.QUEUED, BuildJob.Status.BLOCKED], owner__isnull=True
        )
    )
    for j in queued:
        services.recheck_block(j)  # keep blocked/queued truthful as corp stock changes
    mine = BuildJob.objects.filter(owner=request.user).exclude(
        status__in=[BuildJob.Status.DELIVERED, BuildJob.Status.CANCELLED]
    )
    ctx = {
        "queued": [{"job": j, "mats": services.job_materials(j)} for j in queued],
        "mine": [{"job": j, "mats": services.job_materials(j)} for j in mine],
        "is_officer": is_officer,
    }
    if is_officer:
        ctx["coverage"] = services.blueprint_coverage()
        ctx["blueprints"] = Blueprint.objects.all()[:100]
        ctx["all_jobs"] = BuildJob.objects.select_related("owner")[:100]
        ctx["in_production"] = services.in_production()
    return render(request, "erp/board.html", ctx)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def create_job(request: HttpRequest) -> HttpResponse:
    try:
        output_type_id = int(request.POST.get("output_type_id"))
        quantity = max(1, int(request.POST.get("quantity") or 1))
    except (TypeError, ValueError):
        messages.error(request, _("Need a valid type and quantity."))
        return redirect("erp:board")
    job = BuildJob.objects.create(
        output_type_id=output_type_id, quantity=quantity,
        note=(request.POST.get("note") or "").strip(), created_by=request.user,
    )
    services.recheck_block(job)  # flag immediately if corp stock can't cover it
    messages.success(
        request,
        _("Build job queued: %(qty)s× %(type_id)s.") % {"qty": job.quantity, "type_id": job.output_type_id},
    )
    return redirect("erp:board")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def claim(request: HttpRequest, pk: int) -> HttpResponse:
    job = get_object_or_404(BuildJob, pk=pk)
    services.recheck_block(job)  # unblock if stock has since arrived (or re-block)
    if job.status == BuildJob.Status.BLOCKED:
        messages.error(
            request, _("Can't claim — %(reason)s.") % {"reason": job.blocked_reason or _("materials are short")}
        )
    elif services.claim(job, request.user):
        messages.success(request, _("Claimed — materials and BOM are on the card."))
    else:
        messages.error(request, _("That job is no longer available."))
    return redirect("erp:board")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def update_status(request: HttpRequest, pk: int) -> HttpResponse:
    job = get_object_or_404(BuildJob, pk=pk)
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    if not services.can_act(request.user, job, is_officer=is_officer):
        messages.error(request, _("You can only update your own jobs."))
        return redirect("erp:board")
    to = request.POST.get("status")
    if to == BuildJob.Status.CANCELLED and not is_officer:
        messages.error(request, _("Only officers can cancel jobs."))
        return redirect("erp:board")
    services.set_status(job, to)
    return redirect("erp:board")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def deliver(request: HttpRequest, pk: int) -> HttpResponse:
    job = get_object_or_404(BuildJob, pk=pk)
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    if not services.can_act(request.user, job, is_officer=is_officer):
        messages.error(request, _("You can only deliver your own jobs."))
        return redirect("erp:board")
    if services.deliver(job, request.user):
        messages.success(request, _("Delivered — corp stock updated and you've been credited."))
    else:
        messages.info(request, _("Already delivered."))
    return redirect("erp:board")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def cancel_job(request: HttpRequest, pk: int) -> HttpResponse:
    """Cancel/dismiss a job. Allowed for an officer, the builder, or — while the job
    is still unclaimed — the pilot who created it."""
    job = get_object_or_404(BuildJob, pk=pk)
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    if not services.can_manage(request.user, job, is_officer=is_officer):
        messages.error(request, _("You can't manage that job."))
    elif job.status in (BuildJob.Status.DELIVERED, BuildJob.Status.CANCELLED):
        messages.info(request, _("That job is already closed."))
    else:
        services.set_status(job, BuildJob.Status.CANCELLED)
        messages.success(request, _("Job cancelled."))
    return redirect("industry:jobs")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def edit_job(request: HttpRequest, pk: int) -> HttpResponse:
    """Edit an unclaimed job's quantity/note (creator or officer)."""
    job = get_object_or_404(BuildJob, pk=pk)
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    if not services.can_manage(request.user, job, is_officer=is_officer):
        messages.error(request, _("You can't manage that job."))
        return redirect("industry:jobs")
    if job.status not in (BuildJob.Status.QUEUED, BuildJob.Status.BLOCKED):
        messages.error(request, _("Only a queued job can be edited."))
        return redirect("industry:jobs")
    try:
        quantity = max(1, int(request.POST.get("quantity") or job.quantity))
    except (TypeError, ValueError):
        messages.error(request, _("Enter a valid quantity."))
        return redirect("industry:jobs")
    if services.update_quantity(job, quantity, request.POST.get("note", "")):
        messages.success(request, _("Job updated."))
    else:
        messages.error(request, _("That job can no longer be edited — it was just claimed."))
    return redirect("industry:jobs")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def add_blueprint(request: HttpRequest) -> HttpResponse:
    try:
        type_id = int(request.POST.get("type_id"))
    except (TypeError, ValueError):
        messages.error(request, _("Need a valid blueprint type id."))
        return redirect("erp:board")
    product = request.POST.get("product_type_id")
    Blueprint.objects.create(
        owner_type=Blueprint.Owner.CORPORATION, type_id=type_id,
        product_type_id=int(product) if product else None,
        me=int(request.POST.get("me") or 0), te=int(request.POST.get("te") or 0),
        source="manual",
    )
    messages.success(request, _("Blueprint recorded."))
    return redirect("erp:board")
