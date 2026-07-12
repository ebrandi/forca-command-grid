"""Industry views: project board, detail, and member-driven project management."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.sde.models import SdeType
from apps.sde.search import search_types
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from .forms import ProjectForm, ProjectItemForm
from .models import IndustryProject, IndustryProjectItem
from .services import (
    compute_project_bom,
    detect_bottlenecks,
    generate_shopping_list,
    project_economics,
    project_reservation_summary,
    release_project_stock,
    reserve_project_stock,
)


def _can_manage(user, project: IndustryProject) -> bool:
    """Officers, the creator, or the current assignee may edit a project."""
    return (
        rbac.has_role(user, rbac.ROLE_OFFICER)
        or project.created_by_id == user.id
        or project.assigned_to_id == user.id
    )


def _can_see(user, project: IndustryProject) -> bool:
    """Visibility gate for a single plan (corp = everyone, else owner/leadership)."""
    if project.visibility == IndustryProject.Visibility.CORP:
        return True
    if rbac.has_role(user, rbac.ROLE_OFFICER):
        return True
    return project.created_by_id == user.id or project.assigned_to_id == user.id


def _visible_projects(user):
    """Base queryset: non-archived plans this user is allowed to see."""
    qs = IndustryProject.objects.filter(is_archived=False).select_related("assigned_to")
    if rbac.has_role(user, rbac.ROLE_OFFICER):
        return qs
    # Members see corp-visible plans plus their own private/leadership ones.
    from django.db.models import Q
    return qs.filter(
        Q(visibility=IndustryProject.Visibility.CORP)
        | Q(created_by=user)
        | Q(assigned_to=user)
    ).distinct()


@login_required
@role_required(rbac.ROLE_MEMBER)
def project_board(request: HttpRequest) -> HttpResponse:
    show_archived = request.GET.get("archived") == "1" and rbac.has_role(request.user, rbac.ROLE_OFFICER)
    if show_archived:
        projects = IndustryProject.objects.filter(is_archived=True).select_related("assigned_to")
    else:
        projects = _visible_projects(request.user)
    projects = projects.order_by("-updated_at")
    mine = projects.filter(assigned_to=request.user)
    return render(request, "industry/board.html", {
        "projects": projects, "mine": mine, "show_archived": show_archived,
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def project_duplicate(request: HttpRequest, pk: int) -> HttpResponse:
    """Clone a plan (name + items) into a fresh draft owned by the current pilot."""
    source = get_object_or_404(IndustryProject, pk=pk)
    if not _can_see(request.user, source):
        raise PermissionDenied(_("You can't see that plan."))
    clone = IndustryProject.objects.create(
        name=f"Copy of {source.name}"[:200], description=source.description,
        objective_type=source.objective_type, status=IndustryProject.Status.DRAFT,
        visibility=source.visibility, linked_doctrine=source.linked_doctrine,
        created_by=request.user, assigned_to=request.user, source=source.source,
    )
    for it in source.items.all():
        IndustryProjectItem.objects.create(
            project=clone, type_id=it.type_id, product_name=it.product_name,
            quantity=it.quantity, build_or_buy=it.build_or_buy, strategy=it.strategy,
            blueprint_source=it.blueprint_source, max_depth=it.max_depth, runs=it.runs,
            me=it.me, te=it.te, invent_decryptor_type_id=it.invent_decryptor_type_id,
            invent_science_1=it.invent_science_1, invent_science_2=it.invent_science_2,
            invent_encryption=it.invent_encryption,
        )
    compute_project_bom(clone)
    audit_log(request.user, "industry.project.duplicate", target_type="industry_project",
              target_id=str(clone.id), metadata={"from": source.id}, ip=client_ip(request))
    messages.success(request, _("Duplicated into “%(name)s”.") % {"name": clone.name})
    return redirect("industry:detail", pk=clone.pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def project_archive(request: HttpRequest, pk: int) -> HttpResponse:
    """Soft-delete: archive a plan (recoverable) — never a hard delete by default."""
    project = get_object_or_404(IndustryProject, pk=pk)
    if not _can_manage(request.user, project):
        raise PermissionDenied(_("Not your project to manage."))
    unarchive = request.POST.get("unarchive") == "1"
    project.is_archived = not unarchive
    project.archived_at = None if unarchive else timezone.now()
    project.save(update_fields=["is_archived", "archived_at"])
    audit_log(request.user, "industry.project.archive", target_type="industry_project",
              target_id=str(project.id), metadata={"archived": project.is_archived},
              ip=client_ip(request))
    if unarchive:
        messages.success(request, _("Plan restored."))
        return redirect("industry:detail", pk=pk)
    messages.success(request, _("Plan archived."))
    return redirect("industry:board")


@login_required
@role_required(rbac.ROLE_MEMBER)
def type_search(request: HttpRequest) -> JsonResponse:
    """Autocomplete endpoint for the item picker (members only)."""
    rows = search_types(
        request.GET.get("q", ""),
        limit=20,
        buildable_only=request.GET.get("buildable") == "1",
    )
    return JsonResponse(rows, safe=False)


@login_required
@role_required(rbac.ROLE_MEMBER)
def project_create(request: HttpRequest) -> HttpResponse:
    """Any member can propose/start a build project with one initial item."""
    if request.method == "POST":
        form = ProjectForm(request.POST)
        item_form = ProjectItemForm(request.POST)
        if form.is_valid() and item_form.is_valid():
            type_id = item_form.cleaned_data["type_id"]
            if not SdeType.objects.filter(type_id=type_id).exists():
                item_form.add_error(None, _("Pick an item from the search list."))
            else:
                project = form.save(commit=False)
                project.created_by = request.user
                project.assigned_to = request.user
                project.status = IndustryProject.Status.ACTIVE
                project.save()
                item = item_form.save(commit=False)
                item.project = project
                item.save()
                compute_project_bom(project)
                audit_log(
                    request.user, "industry.project.create",
                    target_type="industry_project", target_id=str(project.id),
                    metadata={"name": project.name, "type_id": type_id}, ip=client_ip(request),
                )
                messages.success(request, _("Project “%(name)s” created and costed.") % {"name": project.name})
                return redirect("industry:detail", pk=project.pk)
    else:
        form = ProjectForm()
        item_form = ProjectItemForm(initial={"quantity": 1})
    return render(request, "industry/create.html", {"form": form, "item_form": item_form})


@login_required
@role_required(rbac.ROLE_MEMBER)
def project_detail(request: HttpRequest, pk: int) -> HttpResponse:
    project = get_object_or_404(
        IndustryProject.objects.select_related("assigned_to").prefetch_related(
            "items__material_requirements", "items__production_steps"
        ),
        pk=pk,
    )
    if not _can_see(request.user, project):
        raise PermissionDenied(_("You can't see that plan."))
    return render(
        request,
        "industry/detail.html",
        {
            "project": project,
            "bottlenecks": detect_bottlenecks(project),
            "economics": project_economics(project),
            "reservation": project_reservation_summary(project),
            "can_manage": _can_manage(request.user, project),
            "is_mine": project.assigned_to_id == request.user.id,
            "item_form": ProjectItemForm(initial={"quantity": 1}),
            "statuses": IndustryProject.Status.choices,
        },
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def project_claim(request: HttpRequest, pk: int) -> HttpResponse:
    project = get_object_or_404(IndustryProject, pk=pk)
    # Only claim a project you're allowed to see: claiming assigns it to you, which grants
    # _can_see/_can_manage — so without this an unassigned PRIVATE/LEADERSHIP project could
    # be claimed by any member to gain read+manage on it.
    if not _can_see(request.user, project):
        raise PermissionDenied(_("You can't claim this project."))
    # Don't let one member yank an active lead from another; officers may reassign.
    if (
        project.assigned_to_id
        and project.assigned_to_id != request.user.id
        and not rbac.has_role(request.user, rbac.ROLE_OFFICER)
    ):
        raise PermissionDenied(_("This project already has a lead."))
    project.assigned_to = request.user
    if project.status == IndustryProject.Status.DRAFT:
        project.status = IndustryProject.Status.ACTIVE
    project.save(update_fields=["assigned_to", "status"])
    audit_log(request.user, "industry.project.claim", target_type="industry_project",
              target_id=str(project.id), ip=client_ip(request))
    messages.success(request, _("You’re now leading this project."))
    return redirect("industry:detail", pk=pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def project_push_jobs(request: HttpRequest, pk: int) -> HttpResponse:
    """IND-1 (3.3): push this plan's buildable lines to the job board as claimable jobs."""
    from .jobs_bridge import push_project_to_jobs

    project = get_object_or_404(IndustryProject, pk=pk)
    if not _can_manage(request.user, project):
        raise PermissionDenied("You can't push this plan to the job board.")
    if project.is_archived or project.status in (
        IndustryProject.Status.DONE, IndustryProject.Status.CANCELLED
    ):
        messages.info(request, _("This plan is closed — reopen it before pushing jobs."))
        return redirect("industry:detail", pk=pk)
    created = push_project_to_jobs(project, request.user)
    audit_log(request.user, "industry.project.push_jobs", target_type="industry_project",
              target_id=str(project.id), metadata={"created": created}, ip=client_ip(request))
    if created:
        messages.success(request, _("Pushed %(count)d build job(s) to the job board.") % {"count": created})
    else:
        messages.info(request, _("Nothing to push — every buildable line already has a job."))
    return redirect("industry:detail", pk=pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def project_status(request: HttpRequest, pk: int) -> HttpResponse:
    project = get_object_or_404(IndustryProject, pk=pk)
    if not _can_manage(request.user, project):
        raise PermissionDenied(_("Not your project to manage."))
    new_status = request.POST.get("status", "")
    if new_status not in IndustryProject.Status.values:
        raise PermissionDenied(_("Invalid status."))
    project.status = new_status
    project.save(update_fields=["status"])
    audit_log(request.user, "industry.project.status", target_type="industry_project",
              target_id=str(project.id), metadata={"status": new_status}, ip=client_ip(request))
    messages.success(request, _("Project marked %(status)s.") % {"status": project.get_status_display()})
    return redirect("industry:detail", pk=pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def add_item(request: HttpRequest, pk: int) -> HttpResponse:
    project = get_object_or_404(IndustryProject, pk=pk)
    if not _can_manage(request.user, project):
        raise PermissionDenied(_("Not your project to manage."))
    item_form = ProjectItemForm(request.POST)
    if item_form.is_valid() and SdeType.objects.filter(type_id=item_form.cleaned_data["type_id"]).exists():
        item = item_form.save(commit=False)
        item.project = project
        item.save()
        compute_project_bom(project)
        messages.success(request, _("Item added and BOM recomputed."))
    else:
        messages.error(request, _("Could not add item — pick one from the search list."))
    return redirect("industry:detail", pk=pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def remove_item(request: HttpRequest, pk: int, item_id: int) -> HttpResponse:
    project = get_object_or_404(IndustryProject, pk=pk)
    if not _can_manage(request.user, project):
        raise PermissionDenied(_("Not your project to manage."))
    get_object_or_404(IndustryProjectItem, pk=item_id, project=project).delete()
    compute_project_bom(project)
    messages.success(request, _("Item removed."))
    return redirect("industry:detail", pk=pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def recompute_bom(request: HttpRequest, pk: int) -> HttpResponse:
    project = get_object_or_404(IndustryProject, pk=pk)
    if not _can_manage(request.user, project):
        raise PermissionDenied(_("Not your project to manage."))
    compute_project_bom(project)
    messages.success(request, _("BOM recomputed."))
    return redirect("industry:detail", pk=pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def make_shopping_list(request: HttpRequest, pk: int) -> HttpResponse:
    project = get_object_or_404(IndustryProject, pk=pk)
    if not _can_manage(request.user, project):
        raise PermissionDenied(_("Not your project to manage."))
    generate_shopping_list(project)
    messages.success(request, _("Shopping list generated."))
    return redirect("industry:detail", pk=pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def reserve_stock(request: HttpRequest, pk: int) -> HttpResponse:
    project = get_object_or_404(IndustryProject, pk=pk)
    if not _can_manage(request.user, project):
        raise PermissionDenied(_("Not your project to manage."))
    result = reserve_project_stock(project)
    if result["units"]:
        audit_log(request.user, "industry.reserve_stock", target_type="industry_project",
                  target_id=str(project.id), metadata=result, ip=client_ip(request))
        messages.success(
            request,
            _("Reserved %(units)s units across %(types)s material(s) from corp stock.")
            % {"units": result["units"], "types": result["types"]},
        )
    else:
        messages.warning(request, _("Nothing reserved — no matching corp stock is available."))
    return redirect("industry:detail", pk=pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def release_stock(request: HttpRequest, pk: int) -> HttpResponse:
    project = get_object_or_404(IndustryProject, pk=pk)
    if not _can_manage(request.user, project):
        raise PermissionDenied(_("Not your project to manage."))
    n = release_project_stock(project)
    messages.success(request, _("Released %(n)s reservation(s) back to corp stock.") % {"n": n})
    return redirect("industry:detail", pk=pk)
