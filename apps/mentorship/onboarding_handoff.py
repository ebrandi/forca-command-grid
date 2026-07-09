"""MEN-2 — onboarding → mentorship auto-handoff.

Closes the gap where a new pilot finishes (or stalls on) the onboarding checklist and is
then silently left unpaired. This surfaces those cadets on the leader's matching worklist
so none slip through — without auto-enrolling anyone as a mentee behind their back
(a leader invites/pairs them, and the existing auto-suggest offers a mentor once they are
an active mentee).
"""
from __future__ import annotations

import datetime as dt

from django.db.models import Count, Max, Q
from django.utils import timezone

from apps.onboarding.models import OnboardingMilestone, OnboardingProgress

from .models import MenteeProfile, MentorProfile

# A cadet who started onboarding but hasn't completed a milestone in this long counts as
# "stalled" and worth a nudge into mentorship.
_STALL_DAYS = 14


def _onboarding_state() -> dict[int, str]:
    """``character_id -> 'complete' | 'stalled'`` for cadets worth a handoff.

    ``complete`` = every active milestone done; ``stalled`` = at least one milestone done
    but not all, and no milestone completed in the last ``_STALL_DAYS``. Cadets still
    actively progressing (or who have done nothing yet) are omitted.
    """
    active_count = OnboardingMilestone.objects.filter(active=True).count()
    if active_count == 0:
        return {}
    stall_cutoff = timezone.now() - dt.timedelta(days=_STALL_DAYS)
    # Scope the counts to *active* milestones — a DONE row for a since-deactivated
    # milestone must not count toward completion (leaders can deactivate milestones).
    _done = Q(status=OnboardingProgress.Status.DONE, milestone__active=True)
    rows = (
        OnboardingProgress.objects.values("character_id").annotate(
            done=Count("id", filter=_done),
            last_done=Max("completed_at", filter=_done),
        )
    )
    out: dict[int, str] = {}
    for r in rows:
        if r["done"] >= active_count:
            out[r["character_id"]] = "complete"
        elif r["done"] > 0 and r["last_done"] and r["last_done"] <= stall_cutoff:
            out[r["character_id"]] = "stalled"
    return out


def handoff_candidates(limit: int = 50) -> list[dict]:
    """Cadets who completed/stalled onboarding and aren't yet in the mentorship system.

    One row per pilot (account), ``{user, character, state}``, so a leader can invite or
    pair them. Excludes anyone who already has a mentee or mentor profile.
    """
    states = _onboarding_state()
    if not states:
        return []

    from apps.sso.models import EveCharacter

    chars = list(
        EveCharacter.objects.filter(
            character_id__in=list(states), is_corp_member=True, user__isnull=False
        ).select_related("user")
    )
    if not chars:
        return []

    user_ids = {c.user_id for c in chars}
    taken = set(
        MenteeProfile.objects.filter(user_id__in=user_ids).values_list("user_id", flat=True)
    )
    taken |= set(
        MentorProfile.objects.filter(user_id__in=user_ids).values_list("user_id", flat=True)
    )

    # One entry per account, preferring a 'complete' cadet over a 'stalled' one.
    best: dict[int, dict] = {}
    for ch in chars:
        if ch.user_id in taken:
            continue
        state = states.get(ch.character_id)
        current = best.get(ch.user_id)
        if current is None or (state == "complete" and current["state"] == "stalled"):
            best[ch.user_id] = {"user": ch.user, "character": ch, "state": state}
    rows = sorted(best.values(), key=lambda r: (r["state"] != "complete", r["character"].name))
    return rows[:limit]
