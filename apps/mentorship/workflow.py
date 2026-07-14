"""Task assignment lifecycle & validation state machine.

A task declares *how* it is validated (``validation_method``) and, for auto
checks, *what* to check (``criteria`` → ``validation.py``). This module drives an
assignment through the pending states to a terminal ``COMPLETED`` /
``COMPLETED_UNREWARDABLE`` / ``REJECTED`` / ``WAIVED`` and fires rewards.

Key honesty rule: **"completed for learning" is separate from "eligible for
reward"**. ``_complete`` always records the completion; whether it is
``rewardable`` depends on the programme policy (``esi_validation_required`` /
``allow_unverified_rewards``) and whether the task was actually verified.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import messages as msg
from . import validation
from .models import (
    MentorshipEnrollment,
    MentorshipEvidence,
    MentorshipTask,
    MentorshipTaskAssignment,
    MentorshipTaskValidation,
)

_A = MentorshipTaskAssignment.Status
_V = MentorshipTaskValidation

CONF_MENTEE = 30
CONF_MENTOR = 70
CONF_LEADERSHIP = 90
VERIFY_THRESHOLD = 60  # auto-check confidence needed to count as "verified"

# Criteria whose data comes from Command Grid's own workflows rather than a raw
# ESI read — recorded as an INTERNAL (not API) validation source.
_INTERNAL_TYPES = {
    "fleet_attended", "contribution_kind", "courier_contract", "buyback_offer",
    "mining_ledger", "industry_job", "session_confirmed", "skill_plan_exists",
}


# ---------------------------------------------------------------------------
# Materialisation
# ---------------------------------------------------------------------------
def materialize_track(pairing, track) -> int:
    """Create the (pairing, task) assignment rows for a track's active tasks."""
    created = 0
    for task in track.tasks.filter(active=True):
        _, was_created = MentorshipTaskAssignment.objects.get_or_create(
            pairing=pairing, task=task, repeat_index=0,
            defaults={"sort_order": task.sort_order},
        )
        created += int(was_created)
    return created


def materialize_pairing(pairing) -> int:
    total = 0
    for enrollment in pairing.enrollments.filter(status=MentorshipEnrollment.Status.ACTIVE):
        total += materialize_track(pairing, enrollment.track)
    return total


# ---------------------------------------------------------------------------
# Prerequisites & repeats
# ---------------------------------------------------------------------------
def prerequisites_met(assignment) -> bool:
    prereq_ids = list(
        assignment.task.prerequisites.values_list("requires_id", flat=True)
    )
    if not prereq_ids:
        return True
    done = set(
        MentorshipTaskAssignment.objects.filter(
            pairing=assignment.pairing, task_id__in=prereq_ids,
            status__in=MentorshipTaskAssignment.DONE_STATUSES,
        ).values_list("task_id", flat=True)
    )
    return set(prereq_ids).issubset(done)


def create_repeat(assignment, actor=None) -> MentorshipTaskAssignment | None:
    """Open the next occurrence of a repeatable task (respecting cooldown/max)."""
    task = assignment.task
    if not task.repeatable:
        return None
    if assignment.cooldown_until and assignment.cooldown_until > timezone.now():
        return None
    next_index = assignment.repeat_index + 1
    if next_index >= max(1, task.max_repeats):
        return None
    obj, created = MentorshipTaskAssignment.objects.get_or_create(
        pairing=assignment.pairing, task=task, repeat_index=next_index,
        defaults={"assigned_by": actor, "sort_order": task.sort_order},
    )
    return obj if created else None


# ---------------------------------------------------------------------------
# Validation records
# ---------------------------------------------------------------------------
def _record(assignment, source, result, confidence=0, detail="", actor=None, evidence=None,
            key="", params=None):
    """Append a validation row.

    Seam B (see ``messages.py``): a *system* detail is stored as a scaffold ``key`` + plain-JSON
    ``params``, with the English msgid written to ``detail`` as the audit record and the fallback.
    A mentor's or officer's free-text note is stored verbatim with no key — it is that person's own
    words and is never machine-translated. Both are read back by the *other* party under *their*
    locale, which is why ``_()`` at this write site would be worse than useless.
    """
    if key:
        detail = msg.english(key, params) or detail
    return MentorshipTaskValidation.objects.create(
        assignment=assignment, source=source, result=result,
        confidence=confidence, detail=detail[:300], actor=actor, evidence=evidence,
        detail_key=key, detail_params=params or {},
    )


def _set_reason(assignment, reason="", key="", params=None) -> None:
    """Stamp ``last_reason`` (+ its scaffold key/params) on an assignment, in memory."""
    if key:
        reason = msg.english(key, params) or reason
    assignment.last_reason = reason[:300]
    assignment.last_reason_key = key
    assignment.last_reason_params = params or {}


# ``last_reason`` is always saved together with its two Seam B companions.
_REASON_FIELDS = ["last_reason", "last_reason_key", "last_reason_params"]


def _has_pass(assignment, source) -> bool:
    return assignment.validations.filter(source=source, result=_V.Result.PASS).exists()


def _best_auto_confidence(assignment) -> int:
    row = (
        assignment.validations.filter(
            source__in=[_V.Source.API, _V.Source.INTERNAL], result=_V.Result.PASS
        )
        .order_by("-confidence")
        .first()
    )
    return row.confidence if row else 0


def run_auto_check(assignment):
    """Run the criteria auto-check and record it. Returns the Outcome (or None)."""
    outcome = validation.run(assignment)
    if outcome is None:
        return None
    ctype = (assignment.task.criteria or {}).get("type")
    source = _V.Source.INTERNAL if ctype in _INTERNAL_TYPES else _V.Source.API
    if outcome.passed:
        result = _V.Result.PASS
    elif outcome.partial:
        result = _V.Result.PENDING
    else:
        result = _V.Result.FAIL
    _record(assignment, source, result, confidence=outcome.confidence, detail=outcome.detail,
            key=outcome.key, params=outcome.params)
    if outcome.passed and outcome.confidence > assignment.confidence:
        assignment.confidence = outcome.confidence
        assignment.save(update_fields=["confidence", "updated_at"])
    return outcome


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------
def _touch(pairing):
    pairing.touch_activity()
    pairing.save(update_fields=["last_activity_at", "updated_at"])


@transaction.atomic
def _complete(assignment, actor, confidence, verified, reason="", reason_key="", reason_params=None):
    from . import rewards, services

    locked = MentorshipTaskAssignment.objects.select_for_update().get(pk=assignment.pk)
    if locked.status in MentorshipTaskAssignment.DONE_STATUSES:
        return locked
    program = services.active_program()
    task = locked.task
    locked.confidence = max(locked.confidence, confidence)

    if not task.reward_eligible:
        status, rewardable = _A.COMPLETED, False
    elif program.esi_validation_required and not verified:
        status, rewardable = _A.COMPLETED_UNREWARDABLE, False
    elif not program.allow_unverified_rewards and not verified:
        status, rewardable = _A.COMPLETED_UNREWARDABLE, False
    else:
        status, rewardable = _A.COMPLETED, True

    now = timezone.now()
    locked.status = status
    locked.rewardable = rewardable
    locked.completed_at = now
    locked.completed_by = actor
    _set_reason(locked, reason, reason_key, reason_params)
    cooldown_hours = task.cooldown_hours or program.default_task_cooldown_hours
    if task.repeatable and cooldown_hours:
        locked.cooldown_until = now + timedelta(hours=cooldown_hours)
    locked.save(update_fields=["status", "rewardable", "completed_at", "completed_by",
                               "confidence", *_REASON_FIELDS, "cooldown_until", "updated_at"])
    _touch(locked.pairing)

    if rewardable:
        rewards.on_task_completed(locked)
    _check_track_completion(locked)
    return locked


def _check_track_completion(assignment):
    from . import rewards

    track = assignment.task.track
    enrollment = assignment.pairing.enrollments.filter(track=track).first()
    if enrollment is None or enrollment.status == MentorshipEnrollment.Status.COMPLETED:
        return
    active_task_ids = set(track.tasks.filter(active=True).values_list("id", flat=True))
    done_task_ids = set(
        MentorshipTaskAssignment.objects.filter(
            pairing=assignment.pairing, task_id__in=active_task_ids,
            status__in=MentorshipTaskAssignment.DONE_STATUSES,
        ).values_list("task_id", flat=True)
    )
    if active_task_ids and active_task_ids.issubset(done_task_ids):
        enrollment.status = MentorshipEnrollment.Status.COMPLETED
        enrollment.completed_at = timezone.now()
        enrollment.save(update_fields=["status", "completed_at", "updated_at"])
        rewards.on_track_completed(assignment.pairing, track)


# ---------------------------------------------------------------------------
# Pilot / mentor / leadership actions
# ---------------------------------------------------------------------------
def start_task(assignment, actor) -> bool:
    if assignment.status in MentorshipTaskAssignment.DONE_STATUSES:
        return False
    if not prerequisites_met(assignment):
        return False
    assignment.status = _A.IN_PROGRESS
    if assignment.started_at is None:
        assignment.started_at = timezone.now()
    _set_reason(assignment)
    assignment.save(update_fields=["status", "started_at", *_REASON_FIELDS, "updated_at"])
    _touch(assignment.pairing)
    return True


def add_evidence(assignment, user, *, kind, url="", text="") -> MentorshipEvidence:
    return MentorshipEvidence.objects.create(
        assignment=assignment, submitted_by=user, kind=kind, url=url[:200], text=text,
    )


def mentee_submit(assignment, actor, evidence=None) -> str:
    """Mentee submits a task for validation. Returns a short status string."""
    if assignment.status in MentorshipTaskAssignment.DONE_STATUSES:
        return "already_done"
    if not prerequisites_met(assignment):
        return "blocked"
    task = assignment.task
    method = task.validation_method
    assignment.submitted_at = timezone.now()
    if assignment.started_at is None:
        assignment.started_at = assignment.submitted_at

    if task.evidence_requirement == MentorshipTask.Evidence.REQUIRED and evidence is None \
            and not assignment.evidence_items.exists():
        return "needs_evidence"

    _record(assignment, _V.Source.MENTEE, _V.Result.PASS, confidence=CONF_MENTEE,
            key="task.mentee_marked_done", actor=actor, evidence=evidence)

    V = MentorshipTask.Validation
    if method == V.MENTEE_CONFIRM:
        assignment.save(update_fields=["submitted_at", "started_at", "updated_at"])
        _complete(assignment, actor, CONF_MENTEE, verified=False,
                  reason_key="task.mentee_self_confirmed")
        return "completed"

    if method in (V.API_REQUIRED, V.AUTO_INTERNAL):
        outcome = run_auto_check(assignment)
        if outcome and outcome.passed:
            assignment.save(update_fields=["submitted_at", "started_at", "updated_at"])
            _complete(assignment, actor, outcome.confidence, verified=True,
                      reason=outcome.detail, reason_key=outcome.key, reason_params=outcome.params)
            return "completed"
        assignment.status = _A.PENDING_API
        if outcome:
            _set_reason(assignment, outcome.detail, outcome.key, outcome.params)
        else:
            _set_reason(assignment, key="task.awaiting_activity_data")
        assignment.save(update_fields=["status", "submitted_at", "started_at", *_REASON_FIELDS,
                                       "updated_at"])
        _touch(assignment.pairing)
        return "pending_api"

    if method in (V.API_ASSISTED, V.HYBRID):
        run_auto_check(assignment)  # contributes confidence; mentor still signs off
        assignment.status = _A.PENDING_MENTOR
    elif method == V.LEADERSHIP:
        assignment.status = _A.PENDING_LEADERSHIP
    elif method == V.DUAL_CONFIRM:
        # Mentee half done; complete now only if the mentor already confirmed.
        if _has_pass(assignment, _V.Source.MENTOR):
            assignment.save(update_fields=["submitted_at", "started_at", "updated_at"])
            _complete(assignment, actor, CONF_MENTOR, verified=False,
                      reason_key="task.both_confirmed")
            return "completed"
        assignment.status = _A.PENDING_MENTOR
    else:  # MANUAL_MENTOR, EVIDENCE
        assignment.status = _A.PENDING_MENTOR

    assignment.save(update_fields=["status", "submitted_at", "started_at", "updated_at"])
    _touch(assignment.pairing)
    return assignment.status


def mentor_decide(assignment, actor, approve: bool, reason="") -> bool:
    """Mentor confirms or rejects a task awaiting their sign-off."""
    if assignment.status not in (_A.PENDING_MENTOR, _A.SUBMITTED, _A.IN_PROGRESS, _A.PENDING_API):
        return False
    if not approve:
        # ``reason`` is the mentor's own words: stored verbatim, no key, never translated.
        _record(assignment, _V.Source.MENTOR, _V.Result.FAIL, detail=reason, actor=actor)
        assignment.status = _A.REJECTED
        _set_reason(assignment, reason)
        assignment.save(update_fields=["status", *_REASON_FIELDS, "updated_at"])
        _touch(assignment.pairing)
        return True

    # With no note from the mentor the sentence is ours, so it carries a key; with one, the mentor's
    # words are stored verbatim.
    key = "" if reason else "task.mentor_confirmed"
    _record(assignment, _V.Source.MENTOR, _V.Result.PASS, confidence=CONF_MENTOR,
            detail=reason, key=key, actor=actor)
    method = assignment.task.validation_method
    auto_conf = _best_auto_confidence(assignment)
    verified = method in (
        MentorshipTask.Validation.API_ASSISTED, MentorshipTask.Validation.HYBRID
    ) and auto_conf >= VERIFY_THRESHOLD
    _complete(assignment, actor, max(CONF_MENTOR, auto_conf), verified=verified,
              reason=reason, reason_key=key)
    return True


def leadership_decide(assignment, actor, approve: bool, reason="") -> bool:
    if assignment.status in MentorshipTaskAssignment.DONE_STATUSES:
        return False
    if not approve:
        # The officer's own words: verbatim, no key.
        _record(assignment, _V.Source.LEADERSHIP, _V.Result.FAIL, detail=reason, actor=actor)
        assignment.status = _A.REJECTED
        _set_reason(assignment, reason)
        assignment.save(update_fields=["status", *_REASON_FIELDS, "updated_at"])
        return True
    _record(assignment, _V.Source.LEADERSHIP, _V.Result.PASS, confidence=CONF_LEADERSHIP,
            detail=reason, key="" if reason else "task.approved_by_leadership", actor=actor)
    if reason:
        _complete(assignment, actor, CONF_LEADERSHIP, verified=True, reason=reason)
    else:
        _complete(assignment, actor, CONF_LEADERSHIP, verified=True,
                  reason_key="task.leadership_approved")
    return True


def waive_task(assignment, actor, reason="") -> bool:
    if assignment.status in MentorshipTaskAssignment.DONE_STATUSES:
        return False
    # The sentence is ours; %(reason)s is the officer's free text, interpolated raw.
    _record(assignment, _V.Source.SYSTEM, _V.Result.PASS, key="task.waived",
            params={"reason": reason[:280]}, actor=actor)
    assignment.status = _A.WAIVED
    assignment.completed_at = timezone.now()
    assignment.completed_by = actor
    assignment.rewardable = False
    _set_reason(assignment, reason)
    assignment.save(update_fields=["status", "completed_at", "completed_by", "rewardable",
                                   *_REASON_FIELDS, "updated_at"])
    _touch(assignment.pairing)
    return True


def sweep_pending_api(assignment) -> bool:
    """Re-run an API/internal auto-check for an assignment stuck PENDING_API.

    Called by the beat sweep once the underlying synced data may have arrived.
    """
    if assignment.status != _A.PENDING_API:
        return False
    outcome = run_auto_check(assignment)
    if outcome and outcome.passed:
        # This runs in the ``mentorship.sweep_api_validations`` worker: no user, no locale. The
        # scaffold key is what lets the mentee read it in German later.
        _complete(assignment, assignment.completed_by, outcome.confidence, verified=True,
                  reason=outcome.detail, reason_key=outcome.key, reason_params=outcome.params)
        return True
    return False


# ---------------------------------------------------------------------------
# Progress rollups (for dashboards)
# ---------------------------------------------------------------------------
def pairing_progress(pairing) -> list[dict]:
    """Per-track progress for a pairing's dashboard."""
    assignments = list(
        pairing.assignments.select_related("task", "task__track").all()
    )
    by_track: dict[int, dict] = {}
    for a in assignments:
        track = a.task.track
        bucket = by_track.setdefault(track.id, {
            "track": track, "total": 0, "done": 0, "pending": 0, "assignments": [],
        })
        bucket["total"] += 1
        bucket["assignments"].append(a)
        if a.is_done:
            bucket["done"] += 1
        elif a.status in MentorshipTaskAssignment.PENDING_STATUSES:
            bucket["pending"] += 1
    rows = sorted(by_track.values(), key=lambda r: r["track"].sort_order)
    for r in rows:
        r["pct"] = int(round(100 * r["done"] / r["total"])) if r["total"] else 0
    return rows
