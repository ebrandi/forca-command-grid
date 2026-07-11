"""Capsuleer Path business logic — the only place career state changes (brief §6, doc 06, doc 11).

Views and background jobs stay thin: they parse input, call a service, and render. Every rule that
matters lives here — the two-layer visibility chokepoint and object re-check (doc 09 §2.2), the
guarded goal lifecycle transition table (doc 05 §3.1), the milestone crediting and endorsement
model (doc 05 §4.3, doc 09 §3), field-level budget/motivation masking (doc 09 §2.3), and the
required-milestone-weighted progress recompute (doc 11 §1). Every mutation takes the goal row lock
(``select_for_update``) before touching milestone rows and recomputing progress, so a concurrent
skill-import hook (Stage 2) and an owner edit converge instead of clobbering (doc 11 §7). Money and
free text never enter audit metadata or ``GoalActivity.detail`` (doc 09 §7.1).

Stage boundaries: ``build_plan``, the verification-engine checkers (``verify.py``) and the
contribution baseline stamp are Stage 2; suggestion generation and pingboard emission are Stage 3.
The transition side effects those own are noted where they attach.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Max, OuterRef, Q, Subquery
from django.utils import timezone

from core.audit import audit_log
from core.rbac import ROLE_OFFICER, has_role

from . import notify, progress
from .models import (
    CareerGoal,
    CareerMilestone,
    CareerProfile,
    GoalActivity,
    GoalPace,
    GoalStatus,
    GoalType,
    MilestoneKind,
    MilestoneStatus,
    Priority,
    Verification,
    Visibility,
)
from .params import validate_milestone_params

logger = logging.getLogger("forca.capsuleer")

# Service-layer abuse guards (doc 09 T-23). Deliberately module constants, not config keys — the
# brief §9 key set is closed and these are safety floors, not leadership knobs. The goal cap is the
# single intentional exception to "advise, never block" (spec §2.7).
MAX_ACTIVE_GOALS = 25
MAX_MILESTONES_PER_GOAL = 60
MAX_STEPS_PER_GOAL = 100

# Soft-target type + audit actions (core.audit; pks and structural metadata only, never titles).
TARGET_GOAL = "capsuleer_goal"
AUDIT_GOAL_CREATED = "capsuleer.goal.created"
AUDIT_GOAL_STATUS = "capsuleer.goal.status_changed"
AUDIT_GOAL_ENDORSE = "capsuleer.goal.endorse"

# GoalActivity verbs for the milestone endorsement stream + the mentor note surface (doc 09 §3).
V_ENDORSED = "milestone.endorsed"
V_ENDORSE_RETRACTED = "milestone.endorsement_retracted"
V_MENTOR_NOTE = "mentor_note"

# The legal goal-status edges (doc 05 §3.1). Anything absent is rejected — the DB never sees the
# transition rules (a trigger cannot see the acting user, campaigns precedent).
_S = GoalStatus
_LEGAL_GOAL_TRANSITIONS = {
    (_S.CONSIDERING, _S.ACTIVE), (_S.CONSIDERING, _S.ABANDONED), (_S.CONSIDERING, _S.ARCHIVED),
    (_S.ACTIVE, _S.PAUSED), (_S.ACTIVE, _S.COMPLETED), (_S.ACTIVE, _S.ABANDONED),
    (_S.PAUSED, _S.ACTIVE), (_S.PAUSED, _S.ABANDONED), (_S.PAUSED, _S.ARCHIVED),
    (_S.COMPLETED, _S.ACTIVE), (_S.COMPLETED, _S.ARCHIVED),
    (_S.ABANDONED, _S.CONSIDERING), (_S.ABANDONED, _S.ARCHIVED),
}


# --------------------------------------------------------------------------- #
#  Visibility chokepoint (doc 09 §2.2) — every non-owner queryset starts here
# --------------------------------------------------------------------------- #
def visible_goals(user):
    """The single list chokepoint: the goals ``user`` may see (doc 09 §2.2).

    Owner always; a ``mentor``-tier goal of a pilot this user actively mentors; any ``officers``-
    tier goal when the user holds ``ROLE_OFFICER`` (director inherits it). ``private`` and
    ``aggregate_only`` never widen direct visibility. Every list/panel/fragment shown to a
    non-owner must enumerate through this queryset — a bare pk fetch is IDOR-vulnerable, so object
    routes additionally call :func:`can_view_goal`.
    """
    if not getattr(user, "is_authenticated", False):
        return CareerGoal.objects.none()
    q = Q(user=user)
    mentee_ids = _active_mentee_user_ids(user)
    if mentee_ids:
        q |= Q(visibility=Visibility.MENTOR, user_id__in=mentee_ids)
    if has_role(user, ROLE_OFFICER):
        q |= Q(visibility=Visibility.OFFICERS)
    return CareerGoal.objects.filter(q)


def can_view_goal(user, goal) -> bool:
    """Object-level re-check on every object route and subresource (never trust the list filter
    alone — the IDOR defence of doc 09 §2.2). Subresources resolve through the parent goal."""
    if not getattr(user, "is_authenticated", False):
        return False
    # Owner fast-path: skip the mentorship-pairing + EXISTS queries on the majority (owner) path;
    # mentor/officer viewers still take the full chokepoint below (finding 25).
    if getattr(user, "pk", None) == goal.user_id:
        return True
    return visible_goals(user).filter(pk=goal.pk).exists()


def _active_mentee_user_ids(user) -> set[int]:
    """User ids of pilots ``user`` actively mentors (an ``ACTIVE`` pairing, doc 09 §3.1).

    Evaluated per call, never cached, so pausing/ending a pairing revokes mentor visibility on the
    next request with no invalidation step. ``PAUSED`` pairings do not admit — suspension of the
    relationship is suspension of visibility (the conservative reading).
    """
    if not getattr(user, "pk", None):
        return set()
    from apps.mentorship.models import MentorshipPairing

    return set(
        MentorshipPairing.objects.filter(
            mentor__user_id=user.pk, status=MentorshipPairing.Status.ACTIVE
        ).values_list("mentee__user_id", flat=True)
    )


# --------------------------------------------------------------------------- #
#  Field-level masking (doc 09 §1, §2.3) — budget/motivation are owner-only
# --------------------------------------------------------------------------- #
def can_view_budget(user, goal) -> bool:
    """The two budget fields are owner-only at field level, every tier (N-class, doc 09 §1).

    This is the account-level truth (owner-only). Impersonation-aware masking (view-as with a
    non-owner real actor still masks) is layered on top by the view's ``_real_actor_is_owner``,
    which gates the budget context vars on the *real* actor, not ``request.user``.
    """
    return getattr(user, "pk", None) == goal.user_id


def shared_goal_payload(user, goal) -> dict:
    """The viewer-facing field dict for a goal, masked at the context-builder layer (doc 09 §2.3).

    Shared-tier (S) fields are always present; owner-only (N/O) fields — ``budget_isk``,
    ``motivation``, ``paused_reason``, ``visibility``, ``review_due_at``, ``corp_alignment_optin``
    — are dropped entirely for any non-owner, so they never reach a template or a JSON context
    (masking is absence of the value, not a hidden label). ``motivation`` and ``budget_isk`` are
    never returned to a mentor/officer by any helper that shapes shared-tier data.
    """
    owner = getattr(user, "pk", None) == goal.user_id
    data = {
        "id": goal.pk,
        "character_id": goal.character_id,
        "title": goal.title,
        "goal_type": goal.goal_type,
        "template_key": goal.template_key,
        "doctrine_id": goal.doctrine_id,
        "ship_type_id": goal.ship_type_id,
        "activity": goal.activity,
        "status": goal.status,
        "priority": goal.priority,
        "pace": goal.pace,
        "target_date": goal.target_date,
        "progress_percent": goal.progress_percent,
        "started_at": goal.started_at,
        "completed_at": goal.completed_at,
    }
    if owner:
        data["budget_isk"] = goal.budget_isk
        data["motivation"] = goal.motivation
        data["paused_reason"] = goal.paused_reason
        data["visibility"] = goal.visibility
        data["review_due_at"] = goal.review_due_at
        data["corp_alignment_optin"] = goal.corp_alignment_optin
    return data


# --------------------------------------------------------------------------- #
#  Activity stream (doc 07 §4.8) — append-only, tier-safe by construction
# --------------------------------------------------------------------------- #
def record_activity(goal, actor, verb, detail=None) -> GoalActivity:
    """Append one row to a goal's activity stream. Automation passes ``actor=None`` (stored NULL).

    Callers are responsible for keeping ``detail`` tier-safe (doc 09 §1.7): verbs, pks, status
    names and short note text only — never budget, motivation, paused_reason or suggestion content.
    """
    return GoalActivity.objects.create(
        goal=goal,
        actor=actor if getattr(actor, "pk", None) else None,
        verb=str(verb)[:64],
        detail=detail or {},
    )


# --------------------------------------------------------------------------- #
#  Goal creation (doc 05 §2)
# --------------------------------------------------------------------------- #
@transaction.atomic
def create_goal(
    user,
    *,
    title,
    goal_type,
    character=None,
    activity="",
    template=None,
    doctrine_id=None,
    ship_type_id=None,
    motivation="",
    priority=Priority.SECONDARY,
    pace=GoalPace.INHERIT,
    visibility=None,
    budget_isk=None,
    target_date=None,
    corp_alignment_optin=False,
    status=GoalStatus.CONSIDERING,
) -> CareerGoal:
    """Create one goal for ``user`` (doc 05 §2.6/§2.7).

    Server-side validation (never trusting the form): ``title`` required; ``goal_type``/``status``/
    ``priority``/``pace``/``visibility`` accept only enum values; ``character`` must belong to the
    owner (re-checked here, not just in the form queryset); ``budget_isk`` non-negative. The
    non-archived goal count is soft-capped at :data:`MAX_ACTIVE_GOALS` (the one abuse guard). When a
    template is set, an existing live goal for the same ``(user, template_key)`` is returned rather
    than duplicated (the partial unique constraint also enforces this at the DB).

    Activation side effects owned by later stages — ``build_plan`` (Stage 2) and the contribution
    baseline stamp (Stage 2) — are not run here; a goal created ``active`` only sets ``started_at``.
    """
    if not (title or "").strip():
        raise ValidationError("A goal needs a title.")
    if goal_type not in GoalType.values:
        raise ValidationError(f"Unknown goal type: {goal_type!r}.")
    if status not in (GoalStatus.CONSIDERING, GoalStatus.ACTIVE):
        raise ValidationError("A new goal starts as considering or active.")
    if priority not in Priority.values:
        raise ValidationError(f"Unknown priority: {priority!r}.")
    if pace not in GoalPace.values:
        raise ValidationError(f"Unknown pace: {pace!r}.")
    if character is not None and character.user_id != getattr(user, "pk", None):
        raise ValidationError("That character does not belong to you.")
    if budget_isk is not None and budget_isk < 0:
        raise ValidationError("Budget cannot be negative.")

    if visibility is None:
        profile = CareerProfile.objects.filter(user=user).first()
        visibility = profile.default_visibility if profile else Visibility.PRIVATE
    elif visibility not in Visibility.values:
        raise ValidationError(f"Unknown visibility: {visibility!r}.")

    template_key = template.key if template is not None else ""
    if template is not None:
        existing = (
            CareerGoal.objects.filter(user=user, template_key=template_key)
            .filter(status__in=[GoalStatus.CONSIDERING, GoalStatus.ACTIVE, GoalStatus.PAUSED])
            .first()
        )
        if existing is not None:
            return existing

    active_count = CareerGoal.objects.filter(user=user).exclude(status=GoalStatus.ARCHIVED).count()
    if active_count >= MAX_ACTIVE_GOALS:
        raise ValidationError(
            f"You already have {MAX_ACTIVE_GOALS} active goals — archive one before adding another."
        )

    goal = CareerGoal(
        user=user,
        character=character,
        title=title.strip()[:140],
        motivation=(motivation or "")[:2000],
        goal_type=goal_type,
        template=template,
        template_key=template_key,
        doctrine_id=doctrine_id,
        ship_type_id=ship_type_id,
        activity=activity or "",
        status=status,
        priority=priority,
        pace=pace,
        visibility=visibility,
        budget_isk=budget_isk,
        target_date=target_date,
        corp_alignment_optin=bool(corp_alignment_optin),
    )
    if status == GoalStatus.ACTIVE:
        goal.started_at = timezone.now()
    goal.save()

    record_activity(goal, user, "goal.created", {"goal_type": goal_type})
    audit_log(user, AUDIT_GOAL_CREATED, target_type=TARGET_GOAL, target_id=str(goal.pk),
              metadata={"goal_type": goal_type})
    if status == GoalStatus.ACTIVE:
        _on_activation(goal)
    return goal


# --------------------------------------------------------------------------- #
#  Goal lifecycle (doc 05 §3.1)
# --------------------------------------------------------------------------- #
def _status_verb(old: str, to_status: str) -> str:
    if to_status == GoalStatus.ACTIVE:
        return {
            GoalStatus.CONSIDERING: "goal.activated",
            GoalStatus.PAUSED: "goal.resumed",
            GoalStatus.COMPLETED: "goal.reopened",
        }.get(old, "goal.activated")
    return {
        GoalStatus.PAUSED: "goal.paused",
        GoalStatus.COMPLETED: "goal.completed",
        GoalStatus.ABANDONED: "goal.abandoned",
        GoalStatus.ARCHIVED: "goal.archived",
        GoalStatus.CONSIDERING: "goal.revived",
    }.get(to_status, "goal.status_changed")


def _completion_blockers(goal) -> list[CareerMilestone]:
    """Required, non-skipped milestones that are not yet done — the completion gate (doc 05 §3.1).

    A skipped required milestone removes itself from the gate (doc 11 §1.1); only ``pending`` (or
    otherwise non-``done``) required milestones block completion.
    """
    return list(
        goal.milestones.filter(required=True).exclude(
            status__in=[MilestoneStatus.DONE, MilestoneStatus.SKIPPED]
        )
    )


@transaction.atomic
def set_goal_status(goal, to_status, actor, reason="") -> CareerGoal:
    """Guarded goal-status transition (doc 05 §3.1).

    Validates under the goal row lock, applies the transition and its timestamp bookkeeping,
    recomputes progress, and writes both a ``GoalActivity`` row and a ``core.audit`` record — all
    in one transaction. Re-posting the current status is an idempotent no-op. An illegal edge, a
    non-owner actor, or completing with unfinished required milestones and no override reason each
    raise :class:`ValidationError`. Owner-only: no mentor/officer/system transition exists (doc 05
    §3.2). Free-text ``reason`` is persisted only on the goal (``paused_reason``) — it never enters
    audit metadata or activity detail (doc 09 §7.1); the completion override is recorded as a flag.
    """
    if to_status not in GoalStatus.values:
        raise ValidationError(f"Unknown status: {to_status!r}.")
    locked = CareerGoal.objects.select_for_update().get(pk=goal.pk)
    if locked.user_id != getattr(actor, "pk", None):
        raise ValidationError("Only the goal owner can change its status.")
    old = locked.status
    if old == to_status:
        _mirror(goal, locked)
        return locked
    if (old, to_status) not in _LEGAL_GOAL_TRANSITIONS:
        raise ValidationError("That status change is not allowed from the goal's current state.")

    now = timezone.now()
    override = False
    if to_status == GoalStatus.COMPLETED and _completion_blockers(locked):
        if not (reason or "").strip():
            raise ValidationError(
                "Complete a goal when its required milestones are done, or supply an override "
                "reason."
            )
        override = True

    update_fields = ["status", "updated_at"]
    prior_completed = None
    locked.status = to_status
    if to_status == GoalStatus.ACTIVE and locked.started_at is None:
        locked.started_at = now
        update_fields.append("started_at")
    if to_status == GoalStatus.COMPLETED:
        locked.completed_at = now
        update_fields.append("completed_at")
    if old == GoalStatus.COMPLETED and to_status == GoalStatus.ACTIVE:
        prior_completed = locked.completed_at  # preserved in activity, cleared on the row
        locked.completed_at = None
        if "completed_at" not in update_fields:
            update_fields.append("completed_at")
    if to_status == GoalStatus.PAUSED:
        locked.paused_reason = (reason or "")[:200]
        update_fields.append("paused_reason")
    elif to_status == GoalStatus.ACTIVE and old == GoalStatus.PAUSED:
        locked.paused_reason = ""
        update_fields.append("paused_reason")
    locked.save(update_fields=update_fields)
    _recompute_and_save(locked)

    detail = {"from": old, "to": to_status}
    if override:
        detail["override"] = True
    if prior_completed is not None:
        detail["prior_completed_at"] = prior_completed.isoformat()
    record_activity(locked, actor, _status_verb(old, to_status), detail)
    audit_log(actor, AUDIT_GOAL_STATUS, target_type=TARGET_GOAL, target_id=str(locked.pk),
              metadata={"from": old, "to": to_status, "override": override})
    if to_status == GoalStatus.COMPLETED:
        transaction.on_commit(lambda g=locked: notify.goal_completed(g))
    if to_status == GoalStatus.ACTIVE and old == GoalStatus.CONSIDERING:
        _on_activation(locked)  # first activation only — never on resume from paused
    _mirror(goal, locked)
    return locked


def set_goal_visibility(goal, actor, visibility) -> CareerGoal:
    """Change a goal's sharing tier (doc 05 §3.4). Owner-only; takes effect immediately (no
    cached authorisation), recorded in ``GoalActivity``. Narrowing revokes mentor/officer access
    on the next request through :func:`visible_goals`."""
    if visibility not in Visibility.values:
        raise ValidationError(f"Unknown visibility: {visibility!r}.")
    with transaction.atomic():
        locked = CareerGoal.objects.select_for_update().get(pk=goal.pk)
        if locked.user_id != getattr(actor, "pk", None):
            raise ValidationError("Only the goal owner can change its visibility.")
        if locked.visibility == visibility:
            _mirror(goal, locked)
            return locked
        old = locked.visibility
        locked.visibility = visibility
        locked.save(update_fields=["visibility", "updated_at"])
        record_activity(locked, actor, "visibility.changed", {"from": old, "to": visibility})
        _mirror(goal, locked)
    return locked


# --------------------------------------------------------------------------- #
#  Milestones (doc 05 §4)
# --------------------------------------------------------------------------- #
def _require_owner(goal, actor) -> None:
    if goal.user_id != getattr(actor, "pk", None):
        raise ValidationError("Only the goal owner can edit its milestones.")


# A finished goal is frozen: no new milestones/steps, credits, activity rows or notifications may
# accrue on it (doc 05 §3.1, finding 13). Reopen it (COMPLETED→ACTIVE / ABANDONED→CONSIDERING)
# before editing.
_TERMINAL_STATUSES = frozenset({GoalStatus.COMPLETED, GoalStatus.ABANDONED, GoalStatus.ARCHIVED})


def _reject_if_terminal(goal) -> None:
    if goal.status in _TERMINAL_STATUSES:
        raise ValidationError(
            "This goal is finished — reopen it before changing its milestones or steps."
        )


@transaction.atomic
def add_milestone(
    goal, actor, *, kind, title, verification=Verification.AUTO, required=True, params=None,
    order=None, due_date=None,
) -> CareerMilestone:
    """Add a milestone to a goal (doc 05 §4.4). Owner-only; ``params`` validated per kind
    (``params.py``); ``order`` auto-assigned after the current max when omitted; count capped at
    :data:`MAX_MILESTONES_PER_GOAL`. Recomputes progress and records activity."""
    locked = CareerGoal.objects.select_for_update().get(pk=goal.pk)
    _require_owner(locked, actor)
    _reject_if_terminal(locked)
    params = params or {}
    validate_milestone_params(kind, params, verification)
    if locked.milestones.count() >= MAX_MILESTONES_PER_GOAL:
        raise ValidationError(f"A goal cannot hold more than {MAX_MILESTONES_PER_GOAL} milestones.")
    if order is None:
        order = (locked.milestones.aggregate(m=Max("order"))["m"] or 0) + 1
    milestone = CareerMilestone.objects.create(
        goal=locked, kind=kind, title=str(title)[:140], verification=verification,
        required=bool(required), params=params, order=order, due_date=due_date,
    )
    _recompute_and_save(locked)
    record_activity(locked, actor, "milestone.added",
                    {"milestone_id": milestone.pk, "kind": kind, "order": order})
    _mirror(goal, locked)
    return milestone


@transaction.atomic
def skip_milestone(goal, milestone, actor, note="") -> CareerMilestone:
    """Owner marks a milestone ``skipped`` (doc 05 §4.4). A skipped required milestone leaves the
    completion gate and the progress denominator (doc 11 §1.1) — skipping is a plan edit, never a
    failure. Idempotent on an already-skipped milestone."""
    locked = CareerGoal.objects.select_for_update().get(pk=goal.pk)
    _require_owner(locked, actor)
    _reject_if_terminal(locked)
    ms = locked.milestones.select_for_update().get(pk=milestone.pk)
    if ms.status == MilestoneStatus.SKIPPED:
        _mirror(goal, locked)
        return ms
    ms.status = MilestoneStatus.SKIPPED
    ms.save(update_fields=["status", "updated_at"])
    _recompute_and_save(locked)
    record_activity(locked, actor, "milestone.skipped",
                    {"milestone_id": ms.pk, "note": (note or "")[:200]})
    _mirror(goal, locked)
    return ms


@transaction.atomic
def unskip_milestone(goal, milestone, actor) -> CareerMilestone:
    """Owner returns a skipped milestone to ``pending`` (doc 05 §4.4). Recomputes progress."""
    locked = CareerGoal.objects.select_for_update().get(pk=goal.pk)
    _require_owner(locked, actor)
    _reject_if_terminal(locked)
    ms = locked.milestones.select_for_update().get(pk=milestone.pk)
    if ms.status != MilestoneStatus.SKIPPED:
        _mirror(goal, locked)
        return ms
    ms.status = MilestoneStatus.PENDING
    ms.save(update_fields=["status", "updated_at"])
    _recompute_and_save(locked)
    record_activity(locked, actor, "milestone.unskipped", {"milestone_id": ms.pk})
    _mirror(goal, locked)
    return ms


@transaction.atomic
def complete_milestone(goal, milestone, actor, *, evidence_note="") -> CareerMilestone:
    """Owner marks a milestone ``done`` (doc 05 §4.3). Always an owner action.

    ``auto`` milestones are never hand-credited (they flip only via the Stage 2 checkers — T-19);
    ``mentor``/``officer`` milestones require a matching, un-retracted endorsement note (doc 09 §3),
    whose endorser identity and note are frozen into ``evidence_snapshot`` at credit time; ``self``
    milestones credit directly. Idempotent on an already-done milestone; recomputes progress.
    """
    locked = CareerGoal.objects.select_for_update().get(pk=goal.pk)
    _require_owner(locked, actor)
    _reject_if_terminal(locked)
    ms = locked.milestones.select_for_update().get(pk=milestone.pk)
    if ms.status == MilestoneStatus.DONE:
        _mirror(goal, locked)
        return ms
    if ms.status == MilestoneStatus.SKIPPED:
        raise ValidationError("Unskip this milestone before completing it.")
    if ms.verification == Verification.AUTO:
        raise ValidationError("Automatic milestones are credited by verification, not by hand.")

    snapshot = {"at": timezone.now().isoformat()}
    if ms.verification in (Verification.MENTOR, Verification.OFFICER):
        endorsement = _active_endorsement(locked, ms, ms.verification)
        if endorsement is None:
            raise ValidationError("This milestone needs a matching endorsement before completion.")
        # Role only — never the endorser's raw user id (doc 09 §1.4: an evidence snapshot must not
        # freeze another pilot's identifiers onto the owner's milestone). The endorsement itself is
        # attributed on its own GoalActivity row, which GDPR erasure anonymises.
        snapshot["verifier_role"] = ms.verification
        snapshot["endorsement_note"] = (endorsement.detail or {}).get("note", "")
    else:
        snapshot["self_certified"] = True

    ms.status = MilestoneStatus.DONE
    ms.completed_at = timezone.now()
    if evidence_note:
        ms.evidence_note = evidence_note[:500]  # bounded pilot text (finding 17)
    ms.evidence_snapshot = snapshot
    ms.save(update_fields=["status", "completed_at", "evidence_note", "evidence_snapshot",
                           "updated_at"])
    _recompute_and_save(locked)
    record_activity(locked, actor, "milestone.credited",
                    {"milestone_id": ms.pk, "order": ms.order, "verification": ms.verification})
    transaction.on_commit(lambda g=locked, m=ms: notify.milestone_reached(g, m))
    _mirror(goal, locked)
    return ms


@transaction.atomic
def reopen_milestone(goal, milestone, actor) -> CareerMilestone:
    """Owner reverts a self/mentor/officer-completed milestone to ``pending`` (doc 05 §4.3).

    Auto-credited milestones are never reverted (their evidence records what was true at credit
    time — doc 05 §4.2). The frozen ``evidence_snapshot`` is left in history. Recomputes progress.
    """
    locked = CareerGoal.objects.select_for_update().get(pk=goal.pk)
    _require_owner(locked, actor)
    _reject_if_terminal(locked)
    ms = locked.milestones.select_for_update().get(pk=milestone.pk)
    if ms.status != MilestoneStatus.DONE:
        _mirror(goal, locked)
        return ms
    if ms.verification == Verification.AUTO:
        raise ValidationError("Automatically credited milestones cannot be reopened.")
    ms.status = MilestoneStatus.PENDING
    ms.completed_at = None
    ms.save(update_fields=["status", "completed_at", "updated_at"])
    _recompute_and_save(locked)
    record_activity(locked, actor, "milestone.reopened", {"milestone_id": ms.pk})
    _mirror(goal, locked)
    return ms


# --------------------------------------------------------------------------- #
#  Endorsements and mentor notes (doc 05 §4.3, doc 09 §3)
# --------------------------------------------------------------------------- #
def _active_endorsement(goal, milestone, role):
    """The current endorsement row for ``milestone`` at ``role``, or ``None`` (latest wins).

    Scans the goal's endorse/retract stream newest-first; the milestone is endorsed if the most
    recent event for it at that role is an endorsement (a later retraction withdraws it).
    """
    rows = goal.activity_log.filter(verb__in=[V_ENDORSED, V_ENDORSE_RETRACTED]).order_by(
        "-created_at", "-id"
    )
    for row in rows:
        detail = row.detail or {}
        if detail.get("milestone_id") == milestone.pk and detail.get("role") == role:
            return row if row.verb == V_ENDORSED else None
    return None


def endorsement_map(goal) -> dict:
    """``{(milestone_id, role): True}`` for currently-endorsed mentor/officer milestones, from a
    single newest-first scan of the goal's endorse/retract stream (finding 19).

    Replaces the per-milestone :func:`_active_endorsement` full-stream query on the goal page: one
    fetch feeds every milestone view-model. First (newest) event per ``(milestone, role)`` wins — a
    later retraction leaves the key out of the map.
    """
    endorsed: dict = {}
    seen: set = set()
    rows = goal.activity_log.filter(verb__in=[V_ENDORSED, V_ENDORSE_RETRACTED]).order_by(
        "-created_at", "-id"
    )
    for row in rows:
        detail = row.detail or {}
        key = (detail.get("milestone_id"), detail.get("role"))
        if key[0] is None or key in seen:
            continue
        seen.add(key)
        if row.verb == V_ENDORSED:
            endorsed[key] = True
    return endorsed


def _assert_can_endorse(goal, milestone, endorser) -> str:
    """Validate that ``endorser`` may endorse ``milestone`` on ``goal`` and return the role.

    Mentor endorsement needs an ``ACTIVE`` pairing with the owner and a ``mentor``-tier goal;
    officer endorsement needs ``ROLE_OFFICER`` and an ``officers``-tier goal. Endorsers never mark
    milestones done — this only authorises the note (doc 05 §4.3, doc 09 §3.1/§3.3).
    """
    if milestone.goal_id != goal.pk:
        raise ValidationError("That milestone is not part of this goal.")
    # An endorsement must come from a second party — an officer/mentor can never endorse their own
    # goal's milestone (self-verification forgery, findings 6/7, doc 09 §3.3). Guards both roles.
    if getattr(endorser, "pk", None) == goal.user_id:
        raise ValidationError("You cannot endorse your own milestone.")
    role = milestone.verification
    if role == Verification.MENTOR:
        if goal.visibility != Visibility.MENTOR or goal.user_id not in _active_mentee_user_ids(
            endorser
        ):
            raise ValidationError("Only an active mentor of a mentor-shared goal can endorse this.")
    elif role == Verification.OFFICER:
        if goal.visibility != Visibility.OFFICERS or not has_role(endorser, ROLE_OFFICER):
            raise ValidationError("Only an officer can endorse an officers-shared goal milestone.")
    else:
        raise ValidationError("This milestone does not take endorsements.")
    return role


@transaction.atomic
def endorse_milestone(goal, milestone, endorser, *, note="", ip="") -> GoalActivity:
    """Record a mentor/officer endorsement note on a milestone (doc 05 §4.3, doc 09 §3).

    The endorser's entire write surface: it appends a note, never mutates goal or milestone state —
    the owner still performs completion. An officer endorsement additionally writes a ``core.audit``
    row with ``client_ip`` (doc 09 §3.3). Returns the created activity row.
    """
    locked = CareerGoal.objects.select_for_update().get(pk=goal.pk)
    _reject_if_terminal(locked)  # a frozen goal accrues no new endorsement activity (finding 13)
    ms = locked.milestones.select_for_update().get(pk=milestone.pk)
    role = _assert_can_endorse(locked, ms, endorser)
    row = record_activity(locked, endorser, V_ENDORSED,
                          {"milestone_id": ms.pk, "role": role, "note": (note or "")[:200]})
    if role == Verification.OFFICER:
        audit_log(endorser, AUDIT_GOAL_ENDORSE, target_type=TARGET_GOAL, target_id=str(locked.pk),
                  metadata={"owner_id": locked.user_id, "milestone_id": ms.pk}, ip=ip)
    return row


@transaction.atomic
def retract_endorsement(goal, milestone, endorser, *, note="") -> GoalActivity:
    """Withdraw a prior endorsement before the owner completes the milestone (doc 05 §4.3).

    Only meaningful while the milestone is still ``pending``; after completion the owner reopens it
    instead. Authorised by the same role rules as endorsing.
    """
    locked = CareerGoal.objects.select_for_update().get(pk=goal.pk)
    _reject_if_terminal(locked)  # consistent with endorse_milestone (finding 13)
    ms = locked.milestones.select_for_update().get(pk=milestone.pk)
    role = _assert_can_endorse(locked, ms, endorser)
    if ms.status == MilestoneStatus.DONE:
        raise ValidationError("The milestone is already complete; the owner must reopen it.")
    return record_activity(locked, endorser, V_ENDORSE_RETRACTED,
                           {"milestone_id": ms.pk, "role": role, "note": (note or "")[:200]})


# --------------------------------------------------------------------------- #
#  Action steps + the one Tasks integration (doc 05 §5, ADR-0008)
# --------------------------------------------------------------------------- #
TASK_RELATED_TYPE = "capsuleer_goal"


def _neutralise_step_title(raw, goal) -> str:
    """Strip goal/ambition context from an auto-derived corp-task title (doc 05 §5.2, finding 9).

    Suggestion-sourced steps embed the goal title in ``«…»`` guillemets ('Mentors available for
    «Become a Black Ops pilot»'); publishing that verbatim would leak a private goal corp-wide. Drop
    every guillemet segment and any bare occurrence of the goal title, then tidy whitespace."""
    import re

    cleaned = re.sub(r"«[^»]*»", "", raw or "")
    if goal.title:
        cleaned = cleaned.replace(goal.title, "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .—-:·")
    return cleaned or "A step from a pilot's plan"


@transaction.atomic
def make_corp_task_from_step(goal, step, actor, *, title=None, description="") -> object:
    """Create a corp task from an action step — an explicit, owner-only action (ADR-0008, doc 05 §5.2).

    The only path that puts career work on the corp task board. ``related_id`` is ``{goal_pk}:{step_pk}``
    so per-pilot dedupe cannot collide corp-wide; an active task already covering the step is linked
    rather than duplicated. Titles for non-``officers`` goals carry the bare action with all goal
    context stripped (the neutral-title rule) — the pilot surfaced the action, not the ambition. No
    milestone/goal state ever lives in Tasks; the DONE roll-up (signals) only adds evidence.
    """
    from apps.tasks.services import create_task
    from core.features import feature_enabled

    locked = CareerGoal.objects.select_for_update().get(pk=goal.pk)
    if locked.user_id != getattr(actor, "pk", None):
        raise ValidationError("Only the goal owner can create a corp task.")
    _reject_if_terminal(locked)
    ms = locked.action_steps.select_for_update().get(pk=step.pk)
    if ms.status != "open":
        raise ValidationError("Only an open step can become a corp task.")
    if not feature_enabled("tasks"):
        raise ValidationError("The corp task board is not enabled.")

    if locked.visibility == Visibility.OFFICERS:
        task_title = (title or ms.title)[:200]
        task_description = (description or f"Surfaced from career goal «{locked.title}».")[:1000]
    else:
        # Neutral: the bare action, no goal title / motivation / template name (doc 05 §5.2). A
        # pilot-supplied title is trusted; an auto-derived one strips any «…» goal context a
        # suggestion-sourced step embeds (finding 9).
        task_title = (title or _neutralise_step_title(ms.title, locked))[:200]
        task_description = (
            description or "A pilot has surfaced this step from their plan as a corp task."
        )[:1000]

    task = create_task(
        task_type="other", title=task_title, description=task_description,
        related_type=TASK_RELATED_TYPE, related_id=f"{locked.pk}:{ms.pk}", created_by=actor,
    )
    ms.task_id = task.pk
    ms.save(update_fields=["task_id", "updated_at"])
    record_activity(locked, actor, "task_created", {"step_id": ms.pk, "task_id": task.pk})
    return task


@transaction.atomic
def add_mentor_note(goal, mentor, text) -> GoalActivity:
    """A mentor posts a suggestion note on a mentor-shared goal (doc 09 §3.1). Read-only otherwise:
    the note changes no goal or milestone state. Requires an ACTIVE pairing with the owner and a
    ``mentor``-tier goal."""
    locked = CareerGoal.objects.select_for_update().get(pk=goal.pk)
    if locked.visibility != Visibility.MENTOR or locked.user_id not in _active_mentee_user_ids(
        mentor
    ):
        raise ValidationError("Only an active mentor of a mentor-shared goal can add a note.")
    return record_activity(locked, mentor, V_MENTOR_NOTE, {"text": (text or "")[:500]})


# --------------------------------------------------------------------------- #
#  Progress (doc 11 §1) — required-milestone weighted, skipped rows excluded
# --------------------------------------------------------------------------- #
# The percent formula and snapshot policy live in progress.py (doc 11 §1, §5); re-exported here for
# callers that imported it from services in Stage 1.
compute_progress_percent = progress.compute_progress_percent


def _recompute_and_save(goal, trigger="recompute") -> int:
    """Recompute + persist progress and write an on-change ``ProgressSnapshot`` (goal assumed
    locked). Delegates to :func:`apps.capsuleer.progress.record_progress` (doc 11 §5)."""
    return progress.record_progress(goal, trigger=trigger)


def recompute_progress(goal) -> int:
    """Public recompute: take the goal row lock, recompute progress, persist and mirror back."""
    with transaction.atomic():
        locked = CareerGoal.objects.select_for_update().get(pk=goal.pk)
        pct = _recompute_and_save(locked)
        _mirror(goal, locked)
    return pct


def _mirror(target, source) -> None:
    """Copy the recomputed columns back onto the caller's instance after a locked write."""
    for field in ("status", "visibility", "started_at", "completed_at", "paused_reason",
                  "progress_percent", "skill_plan_id"):
        setattr(target, field, getattr(source, field))


# --------------------------------------------------------------------------- #
#  Activation side effects (doc 05 §2.6, doc 11 §9) — plan + contribution baselines
# --------------------------------------------------------------------------- #
def _stamp_contribution_baselines(goal) -> None:
    """Stamp ``params.baseline_count`` on every pending contribution milestone at activation
    (future-only evidence, doc 11 §9). The count of matching ledger rows *now* is the baseline the
    checker subtracts, so only events after activation ever credit. Written by direct params
    mutation — the author-facing validator rejects a supplied baseline (doc 07 §6.4)."""
    from apps.pilots.models import ContributionEvent

    for milestone in goal.milestones.filter(
        kind=MilestoneKind.CONTRIBUTION, status=MilestoneStatus.PENDING
    ):
        kinds = (milestone.params or {}).get("kinds", [])
        baseline = ContributionEvent.objects.filter(user_id=goal.user_id, kind__in=kinds).count()
        milestone.params = {**(milestone.params or {}), "baseline_count": baseline}
        milestone.save(update_fields=["params", "updated_at"])


def _on_activation(goal) -> None:
    """Run the first-activation side effects: stamp contribution baselines (correctness-critical, in
    the activation transaction) then generate the skill plan (isolated — a plan-gen failure over
    missing SDE/doctrine data must never block the pilot from activating; the next import or sweep
    retries)."""
    _stamp_contribution_baselines(goal)
    try:
        from . import plan

        with transaction.atomic():
            plan.build_plan(goal)
    except Exception:  # noqa: BLE001 — plan generation is best-effort at activation
        logger.exception("capsuleer build_plan failed for goal %s", goal.pk)


# --------------------------------------------------------------------------- #
#  Verification reconcile (doc 11 §8-§10, doc 12 §4-§5)
# --------------------------------------------------------------------------- #
# Skill-driven kinds ride the import hook (within seconds of a sync); non-skill evidence rides the
# hourly sweep. Both converge on the same locked, one-way credit path.
_SKILL_AUTO_KINDS = frozenset({MilestoneKind.SKILL_TARGET, MilestoneKind.DOCTRINE_READY})
_NONSKILL_AUTO_KINDS = frozenset({
    MilestoneKind.SHIP_OWNED, MilestoneKind.CONTRIBUTION, MilestoneKind.COMBAT_FIRST,
})


def _stamp_check_state(milestone, result) -> None:
    """Record the honesty surface on every evaluation — met or not (doc 11 §8).

    Also persists ``structural_block`` so ``derive_blocked`` can read it on the request path without
    re-running the engine (finding 21)."""
    milestone.last_checked_at = timezone.now()
    milestone.check_state = result.state
    milestone.data_source = (result.data_source or "")[:120]
    milestone.structural_block = bool(getattr(result, "structural", False))
    milestone.save(update_fields=["last_checked_at", "check_state", "data_source",
                                  "structural_block", "updated_at"])


def _credit_auto_milestone(goal_id, milestone_id, result) -> bool:
    """Credit one auto-verified milestone under the goal lock, exactly once (doc 11 §7, §8.2).

    Re-reads the milestone under ``select_for_update`` on the goal row and guards ``pending`` — a
    concurrent hook + sweep race yields one ``pending → done`` transition; the loser no-ops. Freezes
    the checker's evidence into ``evidence_snapshot`` (ADR-0007) and recomputes progress + snapshot.
    Emits ``milestone_reached`` through the disarmed notify chokepoint (in-app only by default).
    Returns whether it credited.
    """
    with transaction.atomic():
        locked = CareerGoal.objects.select_for_update().get(pk=goal_id)
        ms = locked.milestones.select_for_update().get(pk=milestone_id)
        if ms.status != MilestoneStatus.PENDING:
            return False
        now = timezone.now()
        ms.status = MilestoneStatus.DONE
        ms.completed_at = now
        ms.last_checked_at = now
        ms.check_state = result.state if result.state in ("ok", "stale") else "ok"
        ms.data_source = (result.data_source or "")[:120]
        ms.evidence_snapshot = result.evidence or {}
        ms.save(update_fields=["status", "completed_at", "last_checked_at", "check_state",
                               "data_source", "evidence_snapshot", "updated_at"])
        _recompute_and_save(locked, "milestone_credit")
        record_activity(locked, None, "milestone.credited",
                        {"milestone_id": ms.pk, "order": ms.order, "kind": ms.kind, "auto": True})
        transaction.on_commit(lambda g=locked, m=ms: notify.milestone_reached(g, m))
    return True


def _reconcile_milestones(goal_iter, kinds, snapshot_by_char=None) -> dict:
    """Check + credit pending auto milestones of the given kinds across goals, one read context per
    character. ``snapshot_by_char`` threads a fresh snapshot (the import hook); otherwise each
    context loads the character's latest itself. Returns ``{credited, unknown}``."""
    from . import verify

    credited, unknown = 0, 0
    ctx_cache = {}
    for goal in goal_iter:
        char = goal.character
        key = char.character_id if char else None
        ctx = ctx_cache.get(key)
        if ctx is None:
            snap = snapshot_by_char.get(key) if snapshot_by_char else verify._UNSET
            ctx = verify.context_for(char, snap)
            ctx_cache[key] = ctx
        for ms in goal.milestones.all():
            if (ms.status != MilestoneStatus.PENDING or ms.verification != Verification.AUTO
                    or ms.kind not in kinds):
                continue
            result = verify.check_safely(ms, ctx)
            _stamp_check_state(ms, result)
            if result.state == "unknown":
                unknown += 1
            if verify.should_credit(ms.kind, result) and _credit_auto_milestone(goal.pk, ms.pk, result):
                credited += 1
    return {"credited": credited, "unknown": unknown}


def reconcile_from_snapshot(character, snapshot) -> dict:
    """The ``import_character_skills`` side-effect hook body (doc 12 §5).

    Inert while the feature or the reconcile config is disarmed (one cached read). Bounded to the
    character's own ``active`` goals; re-checks their pending skill-driven milestones against the
    fresh snapshot (threaded, no re-query) and credits via the locked path. Never raises into the
    import, calls ESI, or mutates upstream stores.
    """
    from core.features import feature_enabled

    if not feature_enabled("capsuleer"):
        return {"status": "feature_disabled"}
    from . import config

    if not config.get("reconcile")["enabled"]:
        return {"status": "disabled"}
    if getattr(character, "user_id", None) is None:
        return {"status": "no_user"}
    goals = list(
        CareerGoal.objects.filter(character=character, status=GoalStatus.ACTIVE)
        .prefetch_related("milestones")
    )
    if not goals:
        return {"status": "no_goals"}
    result = _reconcile_milestones(goals, _SKILL_AUTO_KINDS,
                                   snapshot_by_char={character.character_id: snapshot})
    return {"status": "ok", **result}


def run_reconcile_sweep() -> dict:
    """``capsuleer.reconcile_progress`` beat body (doc 12 §4.1): credit pending auto milestones whose
    evidence lives in non-skill stores (contribution counts, combat firsts, ship ownership).

    Iterates only users with active goals holding such a pending milestone (indexed), batched per
    user with try/except isolation (``warm_pilots`` pattern) so one pilot never breaks the sweep."""
    counts = {"users": 0, "credited": 0, "unknown": 0, "errors": 0}
    user_ids = (
        CareerMilestone.objects.filter(
            goal__status=GoalStatus.ACTIVE, status=MilestoneStatus.PENDING,
            verification=Verification.AUTO, kind__in=_NONSKILL_AUTO_KINDS,
        ).values_list("goal__user_id", flat=True).distinct()
    )
    for user_id in user_ids:
        counts["users"] += 1
        try:
            goals = (
                CareerGoal.objects.filter(user_id=user_id, status=GoalStatus.ACTIVE)
                .prefetch_related("milestones")
            )
            result = _reconcile_milestones(goals, _NONSKILL_AUTO_KINDS)
            counts["credited"] += result["credited"]
            counts["unknown"] += result["unknown"]
        except Exception:  # noqa: BLE001 — one pilot's failure never aborts the sweep
            counts["errors"] += 1
            logger.exception("capsuleer reconcile failed for user %s", user_id)
    return counts


# --------------------------------------------------------------------------- #
#  Housekeeping (doc 12 §4.3) — retention + stalled/review evaluation
# --------------------------------------------------------------------------- #
def run_housekeeping() -> dict:
    """``capsuleer.housekeeping`` beat body: age-based retention pruning plus stalled/review-due
    flagging (doc 12 §4.3). Each phase is independent and idempotent; deletes are batched."""
    from . import config

    summary = {"snapshots_pruned": 0, "suggestions_pruned": 0, "activity_pruned": 0,
               "reviews_flagged": 0, "errors": 0}
    retention = config.get("retention")
    now = timezone.now()
    try:
        cutoff = now - timedelta(days=int(retention["snapshots_days"]))
        summary["snapshots_pruned"] = _prune_snapshots(cutoff)
    except Exception:  # noqa: BLE001 — a prune failure self-heals on the next nightly run
        summary["errors"] += 1
        logger.exception("capsuleer housekeeping snapshot prune failed")
    try:
        cutoff = now - timedelta(days=int(retention["suggestions_days"]))
        summary["suggestions_pruned"] = _prune_suggestions(now, cutoff)
    except Exception:  # noqa: BLE001
        summary["errors"] += 1
        logger.exception("capsuleer housekeeping suggestion prune failed")
    try:
        cutoff = now - timedelta(days=int(retention["activity_days"]))
        summary["activity_pruned"] = _prune_activity(cutoff)
    except Exception:  # noqa: BLE001
        summary["errors"] += 1
        logger.exception("capsuleer housekeeping activity prune failed")
    try:
        summary["reviews_flagged"] = _flag_reviews(now)
    except Exception:  # noqa: BLE001
        summary["errors"] += 1
        logger.exception("capsuleer housekeeping review flagging failed")
    return summary


def _prune_snapshots(cutoff) -> int:
    """Delete ``ProgressSnapshot`` rows older than ``cutoff``, always keeping the newest per goal so
    a long-idle goal retains its last chart point (the campaigns ``ObjectiveSample`` rule)."""
    from .models import ProgressSnapshot

    newest = (
        ProgressSnapshot.objects.filter(goal_id=OuterRef("goal_id")).order_by("-taken_at", "-id")
    )
    stale = (
        ProgressSnapshot.objects.filter(taken_at__lt=cutoff)
        .annotate(newest_id=Subquery(newest.values("pk")[:1]))
        .exclude(pk=F("newest_id"))
    )
    removed = _batch_delete(ProgressSnapshot, stale)
    return removed + _dedupe_same_day_snapshots()


def _dedupe_same_day_snapshots() -> int:
    """Collapse rare duplicate same-UTC-day ``ProgressSnapshot`` rows from a write race, keeping the
    newest per goal per day (doc 12 §4.3, finding 37) — the guarantee the snapshot writer relies on."""
    from datetime import UTC

    from django.db.models import Count
    from django.db.models.functions import TruncDate

    from .models import ProgressSnapshot

    dupes = (
        ProgressSnapshot.objects.annotate(day=TruncDate("taken_at", tzinfo=UTC))
        .values("goal_id", "day").annotate(n=Count("id")).filter(n__gt=1)
    )
    removed = 0
    for row in dupes:
        ids = list(
            ProgressSnapshot.objects.annotate(day=TruncDate("taken_at", tzinfo=UTC))
            .filter(goal_id=row["goal_id"], day=row["day"])
            .order_by("-taken_at", "-id").values_list("pk", flat=True)
        )
        stale_ids = ids[1:]  # keep the newest row for that goal-day
        if stale_ids:
            ProgressSnapshot.objects.filter(pk__in=stale_ids).delete()
            removed += len(stale_ids)
    return removed


def _prune_suggestions(now, cutoff) -> int:
    """Delete closed (non-open) or expired ``PathSuggestion`` rows last touched before ``cutoff``.
    A kind-level mute already persisted to the profile at act time, so dropping an old
    ``not_interested`` row is safe (doc 12 §4.3)."""
    from .models import PathSuggestion, SuggestionStatus

    stale = PathSuggestion.objects.filter(updated_at__lt=cutoff).filter(
        ~Q(status=SuggestionStatus.OPEN) | Q(expires_at__lt=now)
    )
    return _batch_delete(PathSuggestion, stale)


def _prune_activity(cutoff) -> int:
    """Delete ``GoalActivity`` of **archived** goals older than ``cutoff``. Active-goal activity is
    never pruned; sensitive officer/mentor reads live in ``core.audit`` (730-day floor)."""
    stale = GoalActivity.objects.filter(
        created_at__lt=cutoff, goal__status=GoalStatus.ARCHIVED
    )
    return _batch_delete(GoalActivity, stale)


def _flag_reviews(now) -> int:
    """Set ``review_due_at`` on stalled/past-cadence goals (doc 11 §4.2). No status change, no
    escalation — the entire automated consequence is the flag plus a system activity row."""
    flagged = 0
    candidates = CareerGoal.objects.filter(
        status__in=[GoalStatus.ACTIVE, GoalStatus.PAUSED], review_due_at__isnull=True
    )
    for goal in candidates:
        # Per-goal isolation (doc 12 §1): one goal deleted mid-sweep (account erasure racing the
        # beat) or otherwise poisoned never aborts the remaining candidates (finding 36).
        try:
            if not progress.needs_review_flag(goal, now):
                continue
            with transaction.atomic():
                locked = CareerGoal.objects.select_for_update().filter(pk=goal.pk).first()
                if locked is None or locked.review_due_at is not None:
                    continue
                locked.review_due_at = now
                locked.save(update_fields=["review_due_at", "updated_at"])
                record_activity(locked, None, "review.due_set", {})
                bucket = now.strftime("%Y-%m")
                transaction.on_commit(lambda g=locked, b=bucket: notify.review_due(g, bucket=b))
            flagged += 1
        except Exception:  # noqa: BLE001 — one poisoned goal never breaks the batch (doc 12 §1)
            logger.exception("capsuleer review flagging failed for goal %s", goal.pk)
    return flagged


def _batch_delete(model, queryset, batch=1000) -> int:
    """Delete ``queryset`` in id-chunks so a mid-run crash leaves a smaller, still-valid dataset."""
    total = 0
    while True:
        ids = list(queryset.values_list("pk", flat=True)[:batch])
        if not ids:
            break
        model.objects.filter(pk__in=ids).delete()
        total += len(ids)
        if len(ids) < batch:
            break
    return total


# --------------------------------------------------------------------------- #
#  Review flow (doc 05 §3.4, doc 08 §5.8) — the owner clears a review nudge
# --------------------------------------------------------------------------- #
@transaction.atomic
def complete_review(goal, actor, *, note="") -> CareerGoal:
    """Record an owner review: clear ``review_due_at``, stamp the profile's ``last_reviewed_at``, and
    append a ``review.completed`` activity row (doc 08 §5.8). This condition-clears the ``review_due``
    suggestion next run and stops the review DM. Owner-only."""
    locked = CareerGoal.objects.select_for_update().get(pk=goal.pk)
    if locked.user_id != getattr(actor, "pk", None):
        raise ValidationError("Only the goal owner can review it.")
    locked.review_due_at = None
    locked.save(update_fields=["review_due_at", "updated_at"])
    profile, _ = CareerProfile.objects.get_or_create(user=locked.user)
    profile.last_reviewed_at = timezone.now()
    profile.save(update_fields=["last_reviewed_at", "updated_at"])
    record_activity(locked, actor, "review.completed", {"note": (note or "")[:200]})
    _mirror(goal, locked)
    return locked


# --------------------------------------------------------------------------- #
#  Leadership aggregates (doc 09 §4) — suppressed counts, no names below the tier
# --------------------------------------------------------------------------- #
def _eligible_pipeline_goals():
    """Goals that may aggregate: opt-in or non-private, in a live status (doc 09 §4.1). Abandoned /
    archived never aggregate; budgets and motivations never leave the model."""
    return CareerGoal.objects.filter(
        Q(corp_alignment_optin=True) | ~Q(visibility=Visibility.PRIVATE),
        status__in=[GoalStatus.CONSIDERING, GoalStatus.ACTIVE, GoalStatus.PAUSED],
    )


def _suppress(count, total, min_group) -> bool:
    """A group is suppressed when it is smaller than the floor OR its complement is — the
    complement-inference guard of doc 09 §4.2 (both bounds mandatory)."""
    return count < min_group or (total - count) < min_group


def leadership_pipeline() -> dict:
    """Suppressed aggregate pipeline for the leadership page (doc 09 §4, doc 10 §5.10).

    Counts per template / activity / doctrine over eligible goals, each cell suppressed below the
    ``capsuleer.leadership.min_group`` floor and by the complement guard; nothing is published at all
    when the total itself is below the floor. Names attach in exactly one place — the ``officers``-tier
    goals list. No budget, motivation, or per-pilot count for a suppressed dimension ever appears.
    """
    from django.db.models import Count

    from apps.capsuleer import config

    min_group = max(2, int(config.get("leadership").get("min_group", 4)))
    eligible = _eligible_pipeline_goals()
    total = eligible.count()

    def _dim(field):
        rows = eligible.values(field).annotate(n=Count("id")).order_by("-n")
        return [(r[field], r["n"]) for r in rows if r[field] not in (None, "")]

    published = total >= min_group
    by_template = _resolve_templates(_dim("template_key"), total, min_group, published)
    by_activity = _resolve_activities(_dim("activity"), total, min_group, published)
    by_doctrine = _resolve_doctrines(_dim("doctrine_id"), total, min_group, published)

    completed_90d_count = CareerGoal.objects.filter(
        status=GoalStatus.COMPLETED, completed_at__gte=timezone.now() - timedelta(days=90),
    ).filter(Q(corp_alignment_optin=True) | ~Q(visibility=Visibility.PRIVATE)).count()
    # The completed counter obeys the same floor as every other cell — an exact 1..N-1 below the
    # floor would let an officer attribute a never-shared goal's completion (finding 11).
    completed_90d = {
        "count": completed_90d_count,
        "suppressed": (not published) or completed_90d_count < min_group,
    }

    shared = []
    for goal in (
        CareerGoal.objects.filter(visibility=Visibility.OFFICERS)
        .filter(status__in=[GoalStatus.CONSIDERING, GoalStatus.ACTIVE, GoalStatus.PAUSED,
                            GoalStatus.COMPLETED])
        # Prefetch characters so ``display_name`` (main character) resolves without a per-goal query.
        .select_related("user").prefetch_related("user__characters").order_by("-updated_at")[:100]
    ):
        shared.append({
            "pilot": _pilot_display(goal.user),
            "title": goal.title,
            "progress": goal.progress_percent,
            "goal_id": goal.pk,
            "status": goal.status,
        })

    return {
        "min_group": min_group,
        "total": total,
        "published": published,
        "by_template": by_template,
        "by_activity": by_activity,
        "by_doctrine": by_doctrine,
        "completed_90d": completed_90d,
        "shared_with_officers": shared,
    }


def _pilot_display(user) -> str:
    """A pilot's display name (main character), never a raw id."""
    return getattr(user, "display_name", None) or user.get_username()


# Suppressed dimension rows are dropped entirely — a below-floor group renders identically to an
# empty one (absence), so an officer cannot tell "1..N-1 pilots are on this path" from "nobody is"
# (finding 10, doc 09 §4.2). Only published groups at or above the floor (and complement) survive.
def _resolve_templates(dim, total, min_group, published):
    from .models import CareerTemplate

    names = dict(CareerTemplate.objects.filter(key__in=[k for k, _ in dim]).values_list("key", "name"))
    return [
        {"label": names.get(key, key), "count": n}
        for key, n in dim
        if published and not _suppress(n, total, min_group)
    ]


def _resolve_activities(dim, total, min_group, published):
    from .taxonomy import Activity

    labels = dict(Activity.choices)
    return [
        {"label": labels.get(act, act), "count": n}
        for act, n in dim
        if published and not _suppress(n, total, min_group)
    ]


def _resolve_doctrines(dim, total, min_group, published):
    from apps.doctrines.models import Doctrine

    names = dict(Doctrine.objects.filter(id__in=[d for d, _ in dim]).values_list("id", "name"))
    return [
        {"label": names.get(did, "unknown doctrine"), "count": n}
        for did, n in dim
        if published and not _suppress(n, total, min_group)
    ]


# --------------------------------------------------------------------------- #
#  Dashboard panel context (doc 10 §5.11) — status only, no advice
# --------------------------------------------------------------------------- #
_DASHBOARD_PRIORITY_RANK = {Priority.PRIMARY: 0, Priority.SECONDARY: 1, Priority.SOMEDAY: 2}


def _panel_from_goals(goals) -> dict:
    """The Command-Center panel dict from a pre-sorted (freshest-first) active-goal list."""
    primary = goals[0]
    second = goals[1] if len(goals) > 1 else None
    # Defence-in-depth: the panel exposes the goal object, but the panel template only reads
    # pk/title/progress_percent — never the N-class budget. Blank ``budget_isk`` on the exposed
    # references so a director view-as of the dashboard can never carry a pilot's wallet figure
    # through the panel context, even if a future template edit dereferenced it.
    primary.budget_isk = None
    if second is not None:
        second.budget_isk = None
    milestones = sorted(primary.milestones.all(), key=lambda m: m.order)
    next_ms = next(
        (m for m in milestones if m.required and m.status == MilestoneStatus.PENDING), None
    )
    # Freshness from the already-loaded milestones: the most recent evidence check, else the goal's
    # last update (doc 10 §5.11, finding 47) — no extra query on the dashboard's hottest path.
    checked = [m.last_checked_at for m in milestones if m.last_checked_at]
    return {
        "goal": primary,
        "next_milestone": next_ms.title if next_ms else None,
        "review_due": primary.review_due_at is not None,
        "second": second,
        "freshness": max(checked) if checked else primary.updated_at,
    }


def dashboard_panel(user) -> dict | None:
    """The Command-Center panel context for ``user``: the primary active goal, its progress, next
    milestone and freshness — or ``None`` when the pilot has no active goal (omitted, never hollow)."""
    goals = list(
        CareerGoal.objects.filter(user=user, status=GoalStatus.ACTIVE).prefetch_related("milestones")
    )
    if not goals:
        return None
    goals.sort(key=lambda g: (_DASHBOARD_PRIORITY_RANK.get(g.priority, 3), -g.pk))
    return _panel_from_goals(goals)


def dashboard_bundle(user, *, include_panel=True, include_quests=True) -> dict:
    """One shared active-goal fetch feeding both the dashboard panel and the career quest row
    (doc 10 §5.11/§5.12), so the whole dashboard delta stays within its ≤3-query budget (finding 22).

    The panel is skipped when the pilot has hidden it (no work for a hidden panel). Both projections
    read the same prefetched goals — no second fetch of the same active goals.
    """
    from . import briefing

    goals = list(
        CareerGoal.objects.filter(user=user, status=GoalStatus.ACTIVE)
        .prefetch_related("milestones", "action_steps")
    )
    if not goals:
        return {"panel": None, "quests": []}
    goals.sort(key=lambda g: (_DASHBOARD_PRIORITY_RANK.get(g.priority, 3), -g.pk))
    return {
        "panel": _panel_from_goals(goals) if include_panel else None,
        "quests": briefing.career_quests_from_goals(goals) if include_quests else [],
    }
