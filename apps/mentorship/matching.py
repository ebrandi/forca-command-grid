"""Mentor ↔ mentee matching recommendations.

A transparent, explainable score (0–100) over interest overlap, time-zone,
language, mentor capacity and history — never a black box. Leadership can accept a
suggestion, pair manually, or let either pilot initiate; the score is advisory.
"""
from __future__ import annotations

from .models import MenteeProfile, MentorProfile, MentorshipPairing

# Weights (sum of the positive components ≈ 100).
_W_INTEREST = 45
_W_TZ = 22
_W_LANG = 15
_W_CAPACITY = 8
_W_ADHOC = 5
_W_VOICE = 5
_PENALTY_PRIOR_CANCEL = 25


def _as_set(values) -> set[str]:
    return {str(v).strip().lower() for v in (values or []) if str(v).strip()}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def score(mentor: MentorProfile, mentee: MenteeProfile, *, services=None) -> tuple[float, list[str]]:
    """Return (score 0–100, human reasons). Assumes both are ACTIVE."""
    from . import services as svc

    services = services or svc
    reasons: list[str] = []
    total = 0.0

    mentor_areas = _as_set(mentor.areas)
    mentee_wants = _as_set(mentee.goals) | _as_set(mentee.interests)
    overlap = mentor_areas & mentee_wants
    if overlap:
        j = _jaccard(mentor_areas, mentee_wants)
        total += _W_INTEREST * j
        reasons.append(f"Shared focus: {', '.join(sorted(overlap))}.")
    else:
        reasons.append("No overlapping focus areas.")

    if mentor.timezone and mentee.timezone:
        if mentor.timezone.strip().lower() == mentee.timezone.strip().lower():
            total += _W_TZ
            reasons.append(f"Same time zone ({mentor.timezone}).")

    mentor_langs = _as_set(mentor.languages)
    mentee_langs = _as_set(mentee.languages)
    if mentor_langs & mentee_langs:
        total += _W_LANG
        reasons.append(f"Shared language: {', '.join(sorted(mentor_langs & mentee_langs))}.")

    capacity = services.mentor_capacity(mentor)
    active = services.mentor_active_count(mentor)
    if active < capacity:
        # More free slots → slightly higher score (spread the load).
        total += _W_CAPACITY * (capacity - active) / capacity
        reasons.append(f"Has capacity ({active}/{capacity} mentees).")

    if mentor.open_to_adhoc:
        total += _W_ADHOC
        reasons.append("Open to ad-hoc questions.")

    if mentee.voice_comfortable and mentor.comms:
        total += _W_VOICE

    # Penalise a pair that was previously cancelled.
    prior_cancel = MentorshipPairing.objects.filter(
        mentor=mentor, mentee=mentee,
        status__in=[MentorshipPairing.Status.CANCELLED, MentorshipPairing.Status.EXPIRED],
    ).exists()
    if prior_cancel:
        total -= _PENALTY_PRIOR_CANCEL
        reasons.append("Previously unpaired — lower priority.")

    return max(0.0, min(100.0, round(total, 1))), reasons


def suggest_mentors_for(mentee: MenteeProfile, limit: int = 5) -> list[dict]:
    from . import services

    out = []
    for mentor in MentorProfile.objects.filter(status=MentorProfile.Status.ACTIVE).select_related("user"):
        if services.existing_open_pairing(mentor, mentee):
            continue
        if not services.mentor_has_capacity(mentor):
            continue
        s, reasons = score(mentor, mentee, services=services)
        out.append({"mentor": mentor, "score": s, "reasons": reasons})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:limit]


def suggest_mentees_for(mentor: MentorProfile, limit: int = 5) -> list[dict]:
    from . import services

    if not services.mentor_has_capacity(mentor):
        return []
    out = []
    for mentee in MenteeProfile.objects.filter(status=MenteeProfile.Status.ACTIVE).select_related("user"):
        if services.existing_open_pairing(mentor, mentee):
            continue
        s, reasons = score(mentor, mentee, services=services)
        out.append({"mentee": mentee, "score": s, "reasons": reasons})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:limit]


def unpaired_mentees() -> list[MenteeProfile]:
    """Active mentees with no open pairing — the leader's matching worklist."""
    paired = set(
        MentorshipPairing.objects.filter(status__in=MentorshipPairing.OPEN_STATUSES)
        .values_list("mentee_id", flat=True)
    )
    return list(
        MenteeProfile.objects.filter(status=MenteeProfile.Status.ACTIVE)
        .exclude(id__in=paired)
        .select_related("user")
    )


def auto_suggest(limit_per_mentee: int = 1) -> int:
    """Create SUGGESTED pairings for the best match of each unpaired mentee.

    Idempotent: ``propose_pairing`` refuses to duplicate an open pairing.
    """
    from . import services

    created = 0
    for mentee in unpaired_mentees():
        for rec in suggest_mentors_for(mentee, limit=limit_per_mentee):
            if rec["score"] <= 0:
                continue
            pairing = services.propose_pairing(
                rec["mentor"], mentee, initiated_by=MentorshipPairing.InitiatedBy.SYSTEM,
                status=MentorshipPairing.Status.SUGGESTED, score=rec["score"], reasons=rec["reasons"],
            )
            if pairing is not None:
                created += 1
    return created
