"""Leadership reporting aggregates for the Mentorship Program dashboard."""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

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
    """Enrolment and completion totals per active track, in one query.

    Both numbers come off the SAME relation, so they are conditional aggregates over a
    single ``enrollments`` join rather than two COUNTs per row. Sharing one join is also
    what makes them safe: two ``Count`` annotations that span *different* relations
    multiply each other's rows (a track with 3 enrolments and 4 tasks would report 12 of
    each) and would need ``distinct=True``. That trap does not apply here, and forcing
    ``distinct`` anyway would be a pointless sort.
    """
    tracks = (
        MentorshipTrack.objects.filter(active=True)
        .annotate(
            enrolled_n=Count("enrollments"),
            completed_n=Count(
                "enrollments",
                filter=Q(enrollments__status=MentorshipEnrollment.Status.COMPLETED),
            ),
        )
        .order_by("sort_order")
    )
    rows = []
    for track in tracks:
        enrolled = track.enrolled_n
        completed = track.completed_n
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
    # Both counts read the mentor's own ``pairings``, so one join answers both — see
    # ``track_completion_rates`` for why sharing the relation is what keeps the two
    # annotations from inflating each other.
    mentors = (
        MentorProfile.objects.filter(status=MentorProfile.Status.ACTIVE)
        .select_related("user")
        .annotate(
            active_pairing_n=Count("pairings", filter=Q(pairings__status=_P.ACTIVE)),
            completed_pairing_n=Count("pairings", filter=Q(pairings__status=_P.COMPLETED)),
        )
        # Restated explicitly, not decoration: Django DROPS a model's ``Meta.ordering``
        # the moment a query grows a GROUP BY (compiler: ``if self._meta_ordering:
        # order_by = None``). Without this line the annotated queryset comes back
        # unordered, and since the sort below is stable that silently changes which
        # mentors survive ``[:limit]`` whenever two of them tie on (mentees, signoffs).
        .order_by("-created_at")
    )
    for mentor in mentors:
        rows.append({
            "mentor": mentor,
            "active_mentees": mentor.active_pairing_n,
            "completed": mentor.completed_pairing_n,
            "signoffs": signoffs.get(mentor.user_id, 0),
        })
    rows.sort(key=lambda r: (r["active_mentees"], r["signoffs"]), reverse=True)
    return rows[:limit]


def mentees_needing_attention(limit: int = 10) -> list[dict]:
    """Active mentees who are unpaired or whose pair has stalled/no progress."""
    from . import matching

    out = [{"mentee": m, "why": _("Not yet paired")} for m in matching.unpaired_mentees()]
    # "Has tasks but none done" is two counts over the one ``assignments`` relation, so
    # they annotate onto the same join instead of costing two COUNTs per active pairing.
    pairings = (
        MentorshipPairing.objects.filter(status=_P.ACTIVE)
        .select_related("mentee__user", "mentor__user")
        .annotate(
            done_n=Count(
                "assignments",
                filter=Q(assignments__status__in=MentorshipTaskAssignment.DONE_STATUSES),
            ),
            total_n=Count("assignments"),
        )
        # See ``mentor_activity``: adding a GROUP BY makes Django discard
        # ``Meta.ordering``, and here the order is what the officer actually reads
        # (newest pairings first) before ``[:limit]`` cuts the list.
        .order_by("-created_at")
    )
    for pairing in pairings:
        done = pairing.done_n
        total = pairing.total_n
        if total and done == 0:
            out.append({"mentee": pairing.mentee, "why": _("Paired but no tasks completed yet"),
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
