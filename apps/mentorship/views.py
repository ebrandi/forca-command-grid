"""Pilot-facing Mentorship views (member tier).

Leadership config/approvals/reporting live in the Admin Console
(``apps.admin_audit.console_mentorship``), matching the codebase convention that
leader CRUD is not in the feature app. Every pairing/task mutation re-checks that
the acting user actually belongs to the pairing (IDOR defence); a mentee can never
sign off their own mentor-reviewed task.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from core import rbac
from core.features import feature_required
from core.rbac import role_required

from . import eligibility as elig
from . import forms, matching, models, services, workflow
from .models import (
    MenteeProfile,
    MentorProfile,
    MentorshipPairing,
    MentorshipTaskAssignment,
    MentorshipTrack,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _profiles(user):
    return (
        MentorProfile.objects.filter(user=user).first(),
        MenteeProfile.objects.filter(user=user).first(),
    )


def _role_in_pairing(user, pairing) -> str | None:
    if pairing.mentor.user_id == user.id:
        return "mentor"
    if pairing.mentee.user_id == user.id:
        return "mentee"
    if rbac.has_role(user, rbac.ROLE_OFFICER):
        return "officer"
    return None


def _get_pairing_for(user, pk):
    """Fetch a pairing the user may view, or raise 404 (never leak existence)."""
    from django.http import Http404

    pairing = get_object_or_404(
        MentorshipPairing.objects.select_related("mentor__user", "mentee__user"), pk=pk
    )
    if _role_in_pairing(user, pairing) is None:
        raise Http404("No such pairing.")
    return pairing


# ---------------------------------------------------------------------------
# Landing & dashboard
# ---------------------------------------------------------------------------
@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
def landing(request: HttpRequest) -> HttpResponse:
    program = services.active_program()
    mentor, mentee = _profiles(request.user)
    my_pairings = list(
        MentorshipPairing.objects.filter(services.models_q_user(request.user))
        .exclude(status=MentorshipPairing.Status.EXPIRED)
        .select_related("mentor__user", "mentee__user")
    )
    return render(request, "mentorship/landing.html", {
        "program": program,
        "open": services.program_open(),
        "mentor_profile": mentor,
        "mentee_profile": mentee,
        "my_pairings": my_pairings,
        "tracks": MentorshipTrack.objects.filter(active=True),
        "track_count": MentorshipTrack.objects.filter(active=True).count(),
        "mentor_count": MentorProfile.objects.filter(status=MentorProfile.Status.ACTIVE).count(),
    })


@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
def dashboard(request: HttpRequest) -> HttpResponse:
    """"My Mentorship": my profiles, pairings, suggestions and next actions."""
    mentor, mentee = _profiles(request.user)
    my_pairings = list(
        MentorshipPairing.objects.filter(services.models_q_user(request.user))
        .select_related("mentor__user", "mentee__user")
        .prefetch_related("mentee__user__characters", "mentor__user__characters")
    )
    active = [p for p in my_pairings if p.status == MentorshipPairing.Status.ACTIVE]
    incoming = [
        p for p in my_pairings
        if p.status in (MentorshipPairing.Status.SUGGESTED, MentorshipPairing.Status.REQUESTED)
    ]
    # Suggested mentors for an active mentee with no active pairing.
    suggestions = []
    if mentee and mentee.status == MenteeProfile.Status.ACTIVE and not active:
        suggestions = matching.suggest_mentors_for(mentee, limit=5)
    return render(request, "mentorship/dashboard.html", {
        "program": services.active_program(),
        "mentor_profile": mentor,
        "mentee_profile": mentee,
        "active_pairings": active,
        "incoming": incoming,
        "past_pairings": [p for p in my_pairings if p.status in MentorshipPairing.TERMINAL_STATUSES],
        "suggestions": suggestions,
        "badges": request.user.mentorship_badges.select_related("badge").all(),
    })


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
def register_mentor(request: HttpRequest) -> HttpResponse:
    program = services.active_program()
    existing = MentorProfile.objects.filter(user=request.user).first()
    eligibility = (existing.eligibility if existing else None) or \
        elig.evaluate(request.user, program, "mentor")
    if request.method == "POST":
        if not services.program_open():
            messages.error(request, "The Mentorship Program is currently paused.")
            return redirect("mentorship:landing")
        form = forms.MentorRegistrationForm(request.POST)
        if form.is_valid():
            profile = services.register_mentor(request.user, form.to_data())
            if profile.status == MentorProfile.Status.PENDING:
                from . import notify
                notify.mentor_application(profile)
                messages.success(request, "Mentor registration submitted — pending leadership approval.")
            else:
                messages.success(request, "You're registered as a mentor. Thank you!")
            return redirect("mentorship:dashboard")
    else:
        form = forms.MentorRegistrationForm.from_profile(existing)
    return render(request, "mentorship/register_mentor.html", {
        "form": form, "program": program, "eligibility": eligibility, "existing": existing,
    })


@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
def register_mentee(request: HttpRequest) -> HttpResponse:
    program = services.active_program()
    existing = MenteeProfile.objects.filter(user=request.user).first()
    eligibility = (existing.eligibility if existing else None) or \
        elig.evaluate(request.user, program, "mentee")
    if request.method == "POST":
        if not services.program_open():
            messages.error(request, "The Mentorship Program is currently paused.")
            return redirect("mentorship:landing")
        form = forms.MenteeRegistrationForm(request.POST)
        if form.is_valid():
            profile = services.register_mentee(request.user, form.to_data())
            if profile.status == MenteeProfile.Status.PENDING:
                from . import notify
                notify.mentee_application(profile)
                messages.success(request, "Cadet registration submitted — pending leadership approval.")
            else:
                messages.success(request, "You're registered as a cadet. Let's find you a mentor!")
            return redirect("mentorship:dashboard")
    else:
        form = forms.MenteeRegistrationForm.from_profile(existing)
    return render(request, "mentorship/register_mentee.html", {
        "form": form, "program": program, "eligibility": eligibility, "existing": existing,
    })


# ---------------------------------------------------------------------------
# Tracks (catalogue)
# ---------------------------------------------------------------------------
@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
def tracks(request: HttpRequest) -> HttpResponse:
    track_list = list(
        MentorshipTrack.objects.filter(active=True).prefetch_related("tasks")
    )
    for t in track_list:
        t.active_tasks = [task for task in t.tasks.all() if task.active]
    return render(request, "mentorship/tracks.html", {"tracks": track_list})


@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
def track_detail(request: HttpRequest, key: str) -> HttpResponse:
    track = get_object_or_404(MentorshipTrack, key=key, active=True)
    tasks_qs = track.tasks.filter(active=True).prefetch_related("prerequisites__requires")
    return render(request, "mentorship/track_detail.html", {"track": track, "tasks": tasks_qs})


# ---------------------------------------------------------------------------
# Mentor directory & pairing initiation
# ---------------------------------------------------------------------------
@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
def mentor_directory(request: HttpRequest) -> HttpResponse:
    program = services.active_program()
    if not program.mentor_directory_visible:
        messages.info(request, "The mentor directory is not currently open.")
        return redirect("mentorship:landing")
    mentee = MenteeProfile.objects.filter(user=request.user).first()
    mentors = (
        MentorProfile.objects.filter(status=MentorProfile.Status.ACTIVE)
        .select_related("user").prefetch_related("user__characters")
    )
    rows = []
    for m in mentors:
        rows.append({
            "mentor": m,
            "has_capacity": services.mentor_has_capacity(m),
            "active": services.mentor_active_count(m),
            "capacity": services.mentor_capacity(m),
            "already": bool(mentee and services.existing_open_pairing(m, mentee)),
        })
    return render(request, "mentorship/directory.html", {
        "rows": rows, "mentee": mentee, "program": program,
        "can_request": bool(mentee and mentee.status == MenteeProfile.Status.ACTIVE
                            and program.allow_mentee_initiated),
    })


@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
@require_POST
def request_mentor(request: HttpRequest) -> HttpResponse:
    program = services.active_program()
    mentee = MenteeProfile.objects.filter(user=request.user, status=MenteeProfile.Status.ACTIVE).first()
    if not mentee or not program.allow_mentee_initiated:
        messages.error(request, "You can't request a mentor right now.")
        return redirect("mentorship:directory")
    mentor = get_object_or_404(MentorProfile, pk=request.POST.get("mentor_id"),
                               status=MentorProfile.Status.ACTIVE)
    if not services.mentor_has_capacity(mentor):
        messages.error(request, "That mentor is at capacity.")
        return redirect("mentorship:directory")
    pairing = services.propose_pairing(
        mentor, mentee, actor=request.user,
        initiated_by=MentorshipPairing.InitiatedBy.MENTEE,
        status=MentorshipPairing.Status.REQUESTED,
    )
    if pairing is None:
        messages.info(request, "You already have a pending or active pairing with that mentor.")
    else:
        messages.success(request, f"Request sent to {mentor.user.display_name}.")
    return redirect("mentorship:dashboard")


@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
@require_POST
def invite_mentee(request: HttpRequest) -> HttpResponse:
    program = services.active_program()
    mentor = MentorProfile.objects.filter(user=request.user, status=MentorProfile.Status.ACTIVE).first()
    if not mentor or not program.allow_mentor_initiated:
        messages.error(request, "You can't invite a cadet right now.")
        return redirect("mentorship:dashboard")
    if not services.mentor_has_capacity(mentor):
        messages.error(request, "You're already at your mentee capacity.")
        return redirect("mentorship:dashboard")
    mentee = get_object_or_404(MenteeProfile, pk=request.POST.get("mentee_id"),
                               status=MenteeProfile.Status.ACTIVE)
    pairing = services.propose_pairing(
        mentor, mentee, actor=request.user,
        initiated_by=MentorshipPairing.InitiatedBy.MENTOR,
        status=MentorshipPairing.Status.REQUESTED,
    )
    if pairing is None:
        messages.info(request, "You already have a pending or active pairing with that cadet.")
    else:
        messages.success(request, f"Invitation sent to {mentee.user.display_name}.")
    return redirect("mentorship:dashboard")


@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
@require_POST
def pairing_respond(request: HttpRequest, pk: int) -> HttpResponse:
    """The counterparty accepts or declines a suggested/requested pairing."""
    pairing = _get_pairing_for(request.user, pk)
    role = _role_in_pairing(request.user, pairing)
    decision = request.POST.get("decision")
    if pairing.status not in (MentorshipPairing.Status.SUGGESTED, MentorshipPairing.Status.REQUESTED):
        messages.error(request, "That pairing can no longer be answered.")
        return redirect("mentorship:dashboard")
    # The initiator can't accept on the other's behalf.
    initiator_role = {"mentor": "mentor", "mentee": "mentee"}.get(pairing.initiated_by)
    if decision == "accept":
        if role == initiator_role and pairing.status == MentorshipPairing.Status.REQUESTED:
            messages.error(request, "The other pilot needs to accept this one.")
            return redirect("mentorship:dashboard")
        outcome = services.activate_or_queue(pairing, actor=request.user)
        if outcome == "pending":
            from . import notify
            notify.pairing_pending(pairing)
            messages.success(request, "Accepted — now awaiting leadership approval.")
        else:
            messages.success(request, "Mentorship is now active. Fair winds!")
    else:
        services.cancel_pairing(pairing, request.user, reason="Declined by pilot.")
        messages.info(request, "Pairing declined.")
    return redirect("mentorship:dashboard")


# ---------------------------------------------------------------------------
# Pairing detail (the shared dashboard)
# ---------------------------------------------------------------------------
@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
def pairing_detail(request: HttpRequest, pk: int) -> HttpResponse:
    pairing = _get_pairing_for(request.user, pk)
    role = _role_in_pairing(request.user, pairing)
    progress = workflow.pairing_progress(pairing)
    total = sum(r["total"] for r in progress)
    done = sum(r["done"] for r in progress)
    rewards_qs = pairing.rewards.select_related("rule", "recipient").order_by("-created_at")
    next_actions = _next_actions(pairing, role)
    return render(request, "mentorship/pairing_detail.html", {
        "pairing": pairing,
        "role": role,
        "progress": progress,
        "overall_pct": int(round(100 * done / total)) if total else 0,
        "total_tasks": total,
        "done_tasks": done,
        "sessions": pairing.sessions.all()[:10],
        "events": pairing.events.select_related("actor")[:15],
        "rewards": rewards_qs,
        "flags": pairing.flags.filter(resolved=False) if role == "officer" else None,
        "enrollable_tracks": _enrollable_tracks(pairing),
        "next_actions": next_actions,
        "is_participant": role in ("mentor", "mentee"),
    })


def _enrollable_tracks(pairing):
    enrolled = set(pairing.enrollments.values_list("track_id", flat=True))
    return MentorshipTrack.objects.filter(active=True).exclude(id__in=enrolled)


def _next_actions(pairing, role) -> list[str]:
    out = []
    if pairing.status == MentorshipPairing.Status.ACTIVE:
        pending_mentor = pairing.assignments.filter(
            status=MentorshipTaskAssignment.Status.PENDING_MENTOR).count()
        if role in ("mentor", "officer") and pending_mentor:
            out.append(f"{pending_mentor} task(s) awaiting your sign-off.")
        todo = pairing.assignments.filter(
            status__in=[MentorshipTaskAssignment.Status.NOT_STARTED,
                        MentorshipTaskAssignment.Status.IN_PROGRESS]).count()
        if role == "mentee" and todo:
            out.append(f"{todo} field exercise(s) to work on.")
        if not pairing.sessions.exists():
            out.append("Schedule your first mentoring session.")
    return out


# ---------------------------------------------------------------------------
# Task actions
# ---------------------------------------------------------------------------
@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
@require_POST
def task_action(request: HttpRequest, pk: int) -> HttpResponse:
    assignment = get_object_or_404(
        MentorshipTaskAssignment.objects.select_related(
            "task", "pairing__mentor__user", "pairing__mentee__user"), pk=pk
    )
    pairing = assignment.pairing
    role = _role_in_pairing(request.user, pairing)
    if role is None:
        from django.http import Http404
        raise Http404("No such task.")
    action = request.POST.get("action")
    reason = (request.POST.get("reason") or "").strip()

    if action == "start" and role in ("mentor", "mentee"):
        workflow.start_task(assignment, request.user)
    elif action == "submit" and role in ("mentor", "mentee"):
        evidence = None
        url = (request.POST.get("evidence_url") or "").strip()
        note = (request.POST.get("evidence_text") or "").strip()
        if url or note:
            evidence = workflow.add_evidence(
                assignment, request.user,
                kind=models.MentorshipEvidence.Kind.LINK if url else models.MentorshipEvidence.Kind.NOTE,
                url=url, text=note)
        result = workflow.mentee_submit(assignment, request.user, evidence=evidence)
        if result == "needs_evidence":
            messages.error(request, "This exercise needs an evidence link or note.")
        elif result == "blocked":
            messages.error(request, "Finish the prerequisite exercises first.")
        elif result == "completed":
            messages.success(request, "Exercise completed!")
        else:
            messages.success(request, "Submitted for review.")
    elif action in ("confirm", "reject"):
        # Only the mentor (or an officer) may sign off — never the mentee.
        if role not in ("mentor", "officer"):
            messages.error(request, "Only the mentor can sign this off.")
            return redirect("mentorship:pairing", pk=pairing.pk)
        workflow.mentor_decide(assignment, request.user, approve=(action == "confirm"), reason=reason)
        messages.success(request, "Exercise confirmed." if action == "confirm" else "Exercise sent back.")
    elif action == "repeat" and role in ("mentor", "mentee"):
        if workflow.create_repeat(assignment, request.user):
            messages.success(request, "Opened another attempt.")
        else:
            messages.info(request, "Can't repeat this exercise yet.")
    else:
        messages.error(request, "That action isn't available.")
    return redirect("mentorship:pairing", pk=pairing.pk)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
@require_POST
def session_create(request: HttpRequest, pk: int) -> HttpResponse:
    pairing = _get_pairing_for(request.user, pk)
    if _role_in_pairing(request.user, pairing) not in ("mentor", "mentee"):
        messages.error(request, "Only the pair can schedule a session.")
        return redirect("mentorship:pairing", pk=pk)
    when = parse_datetime(request.POST.get("scheduled_at") or "")
    if when and timezone.is_naive(when):
        when = timezone.make_aware(when)
    track = None
    if request.POST.get("track_id"):
        track = MentorshipTrack.objects.filter(pk=request.POST["track_id"]).first()
    services.schedule_session(
        pairing, actor=request.user,
        topic=(request.POST.get("topic") or "").strip(),
        scheduled_at=when,
        duration_minutes=int(request.POST.get("duration_minutes") or 30),
        track=track, location_hint=(request.POST.get("location_hint") or "").strip(),
    )
    messages.success(request, "Session scheduled.")
    return redirect("mentorship:pairing", pk=pk)


@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
@require_POST
def session_action(request: HttpRequest, pk: int) -> HttpResponse:
    session = get_object_or_404(
        models.MentorshipSession.objects.select_related(
            "pairing__mentor__user", "pairing__mentee__user"), pk=pk)
    if _role_in_pairing(request.user, session.pairing) not in ("mentor", "mentee", "officer"):
        from django.http import Http404
        raise Http404("No such session.")
    action = request.POST.get("action")
    if action == "confirm":
        services.confirm_session(session, request.user)
        if session.status == models.MentorshipSession.Status.COMPLETED:
            from . import rewards
            rewards.on_session_confirmed(session)
        messages.success(request, "Session confirmed.")
    elif action == "cancel":
        services.cancel_session(session, request.user)
        messages.info(request, "Session cancelled.")
    return redirect("mentorship:pairing", pk=session.pairing_id)


# ---------------------------------------------------------------------------
# Enrol extra track / pairing lifecycle by participants
# ---------------------------------------------------------------------------
@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
@require_POST
def enroll_track(request: HttpRequest, pk: int) -> HttpResponse:
    pairing = _get_pairing_for(request.user, pk)
    if _role_in_pairing(request.user, pairing) not in ("mentor", "mentee", "officer"):
        from django.http import Http404
        raise Http404()
    if pairing.status != MentorshipPairing.Status.ACTIVE:
        messages.error(request, "The pairing must be active to add tracks.")
        return redirect("mentorship:pairing", pk=pk)
    track = get_object_or_404(MentorshipTrack, pk=request.POST.get("track_id"), active=True)
    services.enroll_track(pairing, track, actor=request.user)
    messages.success(request, f"Enrolled in “{track.title}”.")
    return redirect("mentorship:pairing", pk=pk)


@login_required
@feature_required("mentorship")
@role_required(rbac.ROLE_MEMBER)
@require_POST
def pairing_action(request: HttpRequest, pk: int) -> HttpResponse:
    pairing = _get_pairing_for(request.user, pk)
    role = _role_in_pairing(request.user, pairing)
    if role not in ("mentor", "mentee", "officer"):
        from django.http import Http404
        raise Http404()
    action = request.POST.get("action")
    reason = (request.POST.get("reason") or "").strip()
    if action == "pause":
        services.pause_pairing(pairing, request.user, reason)
        messages.info(request, "Mentorship paused.")
    elif action == "resume" and pairing.status == MentorshipPairing.Status.PAUSED:
        services.set_status(pairing, MentorshipPairing.Status.ACTIVE, actor=request.user,
                            detail="Resumed.")
        messages.success(request, "Mentorship resumed.")
    elif action == "complete" and role in ("mentor", "officer"):
        services.complete_pairing(pairing, request.user, reason)
        messages.success(request, "Mentorship marked complete. Well done!")
    elif action == "cancel":
        services.cancel_pairing(pairing, request.user, reason)
        messages.info(request, "Mentorship cancelled.")
    else:
        messages.error(request, "That action isn't available.")
    return redirect("mentorship:dashboard")
