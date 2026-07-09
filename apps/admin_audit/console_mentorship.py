"""Admin Console: Mentorship Program governance.

Leadership config, catalogue CRUD (cohorts/tracks/tasks/reward rules/badges),
application & pairing approvals, the matching worklist, the reward approval/pay
queue, and reporting. Mirrors the console conventions: ``@login_required`` +
``@role_required`` (officer for day-to-day, director for programme config &
rewards), ``@require_POST`` on mutations, ``audit_log`` + Post/Redirect/Get.
"""
from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.mentorship import forms, matching, reporting, rewards, services, trust
from apps.mentorship.models import (
    MenteeProfile,
    MentorProfile,
    MentorshipBadge,
    MentorshipCohort,
    MentorshipFlag,
    MentorshipPairing,
    MentorshipRewardLedger,
    MentorshipRewardRule,
    MentorshipTask,
    MentorshipTrack,
)
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required


def _audit(request, action, **kw):
    audit_log(request.user, action, ip=client_ip(request), **kw)


# ---------------------------------------------------------------------------
# Overview hub
# ---------------------------------------------------------------------------
@login_required
@role_required(rbac.ROLE_OFFICER)
def mentorship_hub(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_audit/console/mentorship/hub.html", {
        "summary": reporting.program_summary(),
        "program": services.active_program(),
        "pending_mentors": MentorProfile.objects.filter(
            status=MentorProfile.Status.PENDING).select_related("user")[:8],
        "pending_mentees": MenteeProfile.objects.filter(
            status=MenteeProfile.Status.PENDING).select_related("user")[:8],
        "pending_pairs": MentorshipPairing.objects.filter(
            status=MentorshipPairing.Status.PENDING_APPROVAL
        ).select_related("mentor__user", "mentee__user")[:8],
    })


# ---------------------------------------------------------------------------
# Programme configuration (Director)
# ---------------------------------------------------------------------------
@login_required
@role_required(rbac.ROLE_DIRECTOR)
def mentorship_config(request: HttpRequest) -> HttpResponse:
    program = services.active_program()
    if request.method == "POST":
        form = forms.MentorshipProgramForm(request.POST, instance=program)
        if form.is_valid():
            form.save()
            _audit(request, "mentorship.config_update", target_type="mentorship_program",
                   target_id=str(program.pk))
            messages.success(request, "Mentorship Program settings saved.")
            return redirect("admin_audit:mentorship_config")
        messages.error(request, "Please correct the errors below.")
    else:
        form = forms.MentorshipProgramForm(instance=program)
    return render(request, "admin_audit/console/mentorship/config.html",
                  {"form": form, "program": program})


# ---------------------------------------------------------------------------
# Catalogue: cohorts, tracks, tasks
# ---------------------------------------------------------------------------
@login_required
@role_required(rbac.ROLE_OFFICER)
def mentorship_cohorts(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_audit/console/mentorship/cohorts.html", {
        "cohorts": MentorshipCohort.objects.all(),
        "form": forms.CohortForm(),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_cohort_save(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    instance = get_object_or_404(MentorshipCohort, pk=pk) if pk else None
    form = forms.CohortForm(request.POST, instance=instance)
    if form.is_valid():
        obj = form.save()
        _audit(request, f"mentorship.cohort_{'update' if pk else 'create'}",
               target_type="mentorship_cohort", target_id=str(obj.pk))
        messages.success(request, "Cohort saved.")
    else:
        messages.error(request, "Couldn't save the cohort — check the values.")
    return redirect("admin_audit:mentorship_cohorts")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_cohort_delete(request: HttpRequest, pk: int) -> HttpResponse:
    MentorshipCohort.objects.filter(pk=pk).delete()
    _audit(request, "mentorship.cohort_delete", target_type="mentorship_cohort", target_id=str(pk))
    messages.success(request, "Cohort removed.")
    return redirect("admin_audit:mentorship_cohorts")


@login_required
@role_required(rbac.ROLE_OFFICER)
def mentorship_tracks(request: HttpRequest) -> HttpResponse:
    tracks = MentorshipTrack.objects.prefetch_related("tasks").all()
    return render(request, "admin_audit/console/mentorship/tracks.html", {
        "tracks": tracks, "track_form": forms.TrackForm(),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_track_save(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    instance = get_object_or_404(MentorshipTrack, pk=pk) if pk else None
    form = forms.TrackForm(request.POST, instance=instance)
    if form.is_valid():
        obj = form.save(commit=False)
        if not obj.key:
            obj.key = _unique_slug(MentorshipTrack, obj.title)
        obj.save()
        _audit(request, f"mentorship.track_{'update' if pk else 'create'}",
               target_type="mentorship_track", target_id=str(obj.pk))
        messages.success(request, "Track saved.")
    else:
        messages.error(request, "Couldn't save the track — check the values.")
    return redirect("admin_audit:mentorship_tracks")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_track_delete(request: HttpRequest, pk: int) -> HttpResponse:
    MentorshipTrack.objects.filter(pk=pk).delete()
    _audit(request, "mentorship.track_delete", target_type="mentorship_track", target_id=str(pk))
    messages.success(request, "Track removed.")
    return redirect("admin_audit:mentorship_tracks")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_track_clone(request: HttpRequest, pk: int) -> HttpResponse:
    track = get_object_or_404(MentorshipTrack, pk=pk)
    tasks = list(track.tasks.all())
    track.pk = None
    track.key = _unique_slug(MentorshipTrack, f"{track.title} copy")
    track.title = f"{track.title} (copy)"
    track.is_core = False
    track.active = False
    track.save()
    for task in tasks:
        task.pk = None
        task.track = track
        task.key = _unique_slug(MentorshipTask, task.title)
        task.save()
    _audit(request, "mentorship.track_clone", target_type="mentorship_track", target_id=str(track.pk))
    messages.success(request, "Track cloned (as a draft).")
    return redirect("admin_audit:mentorship_tracks")


@login_required
@role_required(rbac.ROLE_OFFICER)
def mentorship_track_builder(request: HttpRequest, pk: int) -> HttpResponse:
    track = get_object_or_404(MentorshipTrack, pk=pk)
    return render(request, "admin_audit/console/mentorship/track_builder.html", {
        "track": track,
        "tasks": track.tasks.all(),
        "task_form": forms.TaskForm(initial={"track": track}),
        "validation_choices": MentorshipTask.Validation.choices,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_task_save(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    instance = get_object_or_404(MentorshipTask, pk=pk) if pk else None
    form = forms.TaskForm(request.POST, instance=instance)
    criteria_raw = (request.POST.get("criteria") or "").strip()
    tags_raw = (request.POST.get("tags") or "").strip()
    criteria = {}
    if criteria_raw:
        try:
            criteria = json.loads(criteria_raw)
            if not isinstance(criteria, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            messages.error(request, "Criteria must be a JSON object, e.g. "
                                    '{"type": "skill_min", "skill_type_id": 3300, "level": 3}.')
            return redirect(_task_return(form, instance))
    if form.is_valid():
        obj = form.save(commit=False)
        if not obj.key:
            obj.key = _unique_slug(MentorshipTask, obj.title)
        obj.criteria = criteria
        obj.tags = [t.strip() for t in tags_raw.replace(";", ",").split(",") if t.strip()]
        obj.save()
        _audit(request, f"mentorship.task_{'update' if pk else 'create'}",
               target_type="mentorship_task", target_id=str(obj.pk))
        messages.success(request, "Field exercise saved.")
        return redirect("admin_audit:mentorship_track_builder", pk=obj.track_id)
    messages.error(request, "Couldn't save the exercise — check the values.")
    return redirect(_task_return(form, instance))


def _task_return(form, instance):
    track_id = instance.track_id if instance else (
        form.data.get("track") if form.data.get("track") else None)
    if track_id:
        from django.urls import reverse
        return reverse("admin_audit:mentorship_track_builder", args=[track_id])
    return "admin_audit:mentorship_tracks"


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_task_delete(request: HttpRequest, pk: int) -> HttpResponse:
    task = get_object_or_404(MentorshipTask, pk=pk)
    track_id = task.track_id
    task.delete()
    _audit(request, "mentorship.task_delete", target_type="mentorship_task", target_id=str(pk))
    messages.success(request, "Field exercise removed.")
    return redirect("admin_audit:mentorship_track_builder", pk=track_id)


# ---------------------------------------------------------------------------
# Rewards catalogue (Director): reward rules & badges
# ---------------------------------------------------------------------------
@login_required
@role_required(rbac.ROLE_DIRECTOR)
def mentorship_reward_rules(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_audit/console/mentorship/reward_rules.html", {
        "rules": MentorshipRewardRule.objects.select_related("badge").all(),
        "form": forms.RewardRuleForm(),
        "badges": MentorshipBadge.objects.all(),
        "badge_form": forms.BadgeForm(),
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def mentorship_reward_rule_save(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    instance = get_object_or_404(MentorshipRewardRule, pk=pk) if pk else None
    form = forms.RewardRuleForm(request.POST, instance=instance)
    if form.is_valid():
        obj = form.save(commit=False)
        if not obj.key:
            obj.key = _unique_slug(MentorshipRewardRule, obj.label)
        obj.save()
        _audit(request, f"mentorship.reward_rule_{'update' if pk else 'create'}",
               target_type="mentorship_reward_rule", target_id=str(obj.pk))
        messages.success(request, "Reward rule saved.")
    else:
        messages.error(request, "Couldn't save the reward rule — check the values.")
    return redirect("admin_audit:mentorship_reward_rules")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def mentorship_reward_rule_delete(request: HttpRequest, pk: int) -> HttpResponse:
    MentorshipRewardRule.objects.filter(pk=pk).delete()
    _audit(request, "mentorship.reward_rule_delete", target_type="mentorship_reward_rule",
           target_id=str(pk))
    messages.success(request, "Reward rule removed.")
    return redirect("admin_audit:mentorship_reward_rules")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def mentorship_badge_save(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    instance = get_object_or_404(MentorshipBadge, pk=pk) if pk else None
    form = forms.BadgeForm(request.POST, instance=instance)
    if form.is_valid():
        obj = form.save(commit=False)
        if not obj.key:
            obj.key = _unique_slug(MentorshipBadge, obj.label)
        obj.save()
        _audit(request, f"mentorship.badge_{'update' if pk else 'create'}",
               target_type="mentorship_badge", target_id=str(obj.pk))
        messages.success(request, "Badge saved.")
    else:
        messages.error(request, "Couldn't save the badge — check the values.")
    return redirect("admin_audit:mentorship_reward_rules")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def mentorship_badge_delete(request: HttpRequest, pk: int) -> HttpResponse:
    MentorshipBadge.objects.filter(pk=pk).delete()
    _audit(request, "mentorship.badge_delete", target_type="mentorship_badge", target_id=str(pk))
    messages.success(request, "Badge removed.")
    return redirect("admin_audit:mentorship_reward_rules")


# ---------------------------------------------------------------------------
# Approvals: mentor / mentee applications & pairings
# ---------------------------------------------------------------------------
@login_required
@role_required(rbac.ROLE_OFFICER)
def mentorship_approvals(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_audit/console/mentorship/approvals.html", {
        "mentors": MentorProfile.objects.filter(status=MentorProfile.Status.PENDING)
        .select_related("user").prefetch_related("user__characters"),
        "mentees": MenteeProfile.objects.filter(status=MenteeProfile.Status.PENDING)
        .select_related("user").prefetch_related("user__characters"),
        "pairings": MentorshipPairing.objects.filter(
            status=MentorshipPairing.Status.PENDING_APPROVAL
        ).select_related("mentor__user", "mentee__user"),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_mentor_decide(request: HttpRequest, pk: int) -> HttpResponse:
    profile = get_object_or_404(MentorProfile, pk=pk)
    approve = request.POST.get("decision") == "approve"
    reason = (request.POST.get("reason") or "").strip()
    if approve:
        services.approve_mentor(profile, request.user)
    else:
        services.reject_mentor(profile, request.user, reason)
    _audit(request, "mentorship.mentor_decide", target_type="mentor_profile",
           target_id=str(pk), metadata={"approve": approve})
    messages.success(request, "Mentor application updated.")
    return redirect("admin_audit:mentorship_approvals")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_mentee_decide(request: HttpRequest, pk: int) -> HttpResponse:
    profile = get_object_or_404(MenteeProfile, pk=pk)
    approve = request.POST.get("decision") == "approve"
    reason = (request.POST.get("reason") or "").strip()
    if approve:
        services.approve_mentee(profile, request.user)
    else:
        services.reject_mentee(profile, request.user, reason)
    _audit(request, "mentorship.mentee_decide", target_type="mentee_profile",
           target_id=str(pk), metadata={"approve": approve})
    messages.success(request, "Cadet application updated.")
    return redirect("admin_audit:mentorship_approvals")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_pairing_decide(request: HttpRequest, pk: int) -> HttpResponse:
    pairing = get_object_or_404(MentorshipPairing, pk=pk)
    approve = request.POST.get("decision") == "approve"
    reason = (request.POST.get("reason") or "").strip()
    if approve:
        if not services.approve_pairing(pairing, request.user):
            messages.error(request, "Couldn't activate — the mentor may be at capacity.")
            return redirect("admin_audit:mentorship_approvals")
    else:
        services.cancel_pairing(pairing, request.user, reason or "Rejected by leadership.")
    _audit(request, "mentorship.pairing_decide", target_type="mentorship_pairing",
           target_id=str(pk), metadata={"approve": approve})
    messages.success(request, "Pairing updated.")
    return redirect("admin_audit:mentorship_approvals")


# ---------------------------------------------------------------------------
# Matching worklist
# ---------------------------------------------------------------------------
@login_required
@role_required(rbac.ROLE_OFFICER)
def mentorship_matching(request: HttpRequest) -> HttpResponse:
    from apps.mentorship import onboarding_handoff

    rows = []
    for mentee in matching.unpaired_mentees():
        rows.append({"mentee": mentee, "suggestions": matching.suggest_mentors_for(mentee, limit=3)})
    return render(request, "admin_audit/console/mentorship/matching.html", {
        "rows": rows,
        "suggested": MentorshipPairing.objects.filter(
            status=MentorshipPairing.Status.SUGGESTED
        ).select_related("mentor__user", "mentee__user"),
        "handoff": onboarding_handoff.handoff_candidates(),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_pair_create(request: HttpRequest) -> HttpResponse:
    mentor = get_object_or_404(MentorProfile, pk=request.POST.get("mentor_id"))
    mentee = get_object_or_404(MenteeProfile, pk=request.POST.get("mentee_id"))
    pairing = services.propose_pairing(
        mentor, mentee, actor=request.user,
        initiated_by=MentorshipPairing.InitiatedBy.LEADER,
        status=MentorshipPairing.Status.PENDING_APPROVAL,
    )
    if pairing is None:
        messages.info(request, "Those two already have an open pairing.")
        return redirect("admin_audit:mentorship_matching")
    if request.POST.get("activate") == "1":
        services.approve_pairing(pairing, request.user)
    _audit(request, "mentorship.pair_create", target_type="mentorship_pairing",
           target_id=str(pairing.pk))
    messages.success(request, "Pairing created.")
    return redirect("admin_audit:mentorship_matching")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_auto_suggest(request: HttpRequest) -> HttpResponse:
    created = matching.auto_suggest(limit_per_mentee=1)
    _audit(request, "mentorship.auto_suggest", metadata={"created": created})
    messages.success(request, f"Generated {created} suggestion(s).")
    return redirect("admin_audit:mentorship_matching")


# ---------------------------------------------------------------------------
# Reward approval / payment queue
# ---------------------------------------------------------------------------
@login_required
@role_required(rbac.ROLE_OFFICER)
def mentorship_rewards(request: HttpRequest) -> HttpResponse:
    status = request.GET.get("status") or ""
    role = request.GET.get("role") or ""
    ledger = reporting.reward_ledger(status=status or None, role=role or None)
    return render(request, "admin_audit/console/mentorship/rewards.html", {
        "ledger": ledger[:200],
        "status": status,
        "role": role,
        "statuses": MentorshipRewardLedger.Status.choices,
        "outstanding": rewards.outstanding_isk(),
        "pending": MentorshipRewardLedger.objects.filter(
            status=MentorshipRewardLedger.Status.PENDING_APPROVAL).count(),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_reward_decide(request: HttpRequest, pk: int) -> HttpResponse:
    entry = get_object_or_404(MentorshipRewardLedger, pk=pk)
    approve = request.POST.get("decision") == "approve"
    reason = (request.POST.get("reason") or "").strip()
    try:
        if approve:
            rewards.approve_reward(entry, request.user)
        else:
            rewards.reject_reward(entry, request.user, reason)
    except PermissionDenied:
        _audit(request, "mentorship.reward_decide.denied_self", target_type="mentorship_reward",
               target_id=str(pk))
        messages.error(request, "You can't decide your own reward — another officer must.")
        return redirect("admin_audit:mentorship_rewards")
    _audit(request, "mentorship.reward_decide", target_type="mentorship_reward",
           target_id=str(pk), metadata={"approve": approve})
    messages.success(request, "Reward updated.")
    return redirect("admin_audit:mentorship_rewards")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_reward_pay(request: HttpRequest, pk: int) -> HttpResponse:
    entry = get_object_or_404(MentorshipRewardLedger, pk=pk)
    reference = (request.POST.get("payment_reference") or "").strip()
    try:
        paid = rewards.mark_reward_paid(entry, request.user, reference=reference)
    except PermissionDenied:
        _audit(request, "mentorship.reward_pay.denied_self", target_type="mentorship_reward",
               target_id=str(pk))
        messages.error(request, "You can't pay out your own reward — another officer must.")
        return redirect("admin_audit:mentorship_rewards")
    messages.success(request, "Marked paid." if paid else "That reward isn't ready to pay.")
    _audit(request, "mentorship.reward_paid", target_type="mentorship_reward", target_id=str(pk),
           metadata={"reference": reference})
    return redirect("admin_audit:mentorship_rewards")


# ---------------------------------------------------------------------------
# Reporting & flags
# ---------------------------------------------------------------------------
@login_required
@role_required(rbac.ROLE_OFFICER)
def mentorship_report(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_audit/console/mentorship/report.html", {
        "summary": reporting.program_summary(),
        "track_rates": reporting.track_completion_rates(),
        "mentor_activity": reporting.mentor_activity(),
        "needs_attention": reporting.mentees_needing_attention(),
        "rejected": reporting.commonly_rejected_tasks(),
        "flags": trust.open_flags(),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def mentorship_flag_resolve(request: HttpRequest, pk: int) -> HttpResponse:
    flag = get_object_or_404(MentorshipFlag, pk=pk)
    trust.resolve_flag(flag, request.user)
    _audit(request, "mentorship.flag_resolve", target_type="mentorship_flag", target_id=str(pk))
    messages.success(request, "Flag resolved.")
    return redirect("admin_audit:mentorship_report")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _unique_slug(model, text: str) -> str:
    base = slugify(text)[:60] or "item"
    slug = base
    i = 2
    while model.objects.filter(key=slug).exists():
        slug = f"{base}-{i}"
        i += 1
    return slug
