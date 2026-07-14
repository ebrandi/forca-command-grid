"""Capsuleer Path → unified quest-queue adapter (doc 08 §10, doc 10 §5.12).

``career_quests(user)`` returns **at most one** row — the pilot's primary goal's single next action —
in the ``apps.pilots.briefing._quest`` dict shape, merged into ``unified_quest_queue`` by Stage 4's
``career=`` param. It is *not* a rendering of ``PathSuggestion`` rows (suggestions never enter the
quest queue) and contains **no math**: it projects the pilot's own chosen plan — the first open,
un-snoozed action step of the highest-priority active goal, else that goal's first pending required
milestone. Points are a flat 5 (doc 08 §10): a personal step ranks below genuine corp orders rather
than competing with them. The queue's collision-yield rule (doc 08 §10) suppresses the career row via
``subject_doctrine_id`` / ``subject_ship_type_id`` when a surviving CI/readiness item shares the
subject.
"""
from __future__ import annotations

from django.utils.translation import gettext

from .models import (
    CareerGoal,
    GoalStatus,
    MilestoneStatus,
    Priority,
    StepStatus,
    Verification,
)

_PRIORITY_RANK = {Priority.PRIMARY: 0, Priority.SECONDARY: 1, Priority.SOMEDAY: 2}
_QUEST_POINTS = 5  # flat — career_quests carries no scoring math (doc 08 §10)


def career_quests(user) -> list[dict]:
    """At most one quest row for ``user`` — the primary active goal's next action, or ``[]``.

    Returns ``[]`` when the feature is off, the pilot has no active goal, or the primary goal has no
    open (un-snoozed) step and no owner-completable pending required milestone.
    """
    from core.features import feature_enabled

    if not feature_enabled("capsuleer") or not getattr(user, "is_authenticated", False):
        return []

    goals = list(
        CareerGoal.objects.filter(user=user, status=GoalStatus.ACTIVE)
        .prefetch_related("milestones", "action_steps")
    )
    return career_quests_from_goals(goals)


def career_quests_from_goals(goals) -> list[dict]:
    """The at-most-one quest row from a pre-loaded active-goal list.

    Shared by the home page and the dashboard bundle so the active goals (with their milestones and
    action steps) are fetched exactly once per request (findings 22/42). Assumes ``goals`` are the
    caller's active goals with ``milestones``/``action_steps`` prefetched.
    """
    from django.utils import timezone

    if not goals:
        return []
    goals = sorted(goals, key=lambda g: (_PRIORITY_RANK.get(g.priority, 3), -g.pk))
    goal = goals[0]
    now = timezone.now()

    step = next(
        (s for s in sorted(goal.action_steps.all(), key=lambda s: s.id)
         if s.status == StepStatus.OPEN and (s.snoozed_until is None or s.snoozed_until <= now)),
        None,
    )
    if step is not None:
        return [_row(goal, ref=f"s{step.pk}", title=step.title,
                     detail=step.note or gettext("Your next step on this goal."),
                     created_at=step.created_at)]

    # Fallback: the first pending required milestone the owner can actually action from the
    # dashboard. Auto milestones are credited by verification, never by a "Mark done" button, so
    # they are skipped rather than surfaced with an always-failing action (finding 29).
    milestone = next(
        (m for m in sorted(goal.milestones.all(), key=lambda m: m.order)
         if m.required and m.status == MilestoneStatus.PENDING
         and m.verification != Verification.AUTO),
        None,
    )
    if milestone is not None:
        return [_row(goal, ref=f"m{milestone.pk}", title=milestone.title,
                     detail=gettext("Your next milestone on this goal."),
                     created_at=milestone.created_at)]

    return []


def _row(goal, *, ref, title, detail, created_at) -> dict:
    from django.urls import reverse
    from django.utils import timezone

    return {
        "engine": "capsuleer",
        "id": ref,
        "category_key": "capsuleer",
        "category_label": gettext("Capsuleer Path"),
        "icon": "#i-route",
        "corp_order": False,               # personal goals are never corp orders
        "title": title,
        "detail": gettext("%(detail)s (goal: %(title)s)") % {
            "detail": detail, "title": goal.title
        },
        "points": _QUEST_POINTS,
        "action_url": reverse("capsuleer:goal_detail", args=[goal.pk]),
        "action_available": True,
        "form_url_name": "capsuleer:quest_action",
        "is_new": (timezone.now() - created_at).total_seconds() < 172800,  # 48h (doc 10 §5.12)
        "rank": 500 + _QUEST_POINTS,
        # Subjects for the queue's collision-yield rule (doc 08 §10).
        "subject_doctrine_id": goal.doctrine_id,
        "subject_ship_type_id": goal.ship_type_id,
        "goal_id": goal.pk,
    }
