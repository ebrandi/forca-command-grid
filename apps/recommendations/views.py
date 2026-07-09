"""Recommendation views: officer command dashboard, personal recs, actions."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import client_ip
from core.rbac import role_required

from .models import Recommendation
from .services import act_on_recommendation, composite_score, set_action_links

_OPEN = [Recommendation.State.NEW, Recommendation.State.ACKNOWLEDGED]


def _ranked(recs) -> list[Recommendation]:
    """Materialise + sort recs by the composite score (REC-6), stamping each with
    a display ``rank``. created_at is the stable tiebreak (newest first)."""
    rows = list(recs)
    for rec in rows:
        rec.rank = round(composite_score(rec))
    rows.sort(key=lambda r: (composite_score(r), r.created_at), reverse=True)
    return rows


@login_required
@role_required(rbac.ROLE_OFFICER)
def officer_dashboard(request: HttpRequest) -> HttpResponse:
    from apps.industry.models import IndustryProject
    from apps.stockpile.models import HaulingTask

    recs = _ranked(
        Recommendation.objects.filter(
            state__in=_OPEN, required_permission__in=["officer", "director"]
        ).prefetch_related("action_items")
    )
    # Linkage targets (REC-5): active build projects + open haul tasks.
    projects = IndustryProject.objects.filter(status=IndustryProject.Status.ACTIVE).order_by("name")[:50]
    hauls = (
        HaulingTask.objects.filter(status=HaulingTask.Status.OPEN)
        .order_by("-created_at")[:50]
    )
    return render(request, "recommendations/officer.html", {
        "recs": recs, "projects": projects, "hauls": hauls,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def notifications(request: HttpRequest) -> HttpResponse:
    """Relayed in-game ESI notifications: structure attacks, wars, sov, moons."""
    from .models import CorpNotification
    from .notifications import INTERESTING

    rows = []
    for n in CorpNotification.objects.all()[:150]:
        label, alert = INTERESTING.get(n.type, (n.type, False))
        rows.append({"n": n, "label": label, "alert": alert})

    # REC-1 (2.10): surface which character is the authoritative relay source.
    from .relay import designated_relay_character_id

    relay_id = designated_relay_character_id()
    relay_name = None
    if relay_id:
        from apps.sso.models import EveCharacter

        relay_name = EveCharacter.objects.filter(character_id=relay_id).values_list(
            "name", flat=True
        ).first()

    return render(request, "recommendations/notifications.html", {
        "rows": rows,
        "is_director": rbac.has_role(request.user, rbac.ROLE_DIRECTOR),
        "relay_id": relay_id,
        "relay_name": relay_name or (relay_id and f"Character {relay_id}") or None,
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def notifications_sync(request: HttpRequest) -> HttpResponse:
    from core.audit import audit_log, client_ip

    from .notifications import sync_corp_notifications

    result = sync_corp_notifications()
    audit_log(request.user, "notifications.sync", target_type="corp", target_id="notifications",
              metadata={"status": result["status"]}, ip=client_ip(request))
    from django.contrib import messages
    if result["status"] == "ok":
        messages.success(request, f"Synced — {result['new']} new notification(s).")
    elif result["status"] == "no_token":
        messages.warning(request, "No character has granted the notifications scope yet.")
    else:
        messages.error(request, "Notification sync failed; try again later.")
    return redirect("recommendations:notifications")


@login_required
def personal_recs(request: HttpRequest) -> HttpResponse:
    """Absorbed into the Daily Briefing — redirect old /recommendations/mine/ bookmarks.

    The pick-up boards / onboarding / skill-plan sections moved there; the member
    Recommendation rows they showed (skill-training, newbro next-step) were pure
    duplicates of the quest log and the Getting-started section, so they have no
    member surface any more (the identity dashboard still lists them read-only).
    Stays namespace-gated by the ``recommendations`` key.
    """
    return redirect("identity:dashboard")


@login_required
@require_POST
def act(request: HttpRequest, pk: int) -> HttpResponse:
    rec = get_object_or_404(Recommendation, pk=pk)
    action = request.POST.get("action", "")

    # Deny by default: every recommendation must match an explicit allow branch.
    if rec.required_permission in ("officer", "director"):
        if not rbac.has_role(request.user, rbac.ROLE_OFFICER):
            raise PermissionDenied("Officer role required.")
    elif rec.required_permission == "member" and rec.subject_type == "character":
        if not request.user.characters.filter(character_id=rec.subject_id).exists():
            raise PermissionDenied("Not your recommendation.")
    else:
        raise PermissionDenied("Not permitted to act on this recommendation.")

    try:
        act_on_recommendation(rec, request.user, action, ip=client_ip(request))
    except ValueError as exc:
        raise PermissionDenied(str(exc)) from exc

    # Member-level recs are surfaced on the Command Center; officer recs keep
    # their command board.
    target = "recommendations:officer" if rec.required_permission != "member" else "identity:dashboard"
    return redirect(target)


@login_required
@require_POST
def link_action_item(request: HttpRequest, pk: int) -> HttpResponse:
    """REC-5: an officer links a recommendation's action item to a build project
    or open haul task. Targets are validated so the loose BigInteger linkage
    fields can't be set to a dangling/garbage id."""
    from apps.industry.models import IndustryProject
    from apps.stockpile.models import HaulingTask

    rec = get_object_or_404(Recommendation, pk=pk)
    if not rbac.has_role(request.user, rbac.ROLE_OFFICER):
        raise PermissionDenied("Officer role required.")

    def _valid_int(raw, queryset):
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if queryset.filter(pk=value).exists() else None

    project_id = _valid_int(
        request.POST.get("project_id"),
        IndustryProject.objects.filter(status=IndustryProject.Status.ACTIVE),
    )
    haul_task_id = _valid_int(
        request.POST.get("haul_task_id"),
        HaulingTask.objects.filter(status=HaulingTask.Status.OPEN),
    )
    set_action_links(rec, request.user, project_id=project_id, haul_task_id=haul_task_id, ip=client_ip(request))
    messages.success(request, "Recommendation linked.")
    return redirect("recommendations:officer")
