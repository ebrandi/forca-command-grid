"""Capsuleer Path HTTP surface (doc 10).

Thin views: parse input, call a service, render or redirect. Every view is ``@login_required`` and
feature-gated by the ``capsuleer`` namespace (middleware); object routes resolve the goal then
re-check ``services.can_view_goal`` / owner, raising ``Http404`` on failure (the no-existence-oracle
policy, doc 09 §2.2). All mutations are POST. Budget and motivation are masked at the context layer —
never placed in a non-owner's template context (doc 09 §2.3), so they cannot leak through the body.
Raw ids never render: names are resolved server-side (the campaigns UX lesson, doc 10 §6).
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from core.audit import audit_log, client_ip
from core.rbac import ROLE_OFFICER, has_role, role_required

from . import config, progress, services, suggest
from . import plan as plan_mod
from .models import (
    CareerActionStep,
    CareerGoal,
    CareerMilestone,
    CareerProfile,
    CareerTemplate,
    GoalPace,
    GoalStatus,
    GoalType,
    MilestoneKind,
    MilestoneStatus,
    PathSuggestion,
    Priority,
    StepStatus,
    Verification,
    Visibility,
)
from .taxonomy import Activity


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _owned_goal(request, pk) -> CareerGoal:
    goal = get_object_or_404(CareerGoal, pk=pk)
    if goal.user_id != request.user.pk:
        raise Http404("No such goal.")
    return goal


def _viewable_goal(request, pk) -> CareerGoal:
    goal = get_object_or_404(CareerGoal.objects.select_related("character", "template"), pk=pk)
    if not services.can_view_goal(request.user, goal):
        raise Http404("No such goal.")
    return goal


def _owned_milestone(request, pk) -> CareerMilestone:
    ms = get_object_or_404(CareerMilestone.objects.select_related("goal"), pk=pk)
    if ms.goal.user_id != request.user.pk:
        raise Http404("No such milestone.")
    return ms


def _owned_step(request, pk) -> CareerActionStep:
    step = get_object_or_404(CareerActionStep.objects.select_related("goal"), pk=pk)
    if step.goal.user_id != request.user.pk:
        raise Http404("No such step.")
    return step


def _back(request, default):
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect(default)


def _real_actor(request):
    """The real acting account under impersonation (audit attributes to the impersonator, doc 09 §5)."""
    return getattr(request, "impersonator", None) or request.user


def _real_actor_is_owner(request, owner_id) -> bool:
    """True only when the *real* acting account owns the object and is not viewing-as (doc 09 §5).

    The N-class budget fields gate on this, never on ``is_owner`` alone: under impersonation
    ``request.user`` IS the target, so ``is_owner`` would wrongly admit a director view-as to a
    pilot's private wallet figures. Motivation and paused_reason are O-class and do render under
    view-as, so they stay gated on ``is_owner`` (doc 09 §1.3)."""
    return getattr(request.user, "pk", None) == owner_id and not getattr(
        request, "is_impersonating", False
    )


def _owned_characters(user):
    return list(user.characters.all())


def _active_templates():
    disabled = set(config.get("templates").get("disabled_keys", []))
    return CareerTemplate.objects.filter(is_active=True).exclude(key__in=disabled)


def _char_or_none(user, character_id):
    """Resolve a submitted character id to one the user owns, else ``None`` (server-side ownership)."""
    if not character_id:
        return None
    try:
        cid = int(character_id)
    except (TypeError, ValueError):
        return None
    return next((c for c in user.characters.all() if c.character_id == cid), None)


# --------------------------------------------------------------------------- #
#  My Path home (doc 10 §5.1)
# --------------------------------------------------------------------------- #
@login_required
def home(request: HttpRequest) -> HttpResponse:
    user = request.user
    # One fetch feeds the cards and the quest row: the quest derivation needs milestones +
    # action_steps of the active goals, so prefetch both here and project the quest off the loaded
    # list rather than re-querying active goals (finding 42).
    goals = list(
        CareerGoal.objects.filter(user=user).exclude(status=GoalStatus.ARCHIVED)
        .select_related("character").prefetch_related("milestones", "action_steps")
    )
    active = [g for g in goals if g.status == GoalStatus.ACTIVE]
    other = [g for g in goals if g.status in (GoalStatus.PAUSED, GoalStatus.CONSIDERING)]
    profile = CareerProfile.objects.filter(user=user).first()
    from .briefing import career_quests_from_goals

    quest = career_quests_from_goals(active)
    suggestions = []
    if config.get("suggestions").get("enabled", True):
        suggestions = suggest.inbox_suggestions(user)[: int(config.get("suggestions").get("max_open_per_user", 6))]
    completed = list(
        CareerGoal.objects.filter(user=user, status=GoalStatus.COMPLETED)
        .order_by("-completed_at")[:5]
    )
    ctx = {
        "active_goals": active,
        "other_goals": other,
        "quest": quest[0] if quest else None,
        "suggestions": suggestions,
        "profile": profile,
        "profile_nudge": profile is None or profile.last_reviewed_at is None,
        "completed": completed,
        "template_count": _active_templates().count(),
        "all_paused": bool(active) is False and bool([g for g in goals if g.status == GoalStatus.PAUSED]),
        "has_any_goal": bool(goals),
    }
    return render(request, "capsuleer/home.html", ctx)


# --------------------------------------------------------------------------- #
#  Wizard (doc 10 §5.2)
# --------------------------------------------------------------------------- #
@login_required
def start_wizard(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        profile, _ = CareerProfile.objects.get_or_create(user=request.user)
        fields = []
        preferred = _clean_activities(request.POST.get("preferred_activities", ""))
        avoided = _clean_activities(request.POST.get("avoided_activities", ""))
        # A value may appear in at most one list (spec §1.1): avoided wins where both toggled.
        preferred = [a for a in preferred if a not in avoided]
        if preferred or avoided:
            profile.preferred_activities = preferred
            profile.avoided_activities = avoided
            fields += ["preferred_activities", "avoided_activities"]
        wh = _weekly_hours(request.POST.get("weekly_hours"))
        if wh is not None:
            profile.weekly_hours = wh
            fields.append("weekly_hours")
        pw = (request.POST.get("play_windows") or "").strip()
        if pw:
            profile.play_windows = pw[:200]
            fields.append("play_windows")
        pace = request.POST.get("pace")
        if pace in {"relaxed", "balanced", "accelerated"}:
            profile.pace = pace
            fields.append("pace")
        budget = _dec(request.POST.get("monthly_budget_isk"))
        if budget is not None and budget >= 0:
            profile.monthly_budget_isk = budget
            fields.append("monthly_budget_isk")
        alignment = request.POST.get("corp_alignment")
        if alignment in {"personal_only", "mostly_personal", "balanced", "corp_forward", "show_all"}:
            profile.corp_alignment = alignment
            fields.append("corp_alignment")
        if "mentor_interest" in request.POST:
            profile.mentor_interest = request.POST.get("mentor_interest") == "on"
            fields.append("mentor_interest")
        profile.last_reviewed_at = timezone.now()
        fields.append("last_reviewed_at")
        profile.save(update_fields=list(dict.fromkeys(fields)) + ["updated_at"])
        # The wizard's output is the filtered catalogue.
        dest = "capsuleer:paths"
        first = preferred[0] if preferred else ""
        url = redirect(dest)
        if first:
            url["Location"] += f"?activity={first}"
        messages.success(request, gettext("Saved. Here are paths that fit."))
        return url
    return render(request, "capsuleer/start.html", {
        "taxonomy": [{"value": v, "label": lab} for v, lab in Activity.choices],
    })


def _clean_activities(csv):
    values = {v.strip() for v in (csv or "").split(",") if v.strip()}
    return [v for v in Activity.values if v in values]


# --------------------------------------------------------------------------- #
#  Browse / compare / detail (doc 10 §5.3-§5.5)
# --------------------------------------------------------------------------- #
@login_required
def paths_browse(request: HttpRequest) -> HttpResponse:
    templates = _active_templates()
    activity = request.GET.get("activity")
    if activity in Activity.values:
        templates = templates.filter(category=activity)
    difficulty = request.GET.get("difficulty")
    if difficulty in {"1", "2", "3"}:
        templates = templates.filter(difficulty=int(difficulty))
    solo = request.GET.get("solo_group")
    if solo in {"solo", "group", "mixed"}:
        templates = templates.filter(solo_group=solo)
    if request.GET.get("newbro") == "on":
        templates = templates.filter(newbro_friendly=True)
    demand_activities = _demand_activities()
    demand_on = request.GET.get("demand") == "on"
    if demand_on:
        templates = (templates.filter(category__in=demand_activities)
                     if demand_activities else templates.none())
    q = (request.GET.get("q") or "").strip()
    if q:
        from django.db.models import Q

        templates = templates.filter(Q(name__icontains=q) | Q(description__icontains=q))
    templates = list(templates.order_by("difficulty", "name"))
    on_path = set(
        CareerGoal.objects.filter(user=request.user, template_key__in=[t.key for t in templates])
        .filter(status__in=[GoalStatus.CONSIDERING, GoalStatus.ACTIVE, GoalStatus.PAUSED])
        .values_list("template_key", flat=True)
    )
    ctx = {
        "templates": templates,
        "on_path": on_path,
        "activities": Activity.choices,
        "sde_version": _sde_version(),
        "demand_activities": demand_activities,
        "filters": {"activity": activity or "", "difficulty": difficulty or "",
                    "solo_group": solo or "", "newbro": request.GET.get("newbro") == "on",
                    "demand": demand_on, "q": q},
    }
    if request.headers.get("HX-Request"):
        return render(request, "capsuleer/_path_results.html", ctx)
    return render(request, "capsuleer/paths.html", ctx)


@login_required
def paths_compare(request: HttpRequest) -> HttpResponse:
    keys = [k for k in (request.GET.get("keys") or "").split(",") if k][:4]
    templates = (
        list(_active_templates().filter(key__in=keys).prefetch_related("advanced_paths"))
        if keys else []
    )
    all_templates = list(_active_templates().order_by("name"))
    # Mentor availability is computed once for the whole request, not per template (finding 24).
    mentor_counts = _mentor_counts_for_categories({t.category for t in templates})
    rows = [_compare_row(t, mentor_counts) for t in templates]
    return render(request, "capsuleer/compare.html", {
        "rows": rows,
        "all_templates": all_templates,
        "selected_keys": [t.key for t in templates],
        "sde_version": _sde_version(),
        "demand_activities": _demand_activities(),
    })


def _compare_row(template, mentor_counts) -> dict:
    return {
        "template": template,
        "mentors": mentor_counts.get(template.category, 0),
        "upcoming_ops": _upcoming_ops_for_category(template.category),
    }


def _mentor_counts_for_categories(categories) -> dict:
    """``{career category: available-mentor count}`` from ONE annotated mentor-pool fetch, shared
    across every selected template (finding 24) — no per-template, per-mentor capacity query."""
    out = {c: 0 for c in categories}
    if not categories:
        return out
    try:
        from django.db.models import Count, Q

        from apps.mentorship.models import MentorProfile, MentorshipPairing
        from apps.mentorship.services import active_program

        from .taxonomy import mentorship_category_for

        cat_map = {c: mentorship_category_for(c) for c in categories}
        default_cap = active_program().max_active_mentees_per_mentor
        mentors = MentorProfile.objects.filter(status=MentorProfile.Status.ACTIVE).annotate(
            _active_pairs=Count(
                "pairings", filter=Q(pairings__status__in=MentorshipPairing.CAPACITY_STATUSES)
            )
        )
        for m in mentors:
            if m._active_pairs >= (m.max_active_mentees or default_cap):
                continue
            areas = set(m.areas or [])
            for c, mcat in cat_map.items():
                if mcat in areas:
                    out[c] += 1
    except Exception:  # noqa: BLE001 — a live count that can't be computed renders as 0
        return {c: 0 for c in categories}
    return out


@login_required
def path_detail(request: HttpRequest, key: str) -> HttpResponse:
    template = get_object_or_404(_active_templates(), key=key)
    from . import plan as plan_mod_local
    from . import templates_i18n as t_i18n

    structure = template.structure or {}
    resolver = structure.get("doctrine_resolver")
    doctrine = plan_mod_local.resolve_doctrine(resolver) if resolver else None
    existing = (
        CareerGoal.objects.filter(user=request.user, template_key=template.key)
        .filter(status__in=[GoalStatus.CONSIDERING, GoalStatus.ACTIVE, GoalStatus.PAUSED]).first()
    )
    # Built-in milestone titles and assumptions live in the ``structure`` JSON verbatim (never a
    # model column), so route each through the render-time seam under the reader's locale —
    # translated while it still holds the shipped English, the officer's own words once edited.
    milestones = [
        {**m, "title_i18n": t_i18n.render(
            t_i18n.milestone_key(template.key, m.get("order")), "title", m.get("title", "")
        )}
        for m in structure.get("milestones", [])
    ]
    assumptions = [
        t_i18n.render(t_i18n.assumption_key(template.key, i), "text", a)
        for i, a in enumerate(structure.get("assumptions", []))
    ]
    ctx = {
        "template": template,
        "structure": structure,
        "milestones": milestones,
        "assumptions": assumptions,
        "doctrine": doctrine,
        "doctrine_linked": bool(resolver),
        "existing_goal": existing,
        "characters": _owned_characters(request.user),
        "advanced_from": template.advanced_from,
        "advances_to": list(template.advanced_paths.all()),
    }
    return render(request, "capsuleer/path_detail.html", ctx)


@login_required
@require_POST
def path_start(request: HttpRequest, key: str) -> HttpResponse:
    template = get_object_or_404(_active_templates(), key=key)
    character = _char_or_none(request.user, request.POST.get("character_id"))
    activate = request.POST.get("start_now") == "on"
    # Detect a pre-existing live goal so the flash reports what actually happened (finding 49):
    # instantiate_template returns an existing goal unchanged rather than starting a new one.
    already = CareerGoal.objects.filter(
        user=request.user, template_key=template.key,
        status__in=[GoalStatus.CONSIDERING, GoalStatus.ACTIVE, GoalStatus.PAUSED],
    ).exists()
    try:
        goal = plan_mod.instantiate_template(
            template, request.user, character=character, activate=activate,
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("capsuleer:path_detail", key=key)
    if already:
        messages.info(request, gettext("You're already on «%(title)s».") % {"title": goal.title_i18n})
    elif activate:
        messages.success(request, gettext("Started «%(title)s».") % {"title": goal.title_i18n})
    else:
        messages.success(request, gettext("Saved «%(title)s» for later.") % {"title": goal.title_i18n})
    return redirect("capsuleer:goal_detail", pk=goal.pk)


# --------------------------------------------------------------------------- #
#  Goal create / edit (doc 10 §5.6)
# --------------------------------------------------------------------------- #
@login_required
def goal_new(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        try:
            goal = _create_goal_from_post(request)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(request, "capsuleer/goal_form.html", _goal_form_ctx(request, None))
        messages.success(request, gettext("Created «%(title)s».") % {"title": goal.title})
        return redirect("capsuleer:goal_detail", pk=goal.pk)
    return render(request, "capsuleer/goal_form.html", _goal_form_ctx(request, None))


def _create_goal_from_post(request) -> CareerGoal:
    p = request.POST
    goal_type = p.get("goal_type", GoalType.CUSTOM)
    character = _char_or_none(request.user, p.get("character_id"))
    kwargs = dict(
        title=p.get("title", ""), goal_type=goal_type, character=character,
        motivation=p.get("motivation", ""), priority=p.get("priority", Priority.SECONDARY),
        pace=p.get("pace", GoalPace.INHERIT), visibility=p.get("visibility") or None,
        target_date=_date(p.get("target_date")), budget_isk=_dec(p.get("budget_isk")),
        corp_alignment_optin=p.get("corp_alignment_optin") == "on",
        status=GoalStatus.ACTIVE if p.get("start_now") == "on" else GoalStatus.CONSIDERING,
    )
    if goal_type == GoalType.DOCTRINE:
        kwargs["doctrine_id"] = _int(p.get("doctrine_id"))
    elif goal_type == GoalType.SHIP:
        kwargs["ship_type_id"] = _int(p.get("ship_type_id"))
    elif goal_type == GoalType.ACTIVITY:
        act = p.get("activity")
        kwargs["activity"] = act if act in Activity.values else ""
    return services.create_goal(request.user, **kwargs)


@login_required
def goal_edit(request: HttpRequest, pk: int) -> HttpResponse:
    goal = _owned_goal(request, pk)
    if goal.status == GoalStatus.ARCHIVED:
        raise Http404("Archived goals are read-only.")
    if request.method == "POST":
        _apply_goal_edit(request, goal)
        messages.success(request, gettext("Saved."))
        return redirect("capsuleer:goal_detail", pk=goal.pk)
    return render(request, "capsuleer/goal_form.html", _goal_form_ctx(request, goal))


def _apply_goal_edit(request, goal) -> None:
    p = request.POST
    fields = ["updated_at"]
    goal.title = (p.get("title") or goal.title).strip()[:140]
    goal.motivation = (p.get("motivation", goal.motivation) or "")[:2000]  # bounded text (finding 17)
    if p.get("priority") in Priority.values:
        goal.priority = p.get("priority")
    if p.get("pace") in GoalPace.values:
        goal.pace = p.get("pace")
    goal.target_date = _date(p.get("target_date"))
    budget = _dec(p.get("budget_isk"))
    if budget is None or budget >= 0:
        goal.budget_isk = budget
    goal.corp_alignment_optin = p.get("corp_alignment_optin") == "on"
    # Character is editable only while considering (post-activation would invalidate baselines).
    if goal.status == GoalStatus.CONSIDERING:
        character = _char_or_none(request.user, p.get("character_id"))
        goal.character = character
    fields += ["title", "motivation", "priority", "pace", "target_date", "budget_isk",
               "corp_alignment_optin", "character"]
    goal.save(update_fields=list(dict.fromkeys(fields)))
    services.record_activity(goal, request.user, "goal.edited", {})
    # A visibility change from the edit form takes effect through the service so narrowing actually
    # applies and is recorded in GoalActivity — never silently dropped (finding 8, AC14).
    new_vis = p.get("visibility")
    if new_vis in Visibility.values and new_vis != goal.visibility:
        services.set_goal_visibility(goal, request.user, new_vis)


def _goal_form_ctx(request, goal) -> dict:
    from apps.doctrines.models import Doctrine

    ctx = {
        "goal": goal,
        "characters": _owned_characters(request.user),
        "activities": Activity.choices,
        "visibilities": Visibility.choices,
        "priorities": Priority.choices,
        "paces": GoalPace.choices,
        "doctrines": list(Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
                          .order_by("-priority", "name")[:60]),
        "min_group": max(2, int(config.get("leadership").get("min_group", 4))),
    }
    # N-class budget pre-fills only for the real owner, never a director view-as (findings 3/4).
    if goal is not None and _real_actor_is_owner(request, goal.user_id):
        ctx["owner_budget"] = goal.budget_isk
    return ctx


# --------------------------------------------------------------------------- #
#  Goal detail (doc 10 §5.7)
# --------------------------------------------------------------------------- #
@login_required
def goal_detail(request: HttpRequest, pk: int) -> HttpResponse:
    goal = _viewable_goal(request, pk)
    is_owner = goal.user_id == request.user.pk

    # Officer read of an officers-tier goal is audit-logged before render (doc 09 §3.3).
    if not is_owner and goal.visibility == Visibility.OFFICERS and has_role(request.user, ROLE_OFFICER):
        actor = _real_actor(request)
        audit_log(actor, "capsuleer.goal.view", target_type=services.TARGET_GOAL,
                  target_id=str(goal.pk), metadata={"owner_id": goal.user_id}, ip=client_ip(request))

    ctx = _goal_detail_ctx(request, goal, is_owner)
    return render(request, "capsuleer/goal_detail.html", ctx)


def _goal_detail_ctx(request, goal, is_owner) -> dict:
    if not is_owner:
        # Defence-in-depth for the context-layer chokepoint (doc 09 §2.3, T-8, finding 35): a
        # non-owner render never carries the N/O-class values, so even a stray ``{{ goal.motivation }}``
        # cannot leak. Under impersonation ``is_owner`` is True (the target owns it), so a director
        # view-as still sees the O-class fields as the matrix intends. The row is request-scoped.
        goal.motivation = ""
        goal.budget_isk = None
        goal.paused_reason = ""
    milestones = list(goal.milestones.all().order_by("order"))
    # One endorsement-stream fetch feeds every milestone view-model (no per-milestone scan, finding 19).
    endorsements = services.endorsement_map(goal)
    ms_vms = [_milestone_vm(m, endorsements) for m in milestones]
    # derive_blocked reads the stamped structural flag off the already-loaded milestones — the
    # request path never re-runs the verification engine (finding 21).
    blocked, blocked_reasons = progress.derive_blocked(goal, milestones=milestones, live=False)
    steps = list(goal.action_steps.all().order_by("id"))
    next_step = next((s for s in steps if s.status == StepStatus.OPEN), None)

    eta = {"state": "unknown"}
    plan_ctx = None
    if is_owner:
        # Fetch (plan, snapshot) once and thread into both the ETA and the plan panel (finding 25).
        plan_obj, snapshot = progress._plan_and_snapshot(goal)
        eta = progress.estimate_eta(goal, plan=plan_obj, snapshot=snapshot)
        # Target-date risk chip (doc 11 §3.2, finding 30): likely finish vs the pilot's target date.
        if eta.get("state") == "ok" and goal.target_date and eta.get("likely"):
            eta["target_risk"] = "on_track" if eta["likely"].date() <= goal.target_date else "at_risk"
        plan_ctx = _plan_ctx(goal, plan=plan_obj)

    # Viewer affordances (doc 09 §3): mentee ids computed once, non-owner branch only (finding 25).
    can_mentor_note = False
    is_officer_viewer = False
    tier_label = None
    if not is_owner:
        mentee_ids = services._active_mentee_user_ids(request.user)
        can_mentor_note = goal.visibility == Visibility.MENTOR and goal.user_id in mentee_ids
        is_officer_viewer = (goal.visibility == Visibility.OFFICERS
                             and has_role(request.user, ROLE_OFFICER))
        tier_label = "mentor" if can_mentor_note else ("officer" if is_officer_viewer else "viewer")

    ctx = {
        "goal": goal,
        "is_owner": is_owner,
        "milestones": ms_vms,
        "blocked": blocked,
        "blocked_reasons": blocked_reasons,
        "eta": eta,
        "plan": plan_ctx,
        "steps": steps,
        "next_step": next_step,
        "next_milestone": next((v for v in ms_vms if v["milestone"].required
                                and v["milestone"].status == MilestoneStatus.PENDING), None),
        "character_name": goal.character.name if goal.character else None,
        "character_id": goal.character.character_id if goal.character else None,
        "can_mentor_note": can_mentor_note,
        "can_endorse_officer": is_officer_viewer,
        "viewer_tier": tier_label,
        "visibilities": Visibility.choices,
        "legal_transitions": _legal_transitions(goal.status) if is_owner else [],
        "activity": (
            list(goal.activity_log.select_related("actor").prefetch_related("actor__characters")
                 .all()[:40]) if is_owner else []
        ),
        "suggestions": (
            list(PathSuggestion.objects.filter(user=request.user, goal=goal,
                                                status="open").order_by("-created_at"))
            if is_owner else []
        ),
    }
    if is_owner:
        # Motivation / paused_reason are O-class — they render to the owner and, deliberately, to a
        # director view-as (doc 09 §1.3), so they gate on is_owner.
        ctx["owner_motivation"] = goal.motivation
        ctx["owner_paused_reason"] = goal.paused_reason
        ctx["milestone_kinds"] = MilestoneKind.choices
        ctx["mentor_directory_url"] = _mentor_directory_url()
        ctx["sde_version"] = _sde_version()
        # The corp-task dialog pre-fills a neutral default title (doc 05 §5.2, finding 9) — for a
        # non-officers goal the goal/ambition context is stripped so the confirm form shows what
        # will actually be published, editable before it lands on the corp board.
        if next_step is not None:
            ctx["next_step_task_title"] = (
                next_step.title if goal.visibility == Visibility.OFFICERS
                else services._neutralise_step_title(next_step.title, goal)
            )
    # Budget is N-class — it gates on the REAL actor, so an impersonating director never sees it
    # even though request.user (the target) technically owns the goal (findings 1-4).
    if _real_actor_is_owner(request, goal.user_id):
        ctx["owner_budget"] = goal.budget_isk
    return ctx


def _milestone_vm(m, endorsements) -> dict:
    endorsed = False
    if m.verification in (Verification.MENTOR, Verification.OFFICER):
        endorsed = (m.pk, m.verification) in endorsements
    return {
        "milestone": m,
        "awaiting_endorsement": (m.verification in (Verification.MENTOR, Verification.OFFICER)
                                 and m.status == MilestoneStatus.PENDING and not endorsed),
        "endorsed": endorsed,
        "evidence": m.evidence_snapshot or {},
        "evidence_summary": _evidence_summary(m),
    }


def _evidence_summary(m) -> str:
    """A compact human-readable evidence line for a done milestone (doc 10 §5.7, finding 32), from
    ``evidence_snapshot`` — never raw JSON."""
    if m.status != MilestoneStatus.DONE:
        return ""
    snap = m.evidence_snapshot or {}
    bits = []
    if snap.get("self_certified"):
        bits.append(gettext("self-certified"))
    elif snap.get("verifier_role"):
        bits.append(gettext("%(role)s-verified") % {"role": snap["verifier_role"]})
    for key in ("summary", "label", "doctrine_name", "doctrine", "ship_name"):
        if snap.get(key):
            bits.append(str(snap[key]))
            break
    as_of = snap.get("as_of") or snap.get("at")
    if as_of:
        bits.append(gettext("as of %(date)s") % {"date": str(as_of)[:10]})
    return " · ".join(bits)


def _mentor_directory_url():
    """The mentorship directory deep link for the Sharing & mentor panel (AC22, finding 33), or
    ``None`` when mentorship is not mounted."""
    from django.urls import NoReverseMatch, reverse

    try:
        return reverse("mentorship:mentors")
    except NoReverseMatch:
        return None


def _plan_ctx(goal, *, plan=None):
    if not goal.skill_plan_id:
        return {"exists": False}
    from apps.skills.services import remaining_seconds

    if plan is None:
        from apps.skills.models import SkillPlan

        plan = SkillPlan.objects.filter(pk=goal.skill_plan_id).prefetch_related("steps").first()
    if plan is None:
        return {"exists": False}
    return {
        "exists": True,
        "plan": plan,
        "remaining_seconds": remaining_seconds(plan),
        "cost": plan_mod.estimate_initial_cost(goal),
    }


def _legal_transitions(status):
    return [to for (frm, to) in services._LEGAL_GOAL_TRANSITIONS if frm == status]


# --------------------------------------------------------------------------- #
#  Goal mutations (doc 10 §5.7)
# --------------------------------------------------------------------------- #
@login_required
@require_POST
def goal_status(request: HttpRequest, pk: int) -> HttpResponse:
    goal = _owned_goal(request, pk)
    to_status = request.POST.get("to")
    try:
        services.set_goal_status(goal, to_status, request.user, reason=request.POST.get("reason", ""))
        messages.success(request, gettext("Updated."))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("capsuleer:goal_detail", pk=goal.pk)


@login_required
@require_POST
def goal_share(request: HttpRequest, pk: int) -> HttpResponse:
    goal = _owned_goal(request, pk)
    try:
        services.set_goal_visibility(goal, request.user, request.POST.get("visibility"))
        messages.success(request, gettext("Sharing updated."))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("capsuleer:goal_detail", pk=goal.pk)


@login_required
@require_POST
def goal_build_plan(request: HttpRequest, pk: int) -> HttpResponse:
    """Owner-only retry for a missing/failed skill plan (doc 10 §5.7, finding 45). The activation
    build is best-effort, so this gives the pilot a way to recover without abandoning the goal."""
    goal = _owned_goal(request, pk)
    try:
        created = plan_mod.build_plan(goal)
        if created is not None:
            messages.success(request, gettext("Skill plan built."))
        else:
            messages.info(request, gettext("This goal has no skill targets to plan."))
    except Exception:  # noqa: BLE001 — a transient SDE/doctrine issue must not 500 the page
        messages.error(request,
                       gettext("Couldn't build a plan right now — try again after your next skill sync."))
    return redirect("capsuleer:goal_detail", pk=goal.pk)


@login_required
@require_POST
def goal_review(request: HttpRequest, pk: int) -> HttpResponse:
    goal = _owned_goal(request, pk)
    services.complete_review(goal, request.user, note=request.POST.get("note", ""))
    messages.success(request, gettext("Thanks — review noted."))
    return redirect("capsuleer:goal_detail", pk=goal.pk)


@login_required
@require_POST
def goal_note(request: HttpRequest, pk: int) -> HttpResponse:
    goal = get_object_or_404(CareerGoal, pk=pk)
    try:
        services.add_mentor_note(goal, request.user, request.POST.get("text", ""))
        messages.success(request, gettext("Note added."))
    except ValidationError:
        raise Http404("No such goal.")  # noqa: B904 — 404, never a permission oracle
    return redirect("capsuleer:goal_detail", pk=goal.pk)


@login_required
@require_POST
def goal_endorse(request: HttpRequest, pk: int) -> HttpResponse:
    goal = get_object_or_404(CareerGoal, pk=pk)
    milestone = get_object_or_404(CareerMilestone, pk=request.POST.get("milestone_id") or 0,
                                  goal=goal)
    try:
        services.endorse_milestone(goal, milestone, request.user,
                                   note=request.POST.get("note", ""), ip=client_ip(request))
        messages.success(request, gettext("Endorsement recorded."))
    except ValidationError:
        raise Http404("No such goal.")  # noqa: B904
    return redirect("capsuleer:goal_detail", pk=goal.pk)


# --------------------------------------------------------------------------- #
#  Milestone / step mutations
# --------------------------------------------------------------------------- #
@login_required
@require_POST
def milestone_add(request: HttpRequest, pk: int) -> HttpResponse:
    goal = _owned_goal(request, pk)
    kind = request.POST.get("kind", MilestoneKind.MANUAL)
    verification = request.POST.get("verification", Verification.SELF)
    params = {} if kind == MilestoneKind.MANUAL else _milestone_params(request, kind)
    try:
        services.add_milestone(goal, request.user, kind=kind, title=request.POST.get("title", ""),
                               verification=verification, required=request.POST.get("required") == "on",
                               params=params)
        messages.success(request, gettext("Milestone added."))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("capsuleer:goal_detail", pk=goal.pk)


def _milestone_params(request, kind):
    if kind == MilestoneKind.PRACTICAL:
        out = {}
        if request.POST.get("instructions"):
            out["instructions"] = request.POST["instructions"][:500]
        return out
    if kind == MilestoneKind.SKILL_TARGET:
        tid = _int(request.POST.get("type_id"))
        lvl = _int(request.POST.get("level")) or 1
        return {"skills": [{"type_id": tid, "level": lvl}]} if tid else {}
    if kind == MilestoneKind.SHIP_OWNED:
        tid = _int(request.POST.get("ship_type_id"))
        return {"type_ids": [tid]} if tid else {}
    return {}


@login_required
@require_POST
def milestone_update(request: HttpRequest, pk: int) -> HttpResponse:
    ms = _owned_milestone(request, pk)
    if ms.goal.status in services._TERMINAL_STATUSES:
        messages.error(request, gettext("This goal is finished — reopen it before editing its milestones."))
        return redirect("capsuleer:goal_detail", pk=ms.goal_id)
    if ms.status == MilestoneStatus.DONE:
        messages.error(request, gettext("A completed milestone's details can't be edited — reopen it first."))
        return redirect("capsuleer:goal_detail", pk=ms.goal_id)
    ms.title = (request.POST.get("title") or ms.title).strip()[:140]
    ms.required = request.POST.get("required") == "on"
    ms.due_date = _date(request.POST.get("due_date"))
    ms.save(update_fields=["title", "required", "due_date", "updated_at"])
    services.recompute_progress(ms.goal)
    messages.success(request, gettext("Milestone updated."))
    return redirect("capsuleer:goal_detail", pk=ms.goal_id)


@login_required
@require_POST
def milestone_status(request: HttpRequest, pk: int) -> HttpResponse:
    ms = _owned_milestone(request, pk)
    action = request.POST.get("action")
    try:
        if action == "done":
            services.complete_milestone(ms.goal, ms, request.user,
                                        evidence_note=request.POST.get("evidence_note", ""))
        elif action == "skip":
            services.skip_milestone(ms.goal, ms, request.user, note=request.POST.get("note", ""))
        elif action == "unskip":
            services.unskip_milestone(ms.goal, ms, request.user)
        elif action == "reopen":
            services.reopen_milestone(ms.goal, ms, request.user)
        else:
            raise ValidationError(gettext("Unknown action."))
        messages.success(request, gettext("Milestone updated."))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("capsuleer:goal_detail", pk=ms.goal_id)


@login_required
@require_POST
def step_add(request: HttpRequest, pk: int) -> HttpResponse:
    goal = _owned_goal(request, pk)
    if goal.status in services._TERMINAL_STATUSES:
        messages.error(request, gettext("This goal is finished — reopen it before adding steps."))
        return redirect("capsuleer:goal_detail", pk=goal.pk)
    if goal.action_steps.count() >= services.MAX_STEPS_PER_GOAL:
        messages.error(request, gettext("That goal already has the maximum number of steps."))
        return redirect("capsuleer:goal_detail", pk=goal.pk)
    title = (request.POST.get("title") or "").strip()
    if not title:
        messages.error(request, gettext("A step needs a title."))
        return redirect("capsuleer:goal_detail", pk=goal.pk)
    CareerActionStep.objects.create(
        goal=goal, title=title[:140], note=(request.POST.get("note") or "")[:300],
        est_cost_isk=_dec(request.POST.get("est_cost_isk")), source="pilot",
    )
    messages.success(request, gettext("Step added."))
    return redirect("capsuleer:goal_detail", pk=goal.pk)


@login_required
@require_POST
def step_status(request: HttpRequest, pk: int) -> HttpResponse:
    step = _owned_step(request, pk)
    if step.goal.status in services._TERMINAL_STATUSES:
        messages.error(request, gettext("This goal is finished — reopen it before changing its steps."))
        return redirect("capsuleer:goal_detail", pk=step.goal_id)
    action = request.POST.get("action")
    if action == "done":
        step.status = StepStatus.DONE
        step.completed_at = timezone.now()
    elif action == "dismiss":
        step.status = StepStatus.DISMISSED
    elif action == "reopen":
        step.status = StepStatus.OPEN
        step.completed_at = None
    else:
        raise Http404("Unknown action.")
    step.save(update_fields=["status", "completed_at", "updated_at"])
    messages.success(request, gettext("Step updated."))
    return redirect("capsuleer:goal_detail", pk=step.goal_id)


@login_required
@require_POST
def step_task(request: HttpRequest, pk: int) -> HttpResponse:
    step = _owned_step(request, pk)
    try:
        services.make_corp_task_from_step(
            step.goal, step, request.user,
            title=request.POST.get("title") or None, description=request.POST.get("description", ""),
        )
        messages.success(request, gettext("Corp task created — it's visible to the whole corp."))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("capsuleer:goal_detail", pk=step.goal_id)


# --------------------------------------------------------------------------- #
#  Profile (doc 10 §5.8)
# --------------------------------------------------------------------------- #
@login_required
def profile(request: HttpRequest) -> HttpResponse:
    impersonating = getattr(request, "is_impersonating", False)
    if impersonating:
        # View-as is read-only (doc 09 §5): never create the target's profile with a GET, and
        # never surface the N-class monthly budget to a director viewing as the pilot.
        obj = CareerProfile.objects.filter(user=request.user).first() or CareerProfile(
            user=request.user
        )
    else:
        obj, _ = CareerProfile.objects.get_or_create(user=request.user)
    if request.method == "POST":
        _save_profile(request, obj)
        messages.success(request, gettext("Preferences saved."))
        return redirect("capsuleer:profile")
    return render(request, "capsuleer/profile.html", {
        "profile": obj,
        "budget_display": None if impersonating else obj.monthly_budget_isk,
        "activities": Activity.choices,
        "visibilities": Visibility.choices,
        "muted_kinds": set(obj.suggestion_muted_kinds or []),
        "suggestion_kinds": _suggestion_kind_labels(),
    })


def _save_profile(request, obj) -> None:
    p = request.POST
    preferred = _clean_activities(p.get("preferred_activities", ""))
    avoided = _clean_activities(p.get("avoided_activities", ""))
    curious = _clean_activities(p.get("curious_activities", ""))
    # A value belongs to at most one list (spec §1.1): avoided wins over both, and
    # preferred wins over curious, so the three lists are mutually exclusive.
    avoided_set = set(avoided)
    obj.preferred_activities = [a for a in preferred if a not in avoided_set]
    pref_set = set(obj.preferred_activities)
    obj.curious_activities = [a for a in curious if a not in avoided_set and a not in pref_set]
    obj.avoided_activities = avoided
    obj.weekly_hours = _weekly_hours(p.get("weekly_hours"))
    obj.play_windows = (p.get("play_windows") or "")[:200]
    if p.get("pace") in {"relaxed", "balanced", "accelerated"}:
        obj.pace = p.get("pace")
    budget = _dec(p.get("monthly_budget_isk"))
    obj.monthly_budget_isk = budget if (budget is None or budget >= 0) else obj.monthly_budget_isk
    if p.get("corp_alignment") in {"personal_only", "mostly_personal", "balanced", "corp_forward",
                                   "show_all"}:
        obj.corp_alignment = p.get("corp_alignment")
    obj.mentor_interest = p.get("mentor_interest") == "on"
    if p.get("default_visibility") in Visibility.values:
        obj.default_visibility = p.get("default_visibility")
    obj.last_reviewed_at = timezone.now()
    obj.save()
    # Suggestion mutes: the checkbox list is authoritative. Apply the delta through the suggest
    # service so a profile mute also expires that kind's open rows (doc 08 §9, matching the
    # "not interested" path) and an untick resumes it next run (unmute_kind).
    desired = {k for k, _ in _suggestion_kind_labels() if p.get(f"mute_{k}") == "on"}
    current = set(obj.suggestion_muted_kinds or [])
    for kind in desired - current:
        suggest.mute_kind(obj.user, kind)
    for kind in current - desired:
        suggest.unmute_kind(obj.user, kind)


def _suggestion_kind_labels():
    from .models import SuggestionKind

    return list(SuggestionKind.choices)


# --------------------------------------------------------------------------- #
#  Suggestions + quests (doc 10 §5.9, §5.12)
# --------------------------------------------------------------------------- #
@login_required
@require_POST
def suggestion_act(request: HttpRequest, pk: int) -> HttpResponse:
    row = get_object_or_404(PathSuggestion, pk=pk)
    try:
        result = suggest.act_on_suggestion(request.user, row, request.POST.get("action", ""))
    except ValidationError:
        raise Http404("No such suggestion.")  # noqa: B904 — non-owner is indistinguishable from missing
    redirect_hint = result.get("redirect")
    if redirect_hint and redirect_hint.get("url_name"):
        try:
            args = [a for a in redirect_hint.get("args", []) if a is not None]
            return redirect(redirect_hint["url_name"], *args)
        except Exception:  # noqa: BLE001, S110 — a bad deep link never breaks the action
            pass
    return _back(request, "capsuleer:home")


def _parse_quest_ref(ref):
    """Parse a career quest id into ``(kind, pk)``. Career rows prefix the id ``s``/``m`` so a step
    and a milestone that share a numeric pk can never mutate each other (finding 29); a bare-digit
    legacy id resolves as a step. An unparseable ref yields ``pk=None`` → a clean 404."""
    ref = str(ref)
    if ref[:1] == "s" and ref[1:].isdigit():
        return "step", int(ref[1:])
    if ref[:1] == "m" and ref[1:].isdigit():
        return "milestone", int(ref[1:])
    if ref.isdigit():
        return "step", int(ref)
    return "step", None


@login_required
@require_POST
def quest_action(request: HttpRequest, ref: str) -> HttpResponse:
    action = request.POST.get("action")
    kind, pk = _parse_quest_ref(ref)
    if kind == "step":
        step = (CareerActionStep.objects.filter(pk=pk, goal__user=request.user)
                .select_related("goal").first())
        if step is None:
            raise Http404("No such quest.")
        if action == "done":
            step.status = StepStatus.DONE
            step.completed_at = timezone.now()
            step.save(update_fields=["status", "completed_at", "updated_at"])
            services.record_activity(step.goal, request.user, "step.done", {"step_id": step.pk})
        elif action == "snooze":
            step.snoozed_until = timezone.now() + timezone.timedelta(days=7)
            step.save(update_fields=["snoozed_until", "updated_at"])
        elif action == "dismiss":
            step.status = StepStatus.DISMISSED
            step.save(update_fields=["status", "updated_at"])
        return _back(request, "identity:dashboard")
    # Milestone-backed quest row (the fallback surfaces only owner-completable milestones).
    ms = (CareerMilestone.objects.filter(pk=pk, goal__user=request.user)
          .select_related("goal").first())
    if ms is None:
        raise Http404("No such quest.")
    try:
        if action == "done":
            services.complete_milestone(ms.goal, ms, request.user)
        elif action == "dismiss":
            services.skip_milestone(ms.goal, ms, request.user)
        elif action == "snooze":
            # Milestones have no snooze state — give explicit feedback rather than a silent no-op.
            messages.info(request, gettext("Milestones can't be snoozed — mark it done when you're ready."))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return _back(request, "identity:dashboard")


# --------------------------------------------------------------------------- #
#  Leadership aggregates (doc 10 §5.10)
# --------------------------------------------------------------------------- #
@login_required
@role_required(ROLE_OFFICER)
def leadership(request: HttpRequest) -> HttpResponse:
    audit_log(_real_actor(request), "capsuleer.leadership.view",
              target_type="capsuleer_leadership", ip=client_ip(request))
    return render(request, "capsuleer/leadership.html", {"pipeline": services.leadership_pipeline()})


# --------------------------------------------------------------------------- #
#  JSON pickers (doc 10 §7)
# --------------------------------------------------------------------------- #
@login_required
def types_json(request: HttpRequest) -> JsonResponse:
    from apps.sde.search import search_skills

    return JsonResponse(search_skills((request.GET.get("q") or "").strip(), limit=20), safe=False)


@login_required
def ships_json(request: HttpRequest) -> JsonResponse:
    from apps.sde.search import search_ships

    return JsonResponse(search_ships((request.GET.get("q") or "").strip(), limit=20), safe=False)


@login_required
def doctrines_json(request: HttpRequest) -> JsonResponse:
    from apps.doctrines.models import Doctrine

    q = (request.GET.get("q") or "").strip()
    qs = Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
    if q:
        qs = qs.filter(name__icontains=q)
    rows = [
        {"id": d.id, "name": d.name, "hint": f"{d.fits.count()} fit(s)"}
        for d in qs.order_by("-priority", "name")[:25]
    ]
    return JsonResponse(rows, safe=False)


# --------------------------------------------------------------------------- #
#  Small parse/format helpers
# --------------------------------------------------------------------------- #
def _dec(value):
    from decimal import Decimal, InvalidOperation

    value = (value or "").strip() if isinstance(value, str) else value
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    # Reject non-finite (NaN/Infinity) and values beyond the money column's range — either would
    # crash the >= comparison or the DB write (doc 09 T-17). Absence, not a poisoned record.
    if not d.is_finite() or abs(d) >= Decimal("1e18"):
        return None
    return d


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _weekly_hours(raw):
    """Parse a weekly-hours field, clamped to 0..168. Unicode digits / overflow return ``None``
    rather than 500ing (``str.isdigit`` admits '²' which ``int`` rejects — doc 09 T-17)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if 0 <= v <= 168 else None


def _date(value):
    value = (value or "").strip()
    if not value:
        return None
    from django.utils.dateparse import parse_date

    return parse_date(value)


def _sde_version():
    from apps.admin_audit.models import AppSetting

    return (AppSetting.get("sde_version", {}) or {}).get("version", "") if isinstance(
        AppSetting.get("sde_version", {}), dict) else AppSetting.get("sde_version", "")


def _demand_activities() -> set:
    """Career activities the corp needs help with right now — any activity whose operation types
    have an upcoming planned op in the next 14 days (doc 10 §5.3, finding 48).

    Corp-wide and slow-moving, so it is cached briefly: the browse/compare pages read it for free on
    the warm path rather than paying an Operations query on every load. Best-effort — absent on any
    failure, never an error."""
    from django.core.cache import cache

    cached = cache.get("capsuleer:demand_activities")
    if cached is not None:
        return cached
    try:
        from apps.operations.models import Operation

        from .taxonomy import ACTIVITY_TO_OPERATION_TYPES

        now = timezone.now()
        op_types = set(
            Operation.objects.filter(
                status=Operation.Status.PLANNED, target_at__gt=now,
                target_at__lte=now + timezone.timedelta(days=14),
            ).values_list("type", flat=True)
        )
        result = {act for act, types in ACTIVITY_TO_OPERATION_TYPES.items() if types & op_types}
    except Exception:  # noqa: BLE001 — demand signalling is best-effort, absent on failure
        result = set()
    cache.set("capsuleer:demand_activities", result, 120)
    return result


def _upcoming_ops_for_category(category):
    try:
        from apps.operations.models import Operation

        from .taxonomy import operation_types_for

        types = operation_types_for(category)
        if not types:
            return 0
        now = timezone.now()
        return Operation.objects.filter(
            status=Operation.Status.PLANNED, type__in=types,
            target_at__gt=now, target_at__lte=now + timezone.timedelta(days=14),
        ).count()
    except Exception:  # noqa: BLE001
        return 0
