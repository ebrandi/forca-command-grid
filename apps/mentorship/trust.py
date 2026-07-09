"""Anti-abuse: anomaly detection over active pairings.

Raises ``MentorshipFlag`` rows for a leader to review. Every flag is *advisory* —
it never auto-reverses a completion or a reward. Flags dedupe on a stable
``dedupe_key`` (one open flag per pairing+kind) so the sweep is idempotent.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import (
    MentorshipFlag,
    MentorshipPairing,
    MentorshipTaskAssignment,
    MentorshipTaskValidation,
)

RAPID_WINDOW_MINUTES = 15
RAPID_COUNT = 5
SELF_CONFIRM_STREAK = 6
RUBBER_STAMP_SECONDS = 20
RUBBER_STAMP_COUNT = 5


def _raise(kind, *, pairing=None, user=None, severity=50, detail="", key) -> bool:
    _, created = MentorshipFlag.objects.get_or_create(
        dedupe_key=key, resolved=False,
        defaults={"kind": kind, "severity": severity, "pairing": pairing,
                  "user": user, "detail": detail[:300]},
    )
    return created


def scan_pairing(pairing: MentorshipPairing) -> int:
    """Evaluate one pairing; upsert any anomaly flags. Returns flags raised."""
    from . import services

    raised = 0
    now = timezone.now()
    program = services.active_program()

    # 1) Rapid completion: many completions clustered in a short window.
    recent = list(
        pairing.assignments.filter(
            completed_at__isnull=False, completed_at__gte=now - timedelta(minutes=RAPID_WINDOW_MINUTES)
        ).order_by("completed_at")
    )
    if len(recent) >= RAPID_COUNT:
        raised += _raise(
            MentorshipFlag.Kind.RAPID_COMPLETION, pairing=pairing, severity=60,
            detail=f"{len(recent)} tasks completed within {RAPID_WINDOW_MINUTES} min.",
            key=f"rapid:{pairing.pk}:{recent[-1].completed_at:%Y%m%d%H%M}",
        )

    # 2) Self-confirm streak: many completions validated only by the mentee.
    done = list(
        pairing.assignments.filter(
            status__in=MentorshipTaskAssignment.DONE_STATUSES
        ).order_by("-completed_at")[:SELF_CONFIRM_STREAK]
    )
    if len(done) >= SELF_CONFIRM_STREAK:
        self_only = 0
        for a in done:
            has_other = a.validations.filter(
                ~Q(source=MentorshipTaskValidation.Source.MENTEE),
                result=MentorshipTaskValidation.Result.PASS,
            ).exists()
            if not has_other:
                self_only += 1
        if self_only >= SELF_CONFIRM_STREAK:
            raised += _raise(
                MentorshipFlag.Kind.SELF_CONFIRM_STREAK, pairing=pairing, severity=45,
                detail=f"{self_only} recent tasks completed by mentee self-confirmation only.",
                key=f"selfconfirm:{pairing.pk}",
            )

    # 3) Mentor rubber-stamping: many near-instant approvals.
    quick = 0
    for v in MentorshipTaskValidation.objects.filter(
        assignment__pairing=pairing, source=MentorshipTaskValidation.Source.MENTOR,
        result=MentorshipTaskValidation.Result.PASS,
    ).select_related("assignment")[:20]:
        sub = v.assignment.submitted_at
        if sub and (v.created_at - sub).total_seconds() < RUBBER_STAMP_SECONDS:
            quick += 1
    if quick >= RUBBER_STAMP_COUNT:
        raised += _raise(
            MentorshipFlag.Kind.MENTOR_RUBBER_STAMP, pairing=pairing, severity=50,
            detail=f"{quick} mentor approvals landed <{RUBBER_STAMP_SECONDS}s after submission.",
            key=f"rubberstamp:{pairing.pk}",
        )

    # 4) Capacity drift (defence in depth — the claim guard should prevent this).
    if services.mentor_active_count(pairing.mentor) > services.mentor_capacity(pairing.mentor):
        raised += _raise(
            MentorshipFlag.Kind.CAPACITY_EXCEEDED, pairing=pairing, user=pairing.mentor.user,
            severity=40, detail="Mentor is over their active-mentee capacity.",
            key=f"capacity:{pairing.mentor_id}",
        )

    # 5) Stale pair.
    if program.stale_pair_days and pairing.status == MentorshipPairing.Status.ACTIVE:
        ref = pairing.last_activity_at or pairing.started_at or pairing.created_at
        if ref and (now - ref) > timedelta(days=program.stale_pair_days):
            raised += _raise(
                MentorshipFlag.Kind.STALE_PAIR, pairing=pairing, severity=35,
                detail=f"No activity for {program.stale_pair_days}+ days.",
                key=f"stale:{pairing.pk}",
            )
    return raised


def run_scan() -> int:
    total = 0
    for pairing in MentorshipPairing.objects.filter(
        status__in=[MentorshipPairing.Status.ACTIVE, MentorshipPairing.Status.PAUSED]
    ).select_related("mentor__user", "mentee__user"):
        total += scan_pairing(pairing)
    return total


def resolve_flag(flag: MentorshipFlag, officer) -> bool:
    if flag.resolved:
        return False
    flag.resolved = True
    flag.resolved_by = officer
    flag.resolved_at = timezone.now()
    flag.dedupe_key = ""  # free the unique-open-flag slot
    flag.save(update_fields=["resolved", "resolved_by", "resolved_at", "dedupe_key", "updated_at"])
    return True


def open_flags():
    return MentorshipFlag.objects.filter(resolved=False).select_related(
        "pairing__mentor__user", "pairing__mentee__user", "user"
    )
