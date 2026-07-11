"""Capsuleer Path suggestion engine (doc 08) — generators, gating, caps, upsert, actions.

Per-kind trigger/no-trigger boundaries, the alignment gating matrix (personal_only + avoided +
muted), dedupe upsert preserving acted state, condition-cleared expiry, the three storm caps, and
the action service (accept/dismiss/defer window/not-interested feedback/incorrect). ``derive_blocked``
is exercised through the ``blocked_prereq`` generator.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.capsuleer import suggest
from apps.capsuleer.models import (
    CareerActionStep,
    CareerGoal,
    GoalStatus,
    GoalType,
    MilestoneKind,
    PathSuggestion,
    SuggestionKind,
    SuggestionStatus,
    Verification,
)

from ._capsuleer_utils import _character, _goal, _member, _milestone, _profile

pytestmark = pytest.mark.django_db


def _pilot(django_user_model, cid=9001):
    user = _member(django_user_model, str(cid))
    return user, _character(user, cid, "Suggest Pilot")


def _stall(goal, days=60):
    """Backdate every movement signal so the goal reads stalled."""
    old = timezone.now() - timedelta(days=days)
    CareerGoal.objects.filter(pk=goal.pk).update(created_at=old)
    goal.activity_log.update(created_at=old)
    goal.snapshots.update(taken_at=old)


def _ctx(user):
    return suggest._build_context(user, timezone.now())


# --- stalled_goal ------------------------------------------------------------
def test_stalled_goal_triggers_and_no_trigger(django_user_model):
    user, char = _pilot(django_user_model)
    from apps.capsuleer import services

    goal = services.create_goal(user, title="Fly logi", goal_type=GoalType.CUSTOM, character=char)
    goal = services.set_goal_status(goal, GoalStatus.ACTIVE, user)
    # Fresh goal → no stall.
    assert suggest.gen_stalled_goal(_ctx(user)) == []
    _stall(goal)
    drafts = suggest.gen_stalled_goal(_ctx(user))
    assert len(drafts) == 1 and drafts[0].kind == SuggestionKind.STALLED_GOAL
    assert drafts[0].corp_driven is False


def test_run_generation_creates_stalled_row(django_user_model):
    user, char = _pilot(django_user_model)
    from apps.capsuleer import services

    goal = services.create_goal(user, title="x", goal_type=GoalType.CUSTOM, character=char)
    goal = services.set_goal_status(goal, GoalStatus.ACTIVE, user)
    _stall(goal)
    result = suggest.run_generation()
    assert result["admitted"] >= 1
    row = PathSuggestion.objects.get(user=user, kind=SuggestionKind.STALLED_GOAL)
    assert row.status == SuggestionStatus.OPEN and row.reason


# --- review_due --------------------------------------------------------------
def test_review_due_goal_variant(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char, status=GoalStatus.ACTIVE,
                 review_due_at=timezone.now() - timedelta(days=1))
    drafts = suggest.gen_review_due(_ctx(user))
    assert any(d.goal_id == goal.pk for d in drafts)


# --- blocked_prereq (exercises derive_blocked) ------------------------------
def test_blocked_prereq_on_missing_doctrine(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char, status=GoalStatus.ACTIVE)
    _milestone(goal, kind=MilestoneKind.DOCTRINE_READY, verification=Verification.AUTO,
               required=True, params={"doctrine_id": 999999, "tier": "viable"})
    from apps.capsuleer import progress

    blocked, reasons = progress.derive_blocked(goal)
    assert blocked and "no matching doctrine available" in reasons
    drafts = suggest.gen_blocked_prereq(_ctx(user))
    assert len(drafts) == 1 and drafts[0].kind == SuggestionKind.BLOCKED_PREREQ


def test_not_blocked_without_structural_milestone(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char, status=GoalStatus.ACTIVE)
    _milestone(goal, verification=Verification.SELF, required=True)  # manual — no structural check
    from apps.capsuleer import progress

    assert progress.derive_blocked(goal) == (False, [])


# --- alignment gating --------------------------------------------------------
def test_personal_only_suppresses_corp_driven(django_user_model):
    user, char = _pilot(django_user_model)
    _profile(user, corp_alignment="personal_only")
    ctx = _ctx(user)
    # A synthetic corp_driven draft is dropped by the gate.
    corp = suggest.Draft(kind=SuggestionKind.CAMPAIGN_OPPORTUNITY, dedupe_key="u1:x:campaign:1",
                         goal_id=None, title="t", reason="r", data={}, corp_driven=True,
                         expires_at=None)
    personal = suggest.Draft(kind=SuggestionKind.STALLED_GOAL, dedupe_key="u1:y:goal:1",
                             goal_id=1, title="t", reason="r", data={}, corp_driven=False,
                             expires_at=None)
    kept = suggest._apply_alignment(ctx, [corp, personal])
    assert corp not in kept and personal in kept


def test_avoided_activity_blocks_event_match(django_user_model):
    user, char = _pilot(django_user_model)
    _profile(user, avoided_activities=["combat_line"])
    _goal(user, character=char, status=GoalStatus.ACTIVE, activity="combat_line")
    # Even with a matching op, an avoided activity yields nothing.
    from apps.operations.models import Operation

    Operation.objects.create(name="Roam", type=Operation.Type.PVP,
                             target_at=timezone.now() + timedelta(days=1),
                             status=Operation.Status.PLANNED)
    assert suggest.gen_event_match(_ctx(user)) == []


def test_muted_kind_not_generated(django_user_model):
    user, char = _pilot(django_user_model)
    _profile(user, suggestion_muted_kinds=[SuggestionKind.STALLED_GOAL])
    from apps.capsuleer import services

    goal = services.create_goal(user, title="x", goal_type=GoalType.CUSTOM, character=char)
    goal = services.set_goal_status(goal, GoalStatus.ACTIVE, user)
    _stall(goal)
    suggest.run_generation()
    assert not PathSuggestion.objects.filter(user=user, kind=SuggestionKind.STALLED_GOAL).exists()


# --- upsert preserving pilot state ------------------------------------------
def test_dismissed_row_not_reopened(django_user_model):
    user, char = _pilot(django_user_model)
    from apps.capsuleer import services

    goal = services.create_goal(user, title="x", goal_type=GoalType.CUSTOM, character=char)
    goal = services.set_goal_status(goal, GoalStatus.ACTIVE, user)
    _stall(goal)
    suggest.run_generation()
    row = PathSuggestion.objects.get(user=user, kind=SuggestionKind.STALLED_GOAL)
    suggest.act_on_suggestion(user, row, "dismiss")
    # A second run in the same month must not resurrect the dismissed row.
    suggest.run_generation()
    row.refresh_from_db()
    assert row.status == SuggestionStatus.DISMISSED
    assert PathSuggestion.objects.filter(user=user, dedupe_key=row.dedupe_key).count() == 1


# --- storm caps --------------------------------------------------------------
def test_per_run_creation_cap(django_user_model):
    user, char = _pilot(django_user_model)
    # Five stalled goals; the per-run cap admits at most 3 new rows.
    from apps.capsuleer import services

    for i in range(5):
        g = services.create_goal(user, title=f"g{i}", goal_type=GoalType.CUSTOM, character=char)
        g = services.set_goal_status(g, GoalStatus.ACTIVE, user)
        _stall(g)
    result = suggest.run_generation()
    assert result["admitted"] == suggest._PER_RUN_CREATE_CAP
    assert PathSuggestion.objects.filter(user=user, status=SuggestionStatus.OPEN).count() == 3


# --- condition-cleared expiry -----------------------------------------------
def test_blocked_row_expires_when_cleared(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char, status=GoalStatus.ACTIVE)
    ms = _milestone(goal, kind=MilestoneKind.DOCTRINE_READY, verification=Verification.AUTO,
                    required=True, params={"doctrine_id": 999999, "tier": "viable"})
    suggest.run_generation()
    row = PathSuggestion.objects.get(user=user, kind=SuggestionKind.BLOCKED_PREREQ)
    assert row.expires_at is None
    # Skip the blocking milestone → condition clears → the open row is expired next run.
    from apps.capsuleer import services

    services.skip_milestone(goal, ms, user)
    suggest.run_generation()
    row.refresh_from_db()
    assert row.expires_at is not None


# --- action service ----------------------------------------------------------
def test_accept_creates_step_and_redirect(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char, status=GoalStatus.ACTIVE)
    row = PathSuggestion.objects.create(
        user=user, goal=goal, kind=SuggestionKind.STALLED_GOAL, title="Review it",
        reason="r", dedupe_key="u1:stalled_goal:goal:1:2026-07",
    )
    out = suggest.act_on_suggestion(user, row, "accept")
    row.refresh_from_db()
    assert row.status == SuggestionStatus.ACCEPTED
    assert out["redirect"]["url_name"] == "capsuleer:goal_review"
    assert CareerActionStep.objects.filter(goal=goal, source="suggestion").exists()


def test_not_interested_mutes_kind_and_expires(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char, status=GoalStatus.ACTIVE)
    row = PathSuggestion.objects.create(
        user=user, goal=goal, kind=SuggestionKind.STALLED_GOAL, title="t", reason="r",
        dedupe_key="u1:stalled_goal:goal:2:2026-07",
    )
    suggest.act_on_suggestion(user, row, "not_interested")
    row.refresh_from_db()
    assert row.status == SuggestionStatus.NOT_INTERESTED
    profile = user.career_profile
    assert SuggestionKind.STALLED_GOAL in profile.suggestion_muted_kinds


def test_incorrect_does_not_mute(django_user_model):
    user, char = _pilot(django_user_model)
    row = PathSuggestion.objects.create(
        user=user, kind=SuggestionKind.NEAR_QUALIFICATION, title="t", reason="r",
        dedupe_key="u1:near_qualification:doctrine:1",
    )
    suggest.act_on_suggestion(user, row, "incorrect")
    row.refresh_from_db()
    assert row.status == SuggestionStatus.INCORRECT
    # incorrect never mutes — a profile may not even exist.
    from apps.capsuleer.models import CareerProfile

    profile = CareerProfile.objects.filter(user=user).first()
    assert profile is None or SuggestionKind.NEAR_QUALIFICATION not in profile.suggestion_muted_kinds


def test_defer_hides_for_window_then_returns(django_user_model):
    user, char = _pilot(django_user_model)
    row = PathSuggestion.objects.create(
        user=user, kind=SuggestionKind.STALLED_GOAL, title="t", reason="r",
        dedupe_key="u1:stalled_goal:goal:3:2026-07",
    )
    suggest.act_on_suggestion(user, row, "defer")
    now = timezone.now()
    # Within the 14-day window → hidden from the inbox.
    assert row.pk not in {s.pk for s in suggest.inbox_suggestions(user, now)}
    # After the window → shown again unchanged (still deferred, no status mutation).
    later = now + timedelta(days=15)
    assert row.pk in {s.pk for s in suggest.inbox_suggestions(user, later)}
    row.refresh_from_db()
    assert row.status == SuggestionStatus.DEFERRED


def test_act_on_other_users_suggestion_rejected(django_user_model):
    from django.core.exceptions import ValidationError

    owner, _ = _pilot(django_user_model, 9101)
    other, _ = _pilot(django_user_model, 9102)
    row = PathSuggestion.objects.create(
        user=owner, kind=SuggestionKind.STALLED_GOAL, title="t", reason="r",
        dedupe_key="u1:stalled_goal:goal:9:2026-07",
    )
    with pytest.raises(ValidationError):
        suggest.act_on_suggestion(other, row, "dismiss")


# --- goalless widening (doc 08 §6, Stage 4 engine addendum) ------------------
def test_goalless_event_match_under_corp_forward(django_user_model):
    from datetime import timedelta

    from apps.operations.models import Operation

    user, char = _pilot(django_user_model)
    _profile(user, corp_alignment="corp_forward", preferred_activities=["mining"])
    Operation.objects.create(name="Moon dig", type=Operation.Type.MINING,
                             target_at=timezone.now() + timedelta(days=1),
                             status=Operation.Status.PLANNED)
    drafts = suggest.gen_event_match(_ctx(user))
    goalless = [d for d in drafts if d.goal_id is None and d.kind == SuggestionKind.EVENT_MATCH]
    assert len(goalless) == 1


def test_no_goalless_under_balanced(django_user_model):
    from datetime import timedelta

    from apps.operations.models import Operation

    user, char = _pilot(django_user_model)
    _profile(user, corp_alignment="balanced", preferred_activities=["mining"])
    Operation.objects.create(name="Moon dig", type=Operation.Type.MINING,
                             target_at=timezone.now() + timedelta(days=1),
                             status=Operation.Status.PLANNED)
    # Balanced does not fire goalless variants — nothing without a goal.
    assert [d for d in suggest.gen_event_match(_ctx(user)) if d.goal_id is None] == []
