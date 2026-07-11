"""Capsuleer Path progress model: percent, snapshots, training ETA and stalled detection (doc 11).

Pure-ish functions over a goal and its milestones/plan, called only from the service layer under
the goal row lock (doc 11 §7). The percent formula is required-milestone-weighted with skipped rows
excluded (§1.1); ETA is an honest *range* derived from the goal's skill plan and pace, never a
single false date (§3); ``ProgressSnapshot`` writes are on-change with a one-per-UTC-day cap (§5);
stalled/review detection sets a flag the owner can act on or ignore, with zero punishment semantics
(§4). Nothing here computes on the request path — pages render the cached percent plus a snapshot-
derived freshness chip.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from .models import GoalStatus, MilestoneStatus, ProgressSnapshot

_UNSET = object()

# Pace models queue-gap slack, not training speed (skills train offline at a fixed rate). These are
# documented assumptions and candidates for later promotion to AppSetting keys — deliberately not in
# the brief §9 config set for Phase 1 (doc 11 §3).
PACE_UTILISATION = {"accelerated": 1.00, "balanced": 1.15, "relaxed": 1.35}

# Stalled/review thresholds — module constants, not config keys (doc 11 §4.1).
STALLED_AFTER_DAYS = 45
REVIEW_CADENCE_DAYS = 90

# The verbatim ETA assumptions surfaced on the goal page (doc 11 §3).
ETA_ASSUMPTIONS = (
    "continuous skill queue",
    "current neural attributes",
    "no implants, boosters or accelerators modelled",
    "Omega training rates assumed",
)


# --------------------------------------------------------------------------- #
#  Percent (doc 11 §1.1)
# --------------------------------------------------------------------------- #
def _counted_pool(goal) -> list:
    """The milestones the percent formula counts over: required non-skipped, or — when a goal has
    no required milestones — all non-skipped ones (an all-optional goal still shows progress)."""
    milestones = list(goal.milestones.all())
    non_skipped = [m for m in milestones if m.status != MilestoneStatus.SKIPPED]
    required = [m for m in non_skipped if m.required]
    return required if required else non_skipped


def milestone_counts(goal) -> tuple[int, int]:
    """``(done, total)`` over the counted pool (doc 11 §1.1)."""
    pool = _counted_pool(goal)
    done = sum(1 for m in pool if m.status == MilestoneStatus.DONE)
    return done, len(pool)


def compute_progress_percent(goal) -> int:
    """Required-milestone-weighted completion percent (doc 11 §1.1).

    A completed goal reads 100 (terminal display value, honest even on an owner override — the
    override lives in the activity trail). Otherwise floor of ``100 × done / total`` over the
    counted pool (weight 1 in Phase 1); an empty pool reads 0 (never fabricate progress).
    """
    if goal.status == GoalStatus.COMPLETED:
        return 100
    done, total = milestone_counts(goal)
    if not total:
        return 0
    return (100 * done) // total


# --------------------------------------------------------------------------- #
#  Snapshots (doc 11 §5) — on-change, one per UTC day
# --------------------------------------------------------------------------- #
def record_progress(goal, *, trigger="recompute", sp_remaining=_UNSET) -> int:
    """Recompute + persist ``goal.progress_percent`` (goal assumed locked) and, on change, write a
    ``ProgressSnapshot`` within the one-per-day cap. Returns the percent."""
    pct = compute_progress_percent(goal)
    if goal.progress_percent != pct:
        goal.progress_percent = pct
        goal.save(update_fields=["progress_percent", "updated_at"])
    maybe_write_snapshot(goal, trigger, pct=pct, sp_remaining=sp_remaining)
    return pct


def maybe_write_snapshot(goal, trigger, *, pct=None, sp_remaining=_UNSET):
    """Write one ``ProgressSnapshot`` iff percent/milestones-done changed and none exists today.

    A flat period is represented by the gap between points; charts interpolate. A rare duplicate
    same-day row from a write race is harmless and pruned by housekeeping (doc 11 §5).
    """
    if pct is None:
        pct = compute_progress_percent(goal)
    done, total = milestone_counts(goal)
    now = timezone.now()
    latest = goal.snapshots.order_by("-taken_at", "-id").first()
    if latest is not None:
        if latest.taken_at.date() == now.date():
            return None  # daily cap
        if latest.percent == pct and latest.milestones_done == done:
            return None  # unchanged
    if sp_remaining is _UNSET:
        sp_remaining = plan_sp_remaining(goal)
    return ProgressSnapshot.objects.create(
        goal=goal, taken_at=now, percent=pct, milestones_done=done, milestones_total=total,
        sp_remaining=sp_remaining, notes={"trigger": trigger},
    )


# --------------------------------------------------------------------------- #
#  Training ETA (doc 11 §3) — honest range, never a fabricated date
# --------------------------------------------------------------------------- #
def _effective_pace(goal) -> str:
    """The goal's pace, resolving ``inherit`` to the profile default (else ``balanced``)."""
    from .models import CareerProfile, GoalPace

    if goal.pace != GoalPace.INHERIT:
        return goal.pace
    profile = CareerProfile.objects.filter(user_id=goal.user_id).first()
    return profile.pace if profile else "balanced"


def _plan_and_snapshot(goal):
    from apps.skills.models import SkillPlan

    if not goal.skill_plan_id:
        return None, None
    plan = (
        SkillPlan.objects.filter(pk=goal.skill_plan_id).prefetch_related("steps").first()
    )
    snapshot = (
        goal.character.skill_snapshots.filter(is_latest=True).first() if goal.character else None
    )
    return plan, snapshot


def plan_sp_remaining(goal) -> int | None:
    """SP still to train across the plan's not-done steps, or ``None`` when there is no plan
    (never ``0``-as-unknown). ``0`` means a real plan with nothing left to train."""
    from apps.sde.models import SdeType
    from apps.skills.models import SkillPlanStep
    from apps.skills.services import sp_between_levels

    plan, snapshot = _plan_and_snapshot(goal)
    if plan is None:
        return None
    steps = [s for s in plan.steps.all() if s.status != SkillPlanStep.Status.DONE]
    if not steps:
        return 0
    ranks = dict(
        SdeType.objects.filter(type_id__in={s.skill_type_id for s in steps})
        .values_list("type_id", "rank")
    )
    total = 0
    for step in steps:
        have = snapshot.trained_level(step.skill_type_id) if snapshot else 0
        total += sp_between_levels(ranks.get(step.skill_type_id) or 1, have, step.target_level)
    return total


def estimate_eta(goal, *, plan=None, snapshot=None) -> dict:
    """An honest training-time estimate for the goal's skill plan (doc 11 §3).

    Returns ``{"state": "unknown", "reason": ...}`` when there is no plan or nothing to compute, or
    ``{"state": "ok", "earliest", "likely", "pace", "as_of", "assumptions"}`` — a *range* (earliest =
    continuous queue at current attributes; likely = earliest × the pace utilisation multiplier),
    never a single date. Practical/contribution/ownership milestones have no time model, so this is
    explicitly a *training* estimate. ``plan``/``snapshot`` may be supplied by a caller that already
    fetched them (goal_detail) to avoid the duplicate query (finding 25).
    """
    from apps.skills.services import remaining_seconds

    if plan is None:
        plan, snapshot = _plan_and_snapshot(goal)
    if plan is None:
        return {"state": "unknown", "reason": "No skill plan yet."}
    earliest_seconds = remaining_seconds(plan)
    if earliest_seconds <= 0:
        return {"state": "unknown", "reason": "No training left on this plan."}
    pace = _effective_pace(goal)
    likely_seconds = int(earliest_seconds * PACE_UTILISATION.get(pace, 1.15))
    now = timezone.now()
    return {
        "state": "ok",
        "earliest": now + timedelta(seconds=earliest_seconds),
        "likely": now + timedelta(seconds=likely_seconds),
        "earliest_seconds": earliest_seconds,
        "likely_seconds": likely_seconds,
        "pace": pace,
        "as_of": snapshot.as_of if snapshot else None,
        "assumptions": list(ETA_ASSUMPTIONS),
    }


# --------------------------------------------------------------------------- #
#  Stalled / review-due detection (doc 11 §4) — a flag, never a punishment
# --------------------------------------------------------------------------- #
def _last_movement_at(goal):
    """The most recent sign the pilot engaged with the goal: an owner-authored activity row, a
    progress snapshot, a milestone completion, else the goal's creation. System (actor-null) rows
    never count — automation touching a goal is not the pilot engaging with it (doc 11 §4.1)."""
    times = [goal.created_at]
    act = goal.activity_log.filter(actor__isnull=False).order_by("-created_at").first()
    if act:
        times.append(act.created_at)
    snap = goal.snapshots.order_by("-taken_at").first()
    if snap:
        times.append(snap.taken_at)
    ms = goal.milestones.filter(completed_at__isnull=False).order_by("-completed_at").first()
    if ms:
        times.append(ms.completed_at)
    return max(times)


def derive_blocked(goal, *, milestones=None, live=True) -> tuple[bool, list[str]]:
    """Whether a goal is structurally blocked, and the verbatim reasons (doc 11 §2, doc 08 §5.5).

    A goal is blocked when a **required, pending** milestone has a permanent structural blocker —
    a dangling/retired doctrine, an unresolved ``$doctrine`` placeholder, or a detached evidence
    character — surfaced as ``structural=True`` from the verification engine. Missing skill data or
    an un-opted asset scope are *data* conditions, not blockers (the pilot can still work the plan),
    so they are excluded by construction (their checks return a non-structural ``unknown``).

    ``live=True`` (the sweep and the ``blocked_prereq`` suggestion rule) runs the checkers and
    stamps the result. ``live=False`` (the goal_detail request path — doc 11 §2, finding 21) reads
    the persisted ``structural_block`` flag off the already-loaded milestones, spending zero extra
    queries; the sweep, the import hook and template instantiation keep that flag fresh.
    """
    from . import verify

    if milestones is None:
        milestones = list(goal.milestones.all())
    # Only auto-checkable kinds can be structurally blocked; human-verified milestones
    # (practical/manual) are never blockers — they have no checker and await the pilot.
    candidates = [
        m for m in milestones
        if m.required and m.status == MilestoneStatus.PENDING and m.kind in verify.CHECKERS
    ]
    if not candidates:
        return False, []
    reasons: list[str] = []
    if not live:
        for milestone in candidates:
            if milestone.structural_block:
                reason = (milestone.data_source or "").strip()
                if reason and reason not in reasons:
                    reasons.append(reason)
        return bool(reasons), reasons
    ctx = verify.context_for(goal.character)
    for milestone in candidates:
        result = verify.check_safely(milestone, ctx)
        if result.structural:
            reason = (result.data_source or "").strip()
            if reason and reason not in reasons:
                reasons.append(reason)
    return bool(reasons), reasons


def needs_review_flag(goal, now=None) -> bool:
    """Whether housekeeping should set ``review_due_at`` on this goal (doc 11 §4.2).

    An ``active`` goal with no movement for ``STALLED_AFTER_DAYS`` (stalled), or an ``active``/
    ``paused`` goal with no movement for ``REVIEW_CADENCE_DAYS`` (routine cadence) whose
    ``review_due_at`` is unset. ``considering``/terminal goals are never scanned. No status change
    is implied — this only flags.
    """
    if goal.review_due_at is not None:
        return False
    if goal.status not in (GoalStatus.ACTIVE, GoalStatus.PAUSED):
        return False
    now = now or timezone.now()
    idle = now - _last_movement_at(goal)
    if goal.status == GoalStatus.ACTIVE and idle >= timedelta(days=STALLED_AFTER_DAYS):
        return True
    return idle >= timedelta(days=REVIEW_CADENCE_DAYS)
