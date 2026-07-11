"""Capsuleer Path service tests: the lifecycle transition table, goal cap, milestone semantics,
the endorsement model, the progress formula, and the GoalActivity/audit trail (doc 05, doc 11).

These exercise the *stateful* rules through the public service functions (never poking ``status``
or ``progress_percent`` directly). Object-level authorisation (who may see which goal) lives in
``test_capsuleer_security.py``.
"""
from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError

from apps.admin_audit.models import AuditLog
from apps.capsuleer import services
from apps.capsuleer.models import (
    CareerGoal,
    GoalStatus,
    MilestoneKind,
    MilestoneStatus,
    Verification,
    Visibility,
)

from ._capsuleer_utils import _character, _goal, _member, _milestone, _officer, _pair

pytestmark = pytest.mark.django_db

S = GoalStatus


def _active_goal(user):
    """A goal already moved to active through the service (so started_at is set)."""
    goal = services.create_goal(user, title="Fly logi", goal_type="custom")
    return services.set_goal_status(goal, S.ACTIVE, user)


# --- goal creation + cap ------------------------------------------------------
def test_create_goal_records_activity_and_audit(django_user_model):
    user = _member(django_user_model)
    goal = services.create_goal(user, title="Fly logi", goal_type="custom")
    assert goal.status == S.CONSIDERING
    assert goal.activity_log.filter(verb="goal.created").exists()
    assert AuditLog.objects.filter(
        action="capsuleer.goal.created", target_id=str(goal.pk)
    ).exists()


def test_create_goal_active_sets_started_at(django_user_model):
    user = _member(django_user_model)
    goal = services.create_goal(user, title="x", goal_type="custom", status=S.ACTIVE)
    assert goal.status == S.ACTIVE
    assert goal.started_at is not None


def test_create_goal_rejects_foreign_character(django_user_model):
    owner = _member(django_user_model, "owner")
    other = _member(django_user_model, "other")
    foreign = _character(other, cid=9001, name="Alt")
    with pytest.raises(ValidationError):
        services.create_goal(owner, title="x", goal_type="custom", character=foreign)


def test_create_goal_rejects_negative_budget(django_user_model):
    user = _member(django_user_model)
    with pytest.raises(ValidationError):
        services.create_goal(user, title="x", goal_type="custom", budget_isk=-1)


def test_goal_cap_enforced(django_user_model):
    user = _member(django_user_model)
    for i in range(services.MAX_ACTIVE_GOALS):
        CareerGoal.objects.create(user=user, title=f"g{i}", goal_type="custom")
    with pytest.raises(ValidationError):
        services.create_goal(user, title="one too many", goal_type="custom")
    # An archived goal does not count toward the cap — archiving one frees a slot.
    one = CareerGoal.objects.filter(user=user).first()
    CareerGoal.objects.filter(pk=one.pk).update(status=S.ARCHIVED)
    services.create_goal(user, title="now ok", goal_type="custom")


# --- lifecycle transition table (doc 05 §3.1) --------------------------------
_LEGAL = [
    (S.CONSIDERING, S.ACTIVE), (S.CONSIDERING, S.ABANDONED), (S.CONSIDERING, S.ARCHIVED),
    (S.ACTIVE, S.PAUSED), (S.ACTIVE, S.ABANDONED),
    (S.PAUSED, S.ACTIVE), (S.PAUSED, S.ABANDONED), (S.PAUSED, S.ARCHIVED),
    (S.COMPLETED, S.ACTIVE), (S.COMPLETED, S.ARCHIVED),
    (S.ABANDONED, S.CONSIDERING), (S.ABANDONED, S.ARCHIVED),
]

_ILLEGAL = [
    (S.CONSIDERING, S.PAUSED), (S.CONSIDERING, S.COMPLETED),
    (S.ACTIVE, S.CONSIDERING), (S.ACTIVE, S.ARCHIVED),
    (S.PAUSED, S.COMPLETED), (S.COMPLETED, S.PAUSED),
    (S.ABANDONED, S.ACTIVE), (S.ARCHIVED, S.ACTIVE), (S.ARCHIVED, S.CONSIDERING),
]


def _force_status(goal, status):
    """Place a goal in ``status`` for a transition-edge test without walking the chain."""
    CareerGoal.objects.filter(pk=goal.pk).update(status=status)
    goal.refresh_from_db()
    return goal


@pytest.mark.parametrize("frm,to", _LEGAL)
def test_legal_transitions_succeed(django_user_model, frm, to):
    user = _member(django_user_model)
    goal = _force_status(_goal(user), frm)
    result = services.set_goal_status(goal, to, user)
    assert result.status == to
    assert goal.activity_log.exists()


@pytest.mark.parametrize("frm,to", _ILLEGAL)
def test_illegal_transitions_raise(django_user_model, frm, to):
    user = _member(django_user_model)
    goal = _force_status(_goal(user), frm)
    with pytest.raises(ValidationError):
        services.set_goal_status(goal, to, user)
    goal.refresh_from_db()
    assert goal.status == frm


def test_transition_by_non_owner_is_rejected(django_user_model):
    owner = _member(django_user_model, "owner")
    other = _member(django_user_model, "other")
    goal = _goal(owner)
    with pytest.raises(ValidationError):
        services.set_goal_status(goal, S.ACTIVE, other)
    goal.refresh_from_db()
    assert goal.status == S.CONSIDERING


def test_same_status_is_idempotent_noop(django_user_model):
    user = _member(django_user_model)
    goal = _goal(user)
    services.set_goal_status(goal, S.CONSIDERING, user)
    assert not goal.activity_log.exists()  # no transition recorded


def test_activation_sets_started_at_and_audits(django_user_model):
    user = _member(django_user_model)
    goal = services.set_goal_status(_goal(user), S.ACTIVE, user)
    assert goal.started_at is not None
    assert goal.activity_log.filter(verb="goal.activated").exists()
    assert AuditLog.objects.filter(
        action="capsuleer.goal.status_changed", target_id=str(goal.pk)
    ).exists()


def test_pause_records_reason_on_goal_not_in_audit(django_user_model):
    user = _member(django_user_model)
    goal = _active_goal(user)
    services.set_goal_status(goal, S.PAUSED, user, reason="burned out")
    goal.refresh_from_db()
    assert goal.paused_reason == "burned out"
    # The free-text reason never enters the activity detail or the audit metadata (doc 09 §7.1).
    row = goal.activity_log.filter(verb="goal.paused").first()
    assert "burned out" not in str(row.detail)
    audit = AuditLog.objects.filter(action="capsuleer.goal.status_changed").last()
    assert "burned out" not in str(audit.metadata)


def test_resume_clears_paused_reason(django_user_model):
    user = _member(django_user_model)
    goal = _active_goal(user)
    services.set_goal_status(goal, S.PAUSED, user, reason="afk")
    goal = services.set_goal_status(goal, S.ACTIVE, user)
    assert goal.paused_reason == ""


def test_reopen_completed_clears_completed_at_and_keeps_history(django_user_model):
    user = _member(django_user_model)
    goal = _active_goal(user)  # no milestones → completion needs no override
    goal = services.set_goal_status(goal, S.COMPLETED, user)
    assert goal.completed_at is not None
    goal = services.set_goal_status(goal, S.ACTIVE, user)
    assert goal.completed_at is None
    reopened = goal.activity_log.filter(verb="goal.reopened").first()
    assert "prior_completed_at" in reopened.detail


# --- completion gate + override ----------------------------------------------
def test_completion_blocked_by_pending_required_milestone(django_user_model):
    user = _member(django_user_model)
    goal = _active_goal(user)
    _milestone(goal, required=True)  # pending required
    with pytest.raises(ValidationError):
        services.set_goal_status(goal, S.COMPLETED, user)
    goal.refresh_from_db()
    assert goal.status == S.ACTIVE


def test_completion_override_with_reason_is_flagged(django_user_model):
    user = _member(django_user_model)
    goal = _active_goal(user)
    _milestone(goal, required=True)
    goal = services.set_goal_status(goal, S.COMPLETED, user, reason="close enough")
    assert goal.status == S.COMPLETED
    assert goal.progress_percent == 100  # terminal display value
    row = goal.activity_log.filter(verb="goal.completed").first()
    assert row.detail.get("override") is True


def test_completion_allowed_when_required_done(django_user_model):
    user = _member(django_user_model)
    goal = _active_goal(user)
    m = _milestone(goal, required=True, verification=Verification.SELF)
    services.complete_milestone(goal, m, user)
    goal = services.set_goal_status(goal, S.COMPLETED, user)  # no override needed
    assert goal.status == S.COMPLETED


# --- milestone semantics ------------------------------------------------------
def test_auto_milestone_rejects_manual_completion(django_user_model):
    user = _member(django_user_model)
    goal = _active_goal(user)
    m = _milestone(goal, kind=MilestoneKind.SKILL_TARGET, verification=Verification.AUTO,
                   params={"skills": [{"type_id": 1, "level": 1}]})
    with pytest.raises(ValidationError):
        services.complete_milestone(goal, m, user)


def test_self_milestone_completes_and_recomputes_progress(django_user_model):
    user = _member(django_user_model)
    goal = _active_goal(user)
    a = _milestone(goal, required=True, verification=Verification.SELF)
    _milestone(goal, required=True, verification=Verification.SELF)
    services.complete_milestone(goal, a, user)
    goal.refresh_from_db()
    assert goal.progress_percent == 50  # 1 of 2 required done


def test_skipped_required_milestone_leaves_the_gate(django_user_model):
    user = _member(django_user_model)
    goal = _active_goal(user)
    a = _milestone(goal, required=True, verification=Verification.SELF)
    b = _milestone(goal, required=True, verification=Verification.SELF)
    services.complete_milestone(goal, a, user)
    services.skip_milestone(goal, b, user)
    # Only a counted, and it is done → 100; completion needs no override.
    goal.refresh_from_db()
    assert goal.progress_percent == 100
    goal = services.set_goal_status(goal, S.COMPLETED, user)
    assert goal.status == S.COMPLETED


def test_add_milestone_validates_params(django_user_model):
    user = _member(django_user_model)
    goal = _active_goal(user)
    with pytest.raises(ValidationError):
        services.add_milestone(goal, user, kind=MilestoneKind.SKILL_TARGET,
                               title="bad", verification=Verification.AUTO, params={"skills": []})
    m = services.add_milestone(goal, user, kind=MilestoneKind.MANUAL, title="ok",
                               verification=Verification.SELF)
    assert m.order == 1


def test_progress_formula_optional_only(django_user_model):
    user = _member(django_user_model)
    goal = _active_goal(user)
    a = _milestone(goal, required=False, verification=Verification.SELF)
    _milestone(goal, required=False, verification=Verification.SELF)
    services.complete_milestone(goal, a, user)
    # No required milestones → formula runs over the non-skipped optional ones.
    goal.refresh_from_db()
    assert goal.progress_percent == 50


# --- endorsement model (mentor / officer) ------------------------------------
def test_mentor_milestone_requires_endorsement(django_user_model):
    owner = _member(django_user_model, "owner")
    mentor = _member(django_user_model, "mentor")
    _pair(mentor, owner)
    goal = _goal(owner, visibility=Visibility.MENTOR)
    goal = services.set_goal_status(goal, S.ACTIVE, owner)
    m = _milestone(goal, verification=Verification.MENTOR)

    # No endorsement yet → owner completion is rejected.
    with pytest.raises(ValidationError):
        services.complete_milestone(goal, m, owner)

    services.endorse_milestone(goal, m, mentor, note="held cap chain well")
    done = services.complete_milestone(goal, m, owner)
    assert done.status == MilestoneStatus.DONE
    assert done.evidence_snapshot.get("verifier_role") == Verification.MENTOR
    # doc 09 §1.4: the endorser's raw user id is never frozen onto the owner's milestone.
    assert "verified_by" not in done.evidence_snapshot
    assert mentor.pk not in done.evidence_snapshot.values()


def test_retracted_endorsement_blocks_completion(django_user_model):
    owner = _member(django_user_model, "owner")
    mentor = _member(django_user_model, "mentor")
    _pair(mentor, owner)
    goal = _goal(owner, visibility=Visibility.MENTOR)
    goal = services.set_goal_status(goal, S.ACTIVE, owner)
    m = _milestone(goal, verification=Verification.MENTOR)

    services.endorse_milestone(goal, m, mentor)
    services.retract_endorsement(goal, m, mentor)
    with pytest.raises(ValidationError):
        services.complete_milestone(goal, m, owner)


def test_non_mentor_cannot_endorse(django_user_model):
    owner = _member(django_user_model, "owner")
    stranger = _member(django_user_model, "stranger")
    goal = _goal(owner, visibility=Visibility.MENTOR)
    m = _milestone(goal, verification=Verification.MENTOR)
    with pytest.raises(ValidationError):
        services.endorse_milestone(goal, m, stranger)


def test_officer_endorsement_is_audited(django_user_model):
    owner = _member(django_user_model, "owner")
    officer = _officer(django_user_model, "off")
    goal = _goal(owner, visibility=Visibility.OFFICERS)
    goal = services.set_goal_status(goal, S.ACTIVE, owner)
    m = _milestone(goal, verification=Verification.OFFICER)

    services.endorse_milestone(goal, m, officer, note="signed off", ip="203.0.113.7")
    assert AuditLog.objects.filter(
        action="capsuleer.goal.endorse", target_id=str(goal.pk)
    ).exists()
    done = services.complete_milestone(goal, m, owner)
    assert done.status == MilestoneStatus.DONE


def test_mentor_cannot_endorse_officer_milestone(django_user_model):
    owner = _member(django_user_model, "owner")
    mentor = _member(django_user_model, "mentor")
    _pair(mentor, owner)
    goal = _goal(owner, visibility=Visibility.OFFICERS)  # officer tier, not mentor tier
    m = _milestone(goal, verification=Verification.OFFICER)
    with pytest.raises(ValidationError):
        services.endorse_milestone(goal, m, mentor)


# --- visibility change --------------------------------------------------------
def test_visibility_change_records_activity(django_user_model):
    user = _member(django_user_model)
    goal = _goal(user, visibility=Visibility.PRIVATE)
    services.set_goal_visibility(goal, user, Visibility.OFFICERS)
    goal.refresh_from_db()
    assert goal.visibility == Visibility.OFFICERS
    assert goal.activity_log.filter(verb="visibility.changed").exists()


def test_visibility_change_by_non_owner_rejected(django_user_model):
    owner = _member(django_user_model, "owner")
    other = _member(django_user_model, "other")
    goal = _goal(owner)
    with pytest.raises(ValidationError):
        services.set_goal_visibility(goal, other, Visibility.OFFICERS)
