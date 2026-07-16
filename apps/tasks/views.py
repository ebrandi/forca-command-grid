"""Task board: member view (mine + claimable) and officer management."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import rbac
from core.rbac import role_required

from . import services
from .models import Task


@login_required
@role_required(rbac.ROLE_MEMBER)
def board(request: HttpRequest) -> HttpResponse:
    """Everyone sees their own tasks and the open pool; officers also see all."""
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    mine = Task.objects.filter(assignee=request.user).exclude(
        status__in=[Task.Status.DONE, Task.Status.CANCELLED]
    )
    claimable = Task.objects.filter(
        is_open=True, assignee__isnull=True, status=Task.Status.OPEN
    )
    ctx = {
        "mine": mine,
        "claimable": claimable,
        "is_officer": is_officer,
        "types": Task.Type.choices,
    }
    if is_officer:
        from apps.identity.models import User

        status = request.GET.get("status") or ""
        all_tasks = Task.objects.all()
        if status in Task.Status.values:
            all_tasks = all_tasks.filter(status=status)
        ctx["all_tasks"] = all_tasks.select_related("assignee")[:200]
        ctx["filter_status"] = status
        ctx["statuses"] = Task.Status.choices
        ctx["members"] = (
            User.objects.filter(characters__is_corp_member=True)
            .distinct()
            .order_by("username")
        )
    return render(request, "tasks/board.html", ctx)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def create(request: HttpRequest) -> HttpResponse:
    title = (request.POST.get("title") or "").strip()
    if not title:
        messages.error(request, _("A task needs a title."))
        return redirect("tasks:board")
    ttype = request.POST.get("type")
    if ttype not in Task.Type.values:
        ttype = Task.Type.OTHER
    assignee = None
    assignee_id = request.POST.get("assignee_id")
    if assignee_id:
        from apps.identity.models import User

        assignee = User.objects.filter(pk=assignee_id).first()
    try:
        priority = int(request.POST.get("priority") or 0)
    except ValueError:
        priority = 0
    task = Task.objects.create(
        type=ttype,
        title=title,
        description=(request.POST.get("description") or "").strip(),
        priority=priority,
        assignee=assignee,
        is_open=assignee is None,
        status=Task.Status.CLAIMED if assignee else Task.Status.OPEN,
        created_by=request.user,
        related_type=(request.POST.get("related_type") or "").strip(),
        related_id=(request.POST.get("related_id") or "").strip(),
    )
    messages.success(request, _("Task created: %(title)s") % {"title": task.title})
    return redirect("tasks:board")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def claim(request: HttpRequest, pk: int) -> HttpResponse:
    task = get_object_or_404(Task, pk=pk)
    if services.claim(task, request.user):
        messages.success(request, _("Claimed: %(title)s") % {"title": task.title})
    else:
        messages.error(request, _("That task is no longer available to claim."))
    return redirect("tasks:board")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def update_status(request: HttpRequest, pk: int) -> HttpResponse:
    task = get_object_or_404(Task, pk=pk)
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    if not services.can_act(request.user, task, is_officer=is_officer):
        messages.error(request, _("You can only update your own tasks."))
        return redirect("tasks:board")
    to_status = request.POST.get("status")
    # Members may not cancel; that's an officer action.
    if to_status == Task.Status.CANCELLED and not is_officer:
        messages.error(request, _("Only officers can cancel tasks."))
        return redirect("tasks:board")
    if services.set_status(task, request.user, to_status):
        messages.success(request, _("Updated: %(title)s") % {"title": task.title})
    else:
        messages.error(request, _("That status change isn't allowed."))
    nxt = request.POST.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect("tasks:board")


def _assignable_members():
    from apps.sso.models import EveCharacter

    return [
        {"user_id": c.user_id, "name": c.name}
        for c in EveCharacter.objects.filter(
            is_main=True, is_corp_member=True, user__isnull=False
        ).order_by("name")
    ]


def _is_corp_member(user) -> bool:
    from apps.sso.models import EveCharacter

    return EveCharacter.objects.filter(user=user, is_corp_member=True).exists()


def _parse_dt(raw):
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    raw = (raw or "").strip()
    if not raw:
        return None
    dt = parse_datetime(raw)
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _clamp_priority(raw, default: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(-2_000_000_000, min(2_000_000_000, v))  # keep inside 32-bit IntegerField


def _can_edit(user, task, *, is_officer: bool) -> bool:
    return is_officer or task.created_by_id == user.id or task.assignee_id == user.id


def _can_view(user, task, *, is_officer: bool) -> bool:
    """Who may open a task's DETAIL (title, description, actor/history).

    Mirrors the board's non-officer visibility so list and detail cannot drift: an
    officer sees everything; a member sees a task they are assigned or created, or one
    that is currently in the claimable pool. Anything else is not theirs to read —
    without this a member could enumerate ``pk`` and read internal task notes/history
    of work they are not a party to (IDOR)."""
    if _can_edit(user, task, is_officer=is_officer):
        return True
    return bool(
        task.is_open and task.assignee_id is None and task.status == Task.Status.OPEN
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    """SDE-2 (3.7): task detail with history, edit/reassign and status actions."""
    task = get_object_or_404(
        Task.objects.select_related("assignee", "created_by"), pk=pk
    )
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    # Object-level read scope: 404 (not 403) for a task this member isn't a party to,
    # matching the no-existence-oracle convention used elsewhere (e.g. navigation routes).
    if not _can_view(request.user, task, is_officer=is_officer):
        raise Http404("No such task.")
    return render(request, "tasks/detail.html", {
        "task": task,
        "events": task.events.select_related("actor")[:50],
        "is_officer": is_officer,
        "can_edit": _can_edit(request.user, task, is_officer=is_officer),
        "statuses": Task.Status.choices,
        "allowed_next": services._ALLOWED_TRANSITIONS.get(task.status, set()),
        "assignable": _assignable_members() if is_officer else [],
    })


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def edit(request: HttpRequest, pk: int) -> HttpResponse:
    task = get_object_or_404(Task, pk=pk)
    is_officer = rbac.has_role(request.user, rbac.ROLE_OFFICER)
    if not _can_edit(request.user, task, is_officer=is_officer):
        messages.error(request, _("You can't edit this task."))
        return redirect("tasks:detail", pk=pk)
    title = (request.POST.get("title") or "").strip()
    if not title:
        messages.error(request, _("Title can't be empty."))
        return redirect("tasks:detail", pk=pk)
    # Only officers/creator may change priority (the board orders by -priority, so an
    # assignee shouldn't be able to bump their own task to the top).
    if is_officer or task.created_by_id == request.user.id:
        priority = _clamp_priority(request.POST.get("priority"), task.priority)
    else:
        priority = task.priority
    # Distinguish "clear the due date" (empty) from a malformed value (reject, don't silently
    # wipe an existing due date).
    due_raw = (request.POST.get("due_at") or "").strip()
    if due_raw:
        due_at = _parse_dt(due_raw)
        if due_at is None:
            messages.error(request, _("Couldn't read the due date."))
            return redirect("tasks:detail", pk=pk)
    else:
        due_at = None
    if services.edit_task(task, request.user, title=title, priority=priority, due_at=due_at):
        messages.success(request, _("Task updated."))
    return redirect("tasks:detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def reassign(request: HttpRequest, pk: int) -> HttpResponse:
    from django.contrib.auth import get_user_model

    task = get_object_or_404(Task, pk=pk)
    uid = request.POST.get("assignee")
    new_assignee = None
    if uid:
        new_assignee = get_user_model().objects.filter(pk=uid).first()
        if new_assignee is None or not _is_corp_member(new_assignee):
            messages.error(request, _("Pick a current corp member."))
            return redirect("tasks:detail", pk=pk)
    if services.reassign(task, request.user, new_assignee):
        messages.success(request, _("Task reassigned.") if new_assignee else _("Task unassigned."))
    return redirect("tasks:detail", pk=pk)
