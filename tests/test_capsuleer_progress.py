"""Capsuleer Path progress engine — ETA ranges, pace multipliers, and the snapshot write policy."""
from __future__ import annotations

import pytest

from apps.capsuleer import progress, services
from apps.capsuleer.models import GoalPace, GoalStatus, GoalType, ProgressSnapshot, Verification

from ._capsuleer_utils import _character, _member, _milestone, _ship_type, _skill_type, _snapshot

pytestmark = pytest.mark.django_db

LOGI, HULL = 3416, 11985


def _pilot(django_user_model, cid=8001):
    user = _member(django_user_model, str(cid))
    return user, _character(user, cid, "Progress Pilot")


def test_eta_is_an_honest_range_scaled_by_pace(django_user_model):
    from apps.capsuleer import plan as plan_mod

    user, char = _pilot(django_user_model)
    _skill_type(LOGI, "Logistics Cruisers", rank=6)
    _ship_type(HULL, "Osprey", required_skills=[(LOGI, 5)])
    _snapshot(char, {})
    goal = services.create_goal(user, title="Fly Osprey", goal_type=GoalType.SHIP,
                                character=char, ship_type_id=HULL, pace=GoalPace.BALANCED)
    plan_mod.build_plan(goal)
    goal.refresh_from_db()

    eta = progress.estimate_eta(goal)
    assert eta["state"] == "ok"
    assert eta["earliest_seconds"] > 0
    # balanced utilisation is 1.15 → likely trails earliest.
    assert eta["likely_seconds"] > eta["earliest_seconds"]
    assert eta["earliest"] < eta["likely"]
    assert "continuous skill queue" in eta["assumptions"]


def test_eta_unknown_without_plan(django_user_model):
    user, char = _pilot(django_user_model)
    goal = services.create_goal(user, title="No plan", goal_type=GoalType.CUSTOM, character=char)
    assert progress.estimate_eta(goal)["state"] == "unknown"


def test_snapshot_capped_to_one_per_utc_day(django_user_model):
    user, char = _pilot(django_user_model)
    goal = services.create_goal(user, title="Milestoned", goal_type=GoalType.CUSTOM, character=char)
    m = _milestone(goal, verification=Verification.SELF)
    goal = services.set_goal_status(goal, GoalStatus.ACTIVE, user)  # writes the day's snapshot (0%)
    services.complete_milestone(goal, m, user)                      # same day → capped, no 2nd row
    assert ProgressSnapshot.objects.filter(goal=goal).count() == 1


def test_snapshot_records_trigger_and_counts(django_user_model):
    user, char = _pilot(django_user_model)
    goal = services.create_goal(user, title="Counted", goal_type=GoalType.CUSTOM, character=char)
    _milestone(goal, verification=Verification.SELF)
    goal = services.set_goal_status(goal, GoalStatus.ACTIVE, user)
    snap = ProgressSnapshot.objects.filter(goal=goal).first()
    assert snap is not None
    assert snap.milestones_total == 1 and snap.milestones_done == 0
    assert "trigger" in snap.notes
