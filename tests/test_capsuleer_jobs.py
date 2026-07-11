"""Capsuleer Path background jobs — the reconcile sweep, housekeeping, and the import hook.

Beat idempotency (run-twice credits once), the feature-flag early return, the cross-worker lock,
housekeeping retention + stalled flagging, and — the load-bearing safety property — that a raising
capsuleer reconcile can never break ``import_character_skills``.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
import responses
from django.core.cache import cache
from django.utils import timezone

from apps.capsuleer import services, tasks
from apps.capsuleer.models import (
    GoalStatus,
    GoalType,
    MilestoneKind,
    MilestoneStatus,
    ProgressSnapshot,
    Verification,
)
from apps.characters.services import import_character_skills
from apps.sso.models import AuthToken
from core import features

from ._capsuleer_utils import _character, _contribution, _goal, _member, _milestone

pytestmark = pytest.mark.django_db


def _active_goal_with_contribution(user, char, count=3):
    goal = services.create_goal(user, title="Fleets", goal_type=GoalType.CUSTOM, character=char)
    _milestone(goal, kind=MilestoneKind.CONTRIBUTION, verification=Verification.AUTO,
               params={"kinds": ["fleet"], "count": count})
    return services.set_goal_status(goal, GoalStatus.ACTIVE, user)  # stamps baseline_count=0


# --- reconcile sweep idempotency --------------------------------------------
def test_reconcile_sweep_credits_once(django_user_model):
    user = _member(django_user_model, "sweep")
    char = _character(user, 7101, "Sweep Pilot")
    goal = _active_goal_with_contribution(user, char, count=3)
    _contribution(user, "fleet", 3)  # three fleets after activation

    first = services.run_reconcile_sweep()
    assert first["credited"] == 1
    milestone = goal.milestones.get()
    milestone.refresh_from_db()
    assert milestone.status == MilestoneStatus.DONE
    assert milestone.evidence_snapshot["kind"] == "contribution"

    # A second immediate run finds no pending-met milestones → pure no-op.
    second = services.run_reconcile_sweep()
    assert second["credited"] == 0


def test_reconcile_from_snapshot_feature_disabled_is_inert(django_user_model):
    user = _member(django_user_model, "flag")
    char = _character(user, 7102, "Flag Pilot")
    features.set_disabled(["capsuleer"])
    try:
        assert services.reconcile_from_snapshot(char, None)["status"] == "feature_disabled"
        assert tasks.reconcile_progress()["status"] == "feature_disabled"
        assert tasks.housekeeping()["status"] == "feature_disabled"
    finally:
        features.set_disabled([])


def test_reconcile_task_lock_blocks_concurrent_run(django_user_model):
    cache.add(tasks._RECONCILE_LOCK, "someone-elses-token", 300)
    try:
        assert tasks.reconcile_progress()["status"] == "already_running"
    finally:
        cache.delete(tasks._RECONCILE_LOCK)


# --- housekeeping ------------------------------------------------------------
def test_housekeeping_prunes_old_snapshots_keeps_newest(django_user_model):
    user = _member(django_user_model, "hk")
    char = _character(user, 7103, "HK Pilot")
    goal = _goal(user, character=char)
    old = ProgressSnapshot.objects.create(goal=goal, percent=10, milestones_done=0,
                                          milestones_total=1)
    newest = ProgressSnapshot.objects.create(goal=goal, percent=50, milestones_done=1,
                                             milestones_total=2)
    # Age the older snapshot past the 400-day window.
    ProgressSnapshot.objects.filter(pk=old.pk).update(
        taken_at=timezone.now() - timedelta(days=500)
    )
    result = services.run_housekeeping()
    assert result["snapshots_pruned"] == 1
    remaining = set(ProgressSnapshot.objects.filter(goal=goal).values_list("pk", flat=True))
    assert remaining == {newest.pk}  # newest per goal always kept


def test_housekeeping_flags_stalled_goal(django_user_model):
    user = _member(django_user_model, "stall")
    char = _character(user, 7104, "Stall Pilot")
    goal = services.create_goal(user, title="Stalled", goal_type=GoalType.CUSTOM, character=char)
    goal = services.set_goal_status(goal, GoalStatus.ACTIVE, user)
    # Backdate every movement signal well past the 45-day stall threshold.
    old = timezone.now() - timedelta(days=60)
    type(goal).objects.filter(pk=goal.pk).update(created_at=old)
    goal.activity_log.update(created_at=old)
    goal.snapshots.update(taken_at=old)

    result = services.run_housekeeping()
    assert result["reviews_flagged"] == 1
    goal.refresh_from_db()
    assert goal.review_due_at is not None
    assert goal.activity_log.filter(verb="review.due_set", actor__isnull=True).exists()
    # Re-running does not re-flag (review_due_at already set).
    assert services.run_housekeeping()["reviews_flagged"] == 0


# --- the import hook ---------------------------------------------------------
def _token(character):
    token = AuthToken(
        character=character, scopes=["esi-skills.read_skills.v1"],
        access_expires_at=timezone.now() + timedelta(hours=1),
    )
    token.refresh_token = "r"
    token.access_token = "valid-access"
    token.save()
    return token


@responses.activate
def test_import_hook_credits_skill_milestone(character):
    user = character.user
    goal = services.create_goal(user, title="Train it", goal_type=GoalType.CUSTOM,
                                character=character)
    goal = services.set_goal_status(goal, GoalStatus.ACTIVE, user)
    milestone = _milestone(goal, kind=MilestoneKind.SKILL_TARGET, verification=Verification.AUTO,
                           params={"skills": [{"type_id": 3331, "level": 4}]})
    _token(character)
    responses.add(
        responses.GET,
        f"https://esi.evetech.net/characters/{character.character_id}/skills/",
        json={"total_sp": 1_000_000,
              "skills": [{"skill_id": 3331, "trained_skill_level": 4, "skillpoints_in_skill": 1}]},
        status=200,
    )
    import_character_skills(character)
    milestone.refresh_from_db()
    assert milestone.status == MilestoneStatus.DONE
    assert milestone.evidence_snapshot["kind"] == "skill_target"


# --- generate_suggestions beat ----------------------------------------------
def test_generate_suggestions_flag_and_config_gates(django_user_model):
    features.set_disabled(["capsuleer"])
    try:
        assert tasks.generate_suggestions()["status"] == "feature_disabled"
    finally:
        features.set_disabled([])


def test_generate_suggestions_lock_blocks_concurrent(django_user_model):
    cache.add(tasks._SUGGEST_LOCK, "someone-elses-token", 300)
    try:
        assert tasks.generate_suggestions()["status"] == "already_running"
    finally:
        cache.delete(tasks._SUGGEST_LOCK)


def test_generate_suggestions_idempotent_run_twice(django_user_model):
    from datetime import timedelta

    from apps.capsuleer.models import PathSuggestion

    user = _member(django_user_model, "gensweep")
    char = _character(user, 7108, "Gen Pilot")
    goal = services.create_goal(user, title="x", goal_type=GoalType.CUSTOM, character=char)
    goal = services.set_goal_status(goal, GoalStatus.ACTIVE, user)
    old = timezone.now() - timedelta(days=60)
    type(goal).objects.filter(pk=goal.pk).update(created_at=old)
    goal.activity_log.update(created_at=old)
    goal.snapshots.update(taken_at=old)

    first = tasks.generate_suggestions()
    assert first["admitted"] >= 1
    count_after_first = PathSuggestion.objects.filter(user=user).count()
    second = tasks.generate_suggestions()
    assert second["admitted"] == 0  # refresh only, nothing new
    assert PathSuggestion.objects.filter(user=user).count() == count_after_first


@responses.activate
def test_import_hook_failure_never_breaks_skill_import(character, monkeypatch):
    """A raising capsuleer reconcile must not fail the skill import (the snapshot is the source of
    truth); the isolated try/except contains it."""
    def _boom(char, snapshot):
        raise RuntimeError("capsuleer exploded")

    monkeypatch.setattr("apps.capsuleer.services.reconcile_from_snapshot", _boom)
    _token(character)
    responses.add(
        responses.GET,
        f"https://esi.evetech.net/characters/{character.character_id}/skills/",
        json={"total_sp": 500_000,
              "skills": [{"skill_id": 3331, "trained_skill_level": 3, "skillpoints_in_skill": 1}]},
        status=200,
    )
    snapshot = import_character_skills(character)  # must not raise
    assert snapshot is not None
    assert snapshot.trained_level(3331) == 3
