"""Mentor ↔ mentee matching recommendations.

A transparent, explainable score (0–100) over interest overlap, time-zone,
language, mentor capacity and history — never a black box. Leadership can accept a
suggestion, pair manually, or let either pilot initiate; the score is advisory.
"""
from __future__ import annotations

from . import messages as msg
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


def score(mentor: MentorProfile, mentee: MenteeProfile, *, services=None) -> tuple[float, list[dict]]:
    """Return (score 0–100, reason entries). Assumes both are ACTIVE.

    Seam B: the reasons are *persisted* on ``MentorshipPairing.match_reasons`` by
    ``mentorship.auto_suggest_pairings`` — a worker with no locale — and read back by the mentor,
    the cadet and officers under theirs. So a reason is returned as a scaffold
    ``{"key", "params"}`` entry rather than a finished sentence; ``messages.english_list`` renders
    the English audit prose at the write site and ``messages.render_list`` the reader's locale at
    the read site. The interpolated focus areas, languages and time zone are pilot-entered values:
    substituted raw, never translated.
    """
    from . import services as svc

    services = services or svc
    reasons: list[dict] = []
    total = 0.0

    mentor_areas = _as_set(mentor.areas)
    mentee_wants = _as_set(mentee.goals) | _as_set(mentee.interests)
    overlap = mentor_areas & mentee_wants
    if overlap:
        j = _jaccard(mentor_areas, mentee_wants)
        total += _W_INTEREST * j
        reasons.append({"key": "match.shared_focus",
                        "params": {"areas": ", ".join(sorted(overlap))}})
    else:
        reasons.append({"key": "match.no_overlap", "params": {}})

    if mentor.timezone and mentee.timezone:
        if mentor.timezone.strip().lower() == mentee.timezone.strip().lower():
            total += _W_TZ
            reasons.append({"key": "match.same_timezone",
                            "params": {"timezone": mentor.timezone}})

    mentor_langs = _as_set(mentor.languages)
    mentee_langs = _as_set(mentee.languages)
    if mentor_langs & mentee_langs:
        total += _W_LANG
        reasons.append({"key": "match.shared_language",
                        "params": {"languages": ", ".join(sorted(mentor_langs & mentee_langs))}})

    capacity = services.mentor_capacity(mentor)
    active = services.mentor_active_count(mentor)
    if active < capacity:
        # More free slots → slightly higher score (spread the load).
        total += _W_CAPACITY * (capacity - active) / capacity
        reasons.append({"key": "match.has_capacity",
                        "params": {"active": active, "capacity": capacity}})

    if mentor.open_to_adhoc:
        total += _W_ADHOC
        reasons.append({"key": "match.open_to_adhoc", "params": {}})

    if mentee.voice_comfortable and mentor.comms:
        total += _W_VOICE

    # Penalise a pair that was previously cancelled.
    prior_cancel = MentorshipPairing.objects.filter(
        mentor=mentor, mentee=mentee,
        status__in=[MentorshipPairing.Status.CANCELLED, MentorshipPairing.Status.EXPIRED],
    ).exists()
    if prior_cancel:
        total -= _PENALTY_PRIOR_CANCEL
        reasons.append({"key": "match.prior_cancel", "params": {}})

    return max(0.0, min(100.0, round(total, 1))), reasons


def combat_read(profile) -> dict | None:
    """KB-27 (WS-D4): the inferred combat profile for a mentor/mentee, from our killboard.

    A HINT for the human matcher — it never touches :func:`score` or the algorithm. Resolves the
    profile's main linked character (or any linked character) and returns the shared classifier's
    inference (playstyle / FC-likelihood / role usage), or ``None`` when the pilot has no linked
    character or no history on our board (so the hint panel simply stays quiet). Cached per
    character, so surfacing it for a worklist of profiles reuses the same memoised builds.
    """
    from apps.killboard.intel_inference import character_intel

    user = getattr(profile, "user", None)
    if user is None:
        return None
    char = user.characters.filter(is_main=True).first() or user.characters.first()
    if char is None:
        return None
    intel = character_intel(char.character_id)
    return intel if intel.get("has_history") else None


def suggest_mentors_for(mentee: MenteeProfile, limit: int = 5) -> list[dict]:
    from . import services

    out = []
    for mentor in MentorProfile.objects.filter(status=MentorProfile.Status.ACTIVE).select_related("user"):
        if services.existing_open_pairing(mentor, mentee):
            continue
        if not services.mentor_has_capacity(mentor):
            continue
        s, reasons = score(mentor, mentee, services=services)
        # ``reasons`` here is render-time only (a suggestion on the dashboard is never stored), so
        # it resolves in *this* reader's locale; ``reason_keys`` is what a proposal would persist.
        out.append({"mentor": mentor, "score": s,
                    "reasons": msg.render_list(reasons, None), "reason_keys": reasons})
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
        out.append({"mentee": mentee, "score": s,
                    "reasons": msg.render_list(reasons, None), "reason_keys": reasons})
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
                status=MentorshipPairing.Status.SUGGESTED, score=rec["score"],
                reasons=rec["reason_keys"],
            )
            if pairing is not None:
                created += 1
    return created
