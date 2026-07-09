"""Impersonation control surface: start (director-gated), stop, and the audit log."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import rbac
from core.rbac import role_required

from . import policy, services
from .models import ImpersonationSession

User = get_user_model()


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def start(request: HttpRequest, user_id: int) -> HttpResponse:
    """Begin viewing the site as another (strictly lower-ranked) pilot. Director/admin only."""
    # Never nest impersonations — exit the current one first.
    if request.session.get(policy.SESSION_TARGET_KEY) is not None:
        messages.error(request, "Exit your current view-as session before starting another.")
        return redirect("impersonation:log")
    target = get_object_or_404(User, pk=user_id)
    if not policy.can_impersonate(request.user, target):
        raise PermissionDenied("You can't view the site as that account.")
    services.begin(request, target, reason=(request.POST.get("reason") or "").strip())
    messages.success(
        request,
        f"You are now viewing the site as {target.display_name}. Everything is read-only — "
        "use “Exit” in the top banner to return to your own account.",
    )
    return redirect("/dashboard/")


@require_POST
def stop(request: HttpRequest) -> HttpResponse:
    """Exit impersonation and return to the director's own account.

    Gated only on there being an active impersonation: ``request.user`` is the *pilot* here
    (the middleware swapped it), so a role check would fail closed against the very director
    trying to get out. CSRF still applies (the token belongs to the director's session)."""
    if not getattr(request, "is_impersonating", False):
        return redirect("/dashboard/")
    services.end(request, reason="manual", actor=getattr(request, "impersonator", None))
    messages.success(request, "View-as ended — you're back on your own account.")
    return redirect("admin_audit:members")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def log(request: HttpRequest) -> HttpResponse:
    """Accountability view: currently-active + recent view-as sessions."""
    from django.utils import timezone

    cutoff = timezone.now() - policy.max_duration()
    active = list(
        ImpersonationSession.objects.filter(ended_at__isnull=True, started_at__gte=cutoff)
        .select_related("actor", "target")
    )
    # Open rows older than the cap were abandoned (director never returned / cookie died);
    # show them alongside the closed history rather than as misleadingly "active".
    recent = list(
        ImpersonationSession.objects.filter(started_at__lt=cutoff)
        .select_related("actor", "target")[:100]
    )
    for row in recent:
        row.abandoned = row.ended_at is None
    closed = list(
        ImpersonationSession.objects.filter(ended_at__isnull=False, started_at__gte=cutoff)
        .select_related("actor", "target")
    )
    history = sorted(recent + closed, key=lambda r: r.started_at, reverse=True)[:100]
    return render(request, "impersonation/log.html", {
        "active": active,
        "history": history,
        "max_minutes": int(policy.max_duration().total_seconds() // 60),
    })
