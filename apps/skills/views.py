"""Skill-plan views: build a plan toward a doctrine, track it, export it."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

from apps.doctrines.models import Doctrine
from core import pilots, rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from .models import SkillPlan, SkillPlanStep
from .services import export_plan_text, generate_plan_for_doctrine, remaining_seconds


def _user_plans(user):
    return (
        SkillPlan.objects.filter(character__user=user)
        .select_related("character", "target_doctrine")
        .prefetch_related("steps")
        .order_by("-created_at")
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
def my_plans(request: HttpRequest) -> HttpResponse:
    """Skills & training is now consolidated onto the per-character page — send the
    member to their main character's page (the character switcher reaches the rest).
    The plan-management routes (detail/create/export) still live under ``/skills/``."""
    characters = list(request.user.characters.all())
    if not characters:
        return redirect("identity:dashboard")
    main = pilots.acting_pilot(request.user) or characters[0]
    return redirect("identity:character", character_id=main.character_id)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def import_mine(request: HttpRequest) -> HttpResponse:
    """Pull the member's skills + skill queue from ESI right now.

    Requires the skills scope on the character's token; if it's missing we point
    the member at re-authorising rather than failing silently.
    """
    from apps.characters.services import import_character_skillqueue, import_character_skills
    from apps.sso.token_service import NoValidToken
    from core.esi.client import ESIError

    imported = 0
    failed = 0
    for character in request.user.characters.all():
        try:
            import_character_skills(character)
            imported += 1
            try:
                import_character_skillqueue(character)
            except (NoValidToken, ESIError):
                pass  # queue is a nice-to-have; skills already landed
        except (NoValidToken, ESIError):
            failed += 1

    if imported:
        messages.success(request, ngettext(
            "Imported skills for %(count)d character.",
            "Imported skills for %(count)d characters.",
            imported,
        ) % {"count": imported})
    if failed and not imported:
        messages.warning(
            request,
            _(
                "Couldn't read your skills — your character hasn't granted skills access yet. "
                "Log out and log back in with EVE to authorise it."
            ),
        )
    return redirect("skills:plans")


@login_required
@role_required(rbac.ROLE_OFFICER)
def skill_gap(request: HttpRequest) -> HttpResponse:
    """Corp-wide skill-gap intelligence: bottlenecks + fastest candidates.

    Reads member skills corp-wide, so the access is audit-logged (consistent
    with the member-data access policy).
    """
    from apps.sso.models import EveCharacter

    from .gap import corp_skill_gap

    characters = list(EveCharacter.objects.filter(is_corp_member=True))
    audit_log(
        request.user,
        "skills.gap_viewed",
        metadata={"members": len(characters)},
        ip=client_ip(request),
    )
    # SKL-3 (2.12): one cached, snapshot-threaded computation instead of an
    # O(members × doctrines × fits) live scan that re-fetched snapshots per fit.
    gap = corp_skill_gap(characters)
    cand_by_doc = gap["candidates_by_doctrine"]
    doctrines = []
    for doctrine in (
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .order_by("-priority", "name")
    ):
        candidates = cand_by_doc.get(doctrine.id)
        if candidates:
            doctrines.append({"doctrine": doctrine, "candidates": candidates})
    return render(
        request,
        "skills/gap.html",
        {
            "bottlenecks": gap["bottlenecks"],
            "doctrines": doctrines,
            "member_count": len(characters),
        },
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def create_plan(request: HttpRequest) -> HttpResponse:
    character = get_object_or_404(
        request.user.characters, character_id=request.POST.get("character_id")
    )
    doctrine = get_object_or_404(
        Doctrine.objects.prefetch_related("fits__skill_requirements"),
        pk=request.POST.get("doctrine_id"),
    )
    plan = generate_plan_for_doctrine(character, doctrine)
    if not plan.steps.exists():
        plan.delete()
        messages.success(request, _("%(character)s can already fly %(doctrine)s — no plan needed.") % {
            "character": character.name, "doctrine": doctrine.name})
        return redirect("skills:plans")
    messages.success(request, _("Plan built: %(count)d skills toward %(doctrine)s.") % {
        "count": plan.steps.count(), "doctrine": doctrine.name})
    return redirect("skills:detail", pk=plan.pk)


def _get_owned_plan(user, pk: int) -> SkillPlan:
    return get_object_or_404(SkillPlan.objects.select_related("character"), pk=pk, character__user=user)


@login_required
@role_required(rbac.ROLE_MEMBER)
def plan_detail(request: HttpRequest, pk: int) -> HttpResponse:
    plan = _get_owned_plan(request.user, pk)
    from .services import plan_remap_advice
    return render(
        request,
        "skills/detail.html",
        {"plan": plan, "steps": plan.steps.all(), "remaining": remaining_seconds(plan),
         "remap": plan_remap_advice(plan)},
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def toggle_step(request: HttpRequest, pk: int, step_id: int) -> HttpResponse:
    plan = _get_owned_plan(request.user, pk)
    step = get_object_or_404(SkillPlanStep, pk=step_id, plan=plan)
    step.status = (
        SkillPlanStep.Status.PENDING
        if step.status == SkillPlanStep.Status.DONE
        else SkillPlanStep.Status.DONE
    )
    step.save(update_fields=["status"])
    return redirect("skills:detail", pk=pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def remove_step(request: HttpRequest, pk: int, step_id: int) -> HttpResponse:
    """Drop a skill from a plan — members keep agency over their training."""
    plan = _get_owned_plan(request.user, pk)
    get_object_or_404(SkillPlanStep, pk=step_id, plan=plan).delete()
    _resequence(plan)
    messages.success(request, _("Skill removed from the plan."))
    return redirect("skills:detail", pk=pk)


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def move_step(request: HttpRequest, pk: int, step_id: int) -> HttpResponse:
    """Move a step up or down so members can reorder their training path."""
    plan = _get_owned_plan(request.user, pk)
    step = get_object_or_404(SkillPlanStep, pk=step_id, plan=plan)
    direction = request.POST.get("dir")
    ordered = list(plan.steps.all())
    idx = next((i for i, s in enumerate(ordered) if s.id == step.id), None)
    if idx is not None:
        swap = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap < len(ordered):
            ordered[idx], ordered[swap] = ordered[swap], ordered[idx]
            for order, s in enumerate(ordered):
                if s.order != order:
                    s.order = order
                    s.save(update_fields=["order"])
    return redirect("skills:detail", pk=pk)


def _resequence(plan) -> None:
    """Renumber a plan's steps 0..n and refresh its total estimate."""
    total = 0
    for order, step in enumerate(plan.steps.all()):
        total += step.estimated_seconds or 0
        if step.order != order:
            step.order = order
            step.save(update_fields=["order"])
    plan.estimated_total_seconds = total
    plan.save(update_fields=["estimated_total_seconds"])


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def delete_plan(request: HttpRequest, pk: int) -> HttpResponse:
    _get_owned_plan(request.user, pk).delete()
    messages.success(request, _("Plan deleted."))
    return redirect("skills:plans")


@login_required
@role_required(rbac.ROLE_MEMBER)
def export_plan(request: HttpRequest, pk: int) -> HttpResponse:
    plan = _get_owned_plan(request.user, pk)
    return HttpResponse(export_plan_text(plan), content_type="text/plain; charset=utf-8")
