"""Mentorship services: program config, registration/approval, pairing lifecycle,
track enrolment and sessions.

Business rules live here, not in views (mirrors ``apps.srp.services``). The task
progression state machine is in ``workflow.py`` and reward logic in ``rewards.py``.
Every state change that matters is written to an append-only
``MentorshipPairingEvent`` for the audit trail; sensitive admin actions also go
through ``core.audit.audit_log`` at the view layer.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from . import eligibility
from .models import (
    MenteeProfile,
    MentorProfile,
    MentorshipCohort,
    MentorshipEnrollment,
    MentorshipPairing,
    MentorshipPairingEvent,
    MentorshipProgram,
    MentorshipSession,
    MentorshipSessionParticipant,
    MentorshipTrack,
)


# ---------------------------------------------------------------------------
# Program config (singleton, seeded on first use — mirrors SrpProgram)
# ---------------------------------------------------------------------------
def active_program() -> MentorshipProgram:
    program = MentorshipProgram.objects.filter(is_active=True).order_by("-updated_at").first()
    if program is None:
        program = MentorshipProgram.objects.create(name="Mentorship Program", is_active=True)
    return program


def program_open() -> bool:
    """True when pilots can currently participate."""
    p = active_program()
    return bool(p.is_active and p.enabled)


def active_cohort() -> MentorshipCohort | None:
    p = active_program()
    if p.active_cohort_id:
        return p.active_cohort
    return MentorshipCohort.objects.filter(is_active=True).order_by("-starts_on").first()


# ---------------------------------------------------------------------------
# Registration & approval
# ---------------------------------------------------------------------------
def _pick_character(user):
    chars = list(user.characters.all())
    if not chars:
        return None
    return next((c for c in chars if c.is_main), chars[0])


def register_mentor(user, data: dict) -> MentorProfile:
    """Create or update a mentor registration, computing eligibility.

    Enters PENDING when leadership approval is required, else ACTIVE.
    """
    program = active_program()
    elig = eligibility.evaluate(user, program, "mentor")
    profile, _ = MentorProfile.objects.get_or_create(user=user)
    profile.character = _pick_character(user)
    for field in ("areas", "timezone", "play_windows", "languages", "comms",
                  "max_active_mentees", "open_to_adhoc", "bio", "restrictions"):
        if field in data:
            setattr(profile, field, data[field])
    profile.eligibility = elig
    profile.cohort = active_cohort()
    # Re-registering from a rejected/retired state re-enters the queue.
    if profile.status in (MentorProfile.Status.DRAFT, MentorProfile.Status.REJECTED,
                          MentorProfile.Status.RETIRED) or not profile.applied_at:
        profile.applied_at = timezone.now()
        profile.status = (
            MentorProfile.Status.PENDING if program.mentor_requires_approval
            else MentorProfile.Status.ACTIVE
        )
        if profile.status == MentorProfile.Status.ACTIVE:
            profile.approved_at = timezone.now()
    profile.save()
    return profile


def register_mentee(user, data: dict) -> MenteeProfile:
    program = active_program()
    elig = eligibility.evaluate(user, program, "mentee")
    profile, _ = MenteeProfile.objects.get_or_create(user=user)
    profile.character = _pick_character(user)
    for field in ("goals", "experience", "timezone", "play_windows", "languages",
                  "interests", "ships_can_fly", "needs_skill_help", "needs_fitting_help",
                  "voice_comfortable", "notes"):
        if field in data:
            setattr(profile, field, data[field])
    profile.eligibility = elig
    profile.cohort = active_cohort()
    if profile.status in (MenteeProfile.Status.DRAFT, MenteeProfile.Status.REJECTED) or not profile.applied_at:
        profile.applied_at = timezone.now()
        profile.status = (
            MenteeProfile.Status.PENDING if program.mentee_requires_approval
            else MenteeProfile.Status.ACTIVE
        )
        if profile.status == MenteeProfile.Status.ACTIVE:
            profile.approved_at = timezone.now()
    profile.save()
    return profile


def approve_mentor(profile: MentorProfile, officer) -> bool:
    if profile.status != MentorProfile.Status.PENDING:
        return False
    profile.status = MentorProfile.Status.ACTIVE
    profile.approved_at = timezone.now()
    profile.approved_by = officer
    profile.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])
    return True


def reject_mentor(profile: MentorProfile, officer, reason: str = "") -> bool:
    if profile.status not in (MentorProfile.Status.PENDING, MentorProfile.Status.ACTIVE):
        return False
    profile.status = MentorProfile.Status.REJECTED
    profile.reject_reason = reason[:240]
    profile.approved_by = officer
    profile.save(update_fields=["status", "reject_reason", "approved_by", "updated_at"])
    return True


def approve_mentee(profile: MenteeProfile, officer) -> bool:
    if profile.status != MenteeProfile.Status.PENDING:
        return False
    profile.status = MenteeProfile.Status.ACTIVE
    profile.approved_at = timezone.now()
    profile.approved_by = officer
    profile.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])
    return True


def reject_mentee(profile: MenteeProfile, officer, reason: str = "") -> bool:
    if profile.status not in (MenteeProfile.Status.PENDING, MenteeProfile.Status.ACTIVE):
        return False
    profile.status = MenteeProfile.Status.REJECTED
    profile.reject_reason = reason[:240]
    profile.approved_by = officer
    profile.save(update_fields=["status", "reject_reason", "approved_by", "updated_at"])
    return True


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------
def mentor_capacity(mentor: MentorProfile) -> int:
    if mentor.max_active_mentees and mentor.max_active_mentees > 0:
        return mentor.max_active_mentees
    return active_program().max_active_mentees_per_mentor


def mentor_active_count(mentor: MentorProfile) -> int:
    return mentor.pairings.filter(
        status__in=MentorshipPairing.CAPACITY_STATUSES
    ).count()


def mentor_has_capacity(mentor: MentorProfile) -> bool:
    return mentor_active_count(mentor) < mentor_capacity(mentor)


# ---------------------------------------------------------------------------
# Pairing lifecycle
# ---------------------------------------------------------------------------
def _log_event(pairing, *, actor=None, kind=MentorshipPairingEvent.Kind.STATUS,
               from_status="", to_status="", detail=""):
    MentorshipPairingEvent.objects.create(
        pairing=pairing, actor=actor, kind=kind,
        from_status=from_status, to_status=to_status, detail=detail[:300],
    )


def existing_open_pairing(mentor: MentorProfile, mentee: MenteeProfile):
    return mentor.pairings.filter(
        mentee=mentee, status__in=MentorshipPairing.OPEN_STATUSES
    ).first()


def propose_pairing(mentor: MentorProfile, mentee: MenteeProfile, *, actor=None,
                    initiated_by, status=None, score=None, reasons=None) -> MentorshipPairing | None:
    """Create a pairing in a pre-active state. Returns None if one already exists."""
    # A pilot can never mentor themselves — defence-in-depth against a self-pairing being used to
    # self-endorse a career milestone (capsuleer findings 6/7).
    if getattr(mentor, "user_id", None) is not None and mentor.user_id == getattr(
        mentee, "user_id", None
    ):
        return None
    if existing_open_pairing(mentor, mentee):
        return None
    status = status or MentorshipPairing.Status.SUGGESTED
    pairing = MentorshipPairing.objects.create(
        mentor=mentor, mentee=mentee, status=status, initiated_by=initiated_by,
        match_score=score, match_reasons=reasons or [], cohort=active_cohort(),
    )
    _log_event(pairing, actor=actor, to_status=status,
               detail=f"Proposed ({pairing.get_initiated_by_display()}).")
    # Notify the counterparty (auto-suggest, request or invite) so a fresh pairing
    # never sits unseen. Best-effort — never let a notification block the proposal.
    from . import notify

    notify.pairing_proposed(pairing)
    return pairing


@transaction.atomic
def set_status(pairing: MentorshipPairing, to_status: str, *, actor=None, detail="") -> bool:
    """Move a pairing to ``to_status`` with a row lock + audit event.

    Capacity is enforced when moving into ACTIVE/PENDING_APPROVAL. Returns False on
    a no-op (already there) or a blocked transition.
    """
    locked = MentorshipPairing.objects.select_for_update().get(pk=pairing.pk)
    if locked.status == to_status:
        return False
    if to_status in (MentorshipPairing.Status.ACTIVE, MentorshipPairing.Status.PENDING_APPROVAL):
        # Only count capacity when entering from a non-capacity state.
        if locked.status not in MentorshipPairing.CAPACITY_STATUSES and not mentor_has_capacity(locked.mentor):
            return False
    from_status = locked.status
    locked.status = to_status
    if to_status == MentorshipPairing.Status.ACTIVE and not locked.started_at:
        locked.started_at = timezone.now()
        locked.last_activity_at = timezone.now()
    if to_status in MentorshipPairing.TERMINAL_STATUSES:
        locked.ended_at = timezone.now()
    if to_status == MentorshipPairing.Status.ACTIVE:
        locked.approved_by = actor or locked.approved_by
    locked.save(update_fields=["status", "started_at", "ended_at", "approved_by",
                               "last_activity_at", "updated_at"])
    _log_event(locked, actor=actor, from_status=from_status, to_status=to_status, detail=detail)
    if to_status == MentorshipPairing.Status.ACTIVE:
        _on_activated(locked, actor)
    pairing.status = to_status
    return True


def _on_activated(pairing: MentorshipPairing, actor):
    """Auto-enrol the core tracks and materialise their tasks."""
    from . import workflow

    for track in MentorshipTrack.objects.filter(active=True, is_core=True):
        enroll_track(pairing, track, actor=actor, log=False)
    workflow.materialize_pairing(pairing)


def approve_pairing(pairing: MentorshipPairing, officer) -> bool:
    """Leadership approves a pending pairing → ACTIVE."""
    return set_status(pairing, MentorshipPairing.Status.ACTIVE, actor=officer,
                      detail="Approved by leadership.")


def activate_or_queue(pairing: MentorshipPairing, *, actor=None) -> str:
    """Accept a request: go straight ACTIVE, or PENDING_APPROVAL if configured."""
    program = active_program()
    if program.pairing_requires_approval:
        set_status(pairing, MentorshipPairing.Status.PENDING_APPROVAL, actor=actor,
                   detail="Awaiting leadership approval.")
        return "pending"
    set_status(pairing, MentorshipPairing.Status.ACTIVE, actor=actor, detail="Auto-activated.")
    return "active"


def pause_pairing(pairing, actor, reason="") -> bool:
    ok = set_status(pairing, MentorshipPairing.Status.PAUSED, actor=actor, detail=reason)
    if ok:
        pairing.pause_reason = reason[:200]
        pairing.save(update_fields=["pause_reason", "updated_at"])
    return ok


def complete_pairing(pairing, actor, note="") -> bool:
    ok = set_status(pairing, MentorshipPairing.Status.COMPLETED, actor=actor, detail=note)
    if ok:
        pairing.completion_note = note[:240]
        pairing.save(update_fields=["completion_note", "updated_at"])
        from . import rewards
        rewards.on_program_completed(pairing)
    return ok


def cancel_pairing(pairing, actor, reason="") -> bool:
    return set_status(pairing, MentorshipPairing.Status.CANCELLED, actor=actor, detail=reason)


# ---------------------------------------------------------------------------
# Track enrolment
# ---------------------------------------------------------------------------
def enroll_track(pairing: MentorshipPairing, track: MentorshipTrack, *, actor=None,
                 log=True) -> MentorshipEnrollment:
    enrollment, created = MentorshipEnrollment.objects.get_or_create(
        pairing=pairing, track=track,
        defaults={"status": MentorshipEnrollment.Status.ACTIVE},
    )
    if created:
        from . import workflow
        workflow.materialize_track(pairing, track)
        if log:
            _log_event(pairing, actor=actor, kind=MentorshipPairingEvent.Kind.SYSTEM,
                       detail=f"Enrolled in track: {track.title}")
    return enrollment


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def schedule_session(pairing, *, actor, topic="", scheduled_at=None, duration_minutes=30,
                     track=None, location_hint="", location_system_id=None) -> MentorshipSession:
    session = MentorshipSession.objects.create(
        pairing=pairing, created_by=actor, topic=topic[:160], scheduled_at=scheduled_at,
        duration_minutes=duration_minutes or 30, track=track,
        location_hint=location_hint[:120], location_system_id=location_system_id,
    )
    MentorshipSessionParticipant.objects.get_or_create(
        session=session, user=pairing.mentor.user,
        defaults={"role": MentorshipSessionParticipant.Role.MENTOR},
    )
    MentorshipSessionParticipant.objects.get_or_create(
        session=session, user=pairing.mentee.user,
        defaults={"role": MentorshipSessionParticipant.Role.MENTEE},
    )
    pairing.touch_activity()
    pairing.save(update_fields=["last_activity_at", "updated_at"])
    _log_event(pairing, actor=actor, kind=MentorshipPairingEvent.Kind.SYSTEM,
               detail=f"Session scheduled: {topic or 'mentoring'}")
    return session


def confirm_session(session: MentorshipSession, user) -> bool:
    part = session.participants.filter(user=user).first()
    if part is None:
        return False
    if not part.confirmed:
        part.confirmed = True
        part.confirmed_at = timezone.now()
        part.save(update_fields=["confirmed", "confirmed_at"])
    # When both mentor & mentee have confirmed, mark the session completed.
    confirmed = session.participants.filter(confirmed=True).count()
    if confirmed >= 2 and session.status == MentorshipSession.Status.SCHEDULED:
        session.status = MentorshipSession.Status.COMPLETED
        session.save(update_fields=["status", "updated_at"])
    session.pairing.touch_activity()
    session.pairing.save(update_fields=["last_activity_at", "updated_at"])
    return True


def cancel_session(session: MentorshipSession, actor) -> bool:
    if session.status in (MentorshipSession.Status.COMPLETED, MentorshipSession.Status.CANCELLED):
        return False
    session.status = MentorshipSession.Status.CANCELLED
    session.save(update_fields=["status", "updated_at"])
    return True


# ---------------------------------------------------------------------------
# Small query helpers used by views
# ---------------------------------------------------------------------------
def pairings_for_user(user):
    """Every pairing where the user is the mentor or the mentee."""
    return MentorshipPairing.objects.filter(
        models_q_user(user)
    ).select_related("mentor__user", "mentee__user")


def models_q_user(user):
    from django.db.models import Q

    return Q(mentor__user=user) | Q(mentee__user=user)


def user_is_in_pairing(user, pairing) -> bool:
    return pairing.mentor.user_id == user.id or pairing.mentee.user_id == user.id
