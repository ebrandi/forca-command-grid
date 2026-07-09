"""Leadership reporting aggregates for the Mentorship Program dashboard."""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from . import rewards
from .models import (
    MenteeProfile,
    MentorProfile,
    MentorshipEnrollment,
    MentorshipPairing,
    MentorshipRewardLedger,
    MentorshipTaskAssignment,
    MentorshipTaskValidation,
    MentorshipTrack,
)

_P = MentorshipPairing.Status
_R = MentorshipRewardLedger.Status


def program_summary() -> dict:
    from . import services

    program = services.active_program()
    now = timezone.now()
    stale_days = program.stale_pair_days or 14
    stale_cut = now.timestamp() - stale_days * 86400

    active_pairs = MentorshipPairing.objects.filter(status=_P.ACTIVE)
    stalled = [
        p for p in active_pairs
        if (p.last_activity_at or p.started_at or p.created_at)
        and (p.last_activity_at or p.started_at or p.created_at).timestamp() < stale_cut
    ]
    return {
        "active_mentors": MentorProfile.objects.filter(status=MentorProfile.Status.ACTIVE).count(),
        "active_mentees": MenteeProfile.objects.filter(status=MenteeProfile.Status.ACTIVE).count(),
        "pending_mentor_apps": MentorProfile.objects.filter(status=MentorProfile.Status.PENDING).count(),
        "pending_mentee_apps": MenteeProfile.objects.filter(status=MenteeProfile.Status.PENDING).count(),
        "pending_pairings": MentorshipPairing.objects.filter(status=_P.PENDING_APPROVAL).count(),
        "suggested_pairings": MentorshipPairing.objects.filter(status=_P.SUGGESTED).count(),
        "active_pairs": active_pairs.count(),
        "paused_pairs": MentorshipPairing.objects.filter(status=_P.PAUSED).count(),
        "completed_pairs": MentorshipPairing.objects.filter(status=_P.COMPLETED).count(),
        "stalled_pairs": len(stalled),
        "reward_liability": rewards.outstanding_isk(),
        "rewards_pending_approval": MentorshipRewardLedger.objects.filter(
            status=_R.PENDING_APPROVAL).count(),
        "rewards_paid_total": MentorshipRewardLedger.objects.filter(status=_R.PAID).aggregate(
            t=Sum("amount"))["t"] or Decimal(0),
        "open_flags": _open_flag_count(),
    }


def _open_flag_count() -> int:
    from .models import MentorshipFlag
    return MentorshipFlag.objects.filter(resolved=False).count()


def track_completion_rates() -> list[dict]:
    rows = []
    for track in MentorshipTrack.objects.filter(active=True).order_by("sort_order"):
        enrolled = track.enrollments.count()
        completed = track.enrollments.filter(status=MentorshipEnrollment.Status.COMPLETED).count()
        rows.append({
            "track": track,
            "enrolled": enrolled,
            "completed": completed,
            "pct": int(round(100 * completed / enrolled)) if enrolled else 0,
        })
    return rows


def mentor_activity(limit: int = 10) -> list[dict]:
    """Most active mentors: active mentees + tasks they've signed off."""
    signoffs = dict(
        MentorshipTaskValidation.objects.filter(
            source=MentorshipTaskValidation.Source.MENTOR,
            result=MentorshipTaskValidation.Result.PASS,
        ).values_list("actor").annotate(n=Count("id")).values_list("actor", "n")
    )
    rows = []
    for mentor in MentorProfile.objects.filter(status=MentorProfile.Status.ACTIVE).select_related("user"):
        active = mentor.pairings.filter(status=_P.ACTIVE).count()
        completed = mentor.pairings.filter(status=_P.COMPLETED).count()
        rows.append({
            "mentor": mentor,
            "active_mentees": active,
            "completed": completed,
            "signoffs": signoffs.get(mentor.user_id, 0),
        })
    rows.sort(key=lambda r: (r["active_mentees"], r["signoffs"]), reverse=True)
    return rows[:limit]


def mentees_needing_attention(limit: int = 10) -> list[dict]:
    """Active mentees who are unpaired or whose pair has stalled/no progress."""
    from . import matching

    out = [{"mentee": m, "why": "Not yet paired"} for m in matching.unpaired_mentees()]
    for pairing in MentorshipPairing.objects.filter(status=_P.ACTIVE).select_related(
        "mentee__user", "mentor__user"
    ):
        done = pairing.assignments.filter(
            status__in=MentorshipTaskAssignment.DONE_STATUSES).count()
        total = pairing.assignments.count()
        if total and done == 0:
            out.append({"mentee": pairing.mentee, "why": "Paired but no tasks completed yet",
                        "pairing": pairing})
    return out[:limit]


def commonly_rejected_tasks(limit: int = 10) -> list[dict]:
    rows = (
        MentorshipTaskValidation.objects.filter(result=MentorshipTaskValidation.Result.FAIL)
        .values("assignment__task__key", "assignment__task__title")
        .annotate(n=Count("id"))
        .order_by("-n")[:limit]
    )
    return [{"key": r["assignment__task__key"], "title": r["assignment__task__title"], "count": r["n"]}
            for r in rows]


def reward_ledger(status: str | None = None, role: str | None = None):
    qs = MentorshipRewardLedger.objects.select_related(
        "recipient", "rule", "pairing__mentor__user", "pairing__mentee__user"
    ).prefetch_related("recipient__characters")
    if status:
        qs = qs.filter(status=status)
    if role:
        qs = qs.filter(recipient_role=role)
    return qs
