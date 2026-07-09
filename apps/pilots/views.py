"""Pilot self-service: preferences and the personal contribution ledger."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .services import (
    contribution_nudge,
    get_prefs,
    monthly_points,
    monthly_summary,
    personal_trend,
    recent_for_user,
)


@login_required
@require_POST
def toggle_recognition(request: HttpRequest) -> HttpResponse:
    """Member opts in/out of corp-wide recognition of their contributions."""
    prefs = get_prefs(request.user)
    prefs.public_recognition = not prefs.public_recognition
    prefs.save(update_fields=["public_recognition", "updated_at"])
    # The Hall of Fame is cached; drop it so the opt-out takes effect immediately
    # rather than lingering for up to the cache TTL.
    from apps.pilots.halloffame import invalidate_cache
    invalidate_cache()
    from core.redirects import safe_next
    return redirect(safe_next(request, request.POST.get("next"), "identity:privacy"))


@login_required
@require_POST
def toggle_idle_queue_nudge(request: HttpRequest) -> HttpResponse:
    """Member opts in/out of a DM when a character's skill queue runs dry (SKL-4)."""
    prefs = get_prefs(request.user)
    prefs.notify_idle_queue = not prefs.notify_idle_queue
    prefs.save(update_fields=["notify_idle_queue", "updated_at"])
    from core.redirects import safe_next
    return redirect(safe_next(request, request.POST.get("next"), "identity:privacy"))


@login_required
def briefing(request: HttpRequest) -> HttpResponse:
    """Absorbed into the Command Center (/dashboard/) — redirect old bookmarks.

    Keeps the union off-switch semantics the merged Daily Briefing had: 404
    when all three of its old section features are disabled.
    """
    from django.http import Http404

    from core.features import feature_enabled

    if not (feature_enabled("briefing") or feature_enabled("command_intel_pilot")
            or feature_enabled("recommendations")):
        raise Http404("This feature is not enabled for this corporation.")
    return redirect("identity:dashboard")


@login_required
def hall_of_fame(request: HttpRequest) -> HttpResponse:
    """Corp Hall of Fame: top contributors overall + per category, by month."""
    from core import rbac

    from .halloffame import available_months, scoreboard

    if not rbac.has_role(request.user, rbac.ROLE_MEMBER):
        return render(request, "doctrines/forbidden.html", status=403)

    months = available_months()
    selected = request.GET.get("m", "")
    chosen = next((m for m in months if m["key"] == selected), months[0])
    board = scoreboard(chosen["year"], chosen["month"])
    return render(request, "pilots/hall_of_fame.html", {
        "board": board, "months": months, "chosen": chosen,
    })


@login_required
def contributions(request: HttpRequest) -> HttpResponse:
    """The member's own contribution history and this-month totals."""
    from core.features import feature_enabled

    # The nudge surfaces CI constraint labels, so gate it on the same pilot-facing CI feature
    # the directive surface uses — if leadership disabled that surface, no nudge.
    nudge = contribution_nudge(request.user) if feature_enabled("command_intel_pilot") else None
    return render(
        request,
        "pilots/contributions.html",
        {
            "summary": monthly_summary(request.user),
            "points_total": monthly_points(request.user),
            "events": recent_for_user(request.user, limit=50),
            "prefs": get_prefs(request.user),
            "trend": personal_trend(request.user),
            "nudge": nudge,  # PCC-3 (3.11)
        },
    )
