"""Capsuleer Path model tests: enums, defaults, DB constraints, params validators, seed idempotency.

These pin what the *database* and the pure validators guarantee (named unique + check constraints,
field defaults, the per-kind ``params`` rules with the two agreed amendments, and idempotent
built-in seeding) so a future migration or edit that drops one fails loudly. Stateful rules live in
``services.py`` and are covered by ``test_capsuleer_services.py``.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.capsuleer.models import (
    CareerGoal,
    CareerMilestone,
    CareerProfile,
    CareerTemplate,
    GoalStatus,
    GoalType,
    MilestoneKind,
    PathSuggestion,
    ProgressSnapshot,
    SuggestionStatus,
    Verification,
    Visibility,
)
from apps.capsuleer.params import validate_milestone_params
from apps.capsuleer.templates_builtin import (
    BUILTIN,
    BUILTIN_KEYS,
    sync_builtin_templates,
    validate_structure,
)

from ._capsuleer_utils import _goal, _member

pytestmark = pytest.mark.django_db


# --- defaults -----------------------------------------------------------------
def test_profile_defaults(django_user_model):
    user = _member(django_user_model)
    p = CareerProfile.objects.create(user=user)
    assert p.pace == "balanced"
    assert p.corp_alignment == "balanced"
    assert p.default_visibility == Visibility.PRIVATE
    assert p.mentor_interest is False
    assert p.monthly_budget_isk is None
    assert p.preferred_activities == []
    assert p.suggestion_muted_kinds == []


def test_goal_defaults(django_user_model):
    user = _member(django_user_model)
    g = CareerGoal.objects.create(user=user, title="Fly logi", goal_type=GoalType.CUSTOM)
    assert g.status == GoalStatus.CONSIDERING
    assert g.priority == "secondary"
    assert g.pace == "inherit"
    assert g.visibility == Visibility.PRIVATE
    assert g.progress_percent == 0
    assert g.corp_alignment_optin is False
    assert g.budget_isk is None


def test_milestone_defaults(django_user_model):
    g = _goal(_member(django_user_model))
    m = CareerMilestone.objects.create(goal=g, order=1, title="x", kind=MilestoneKind.MANUAL)
    assert m.status == "pending"
    assert m.verification == Verification.AUTO
    assert m.check_state == "unknown"
    assert m.params == {}
    assert m.evidence_snapshot == {}


# --- enum value sets (doc 07 §5) ---------------------------------------------
def test_enum_value_sets_match_the_spec():
    assert [v for v, _ in GoalStatus.choices] == [
        "considering", "active", "paused", "completed", "abandoned", "archived",
    ]
    assert [v for v, _ in MilestoneKind.choices] == [
        "skill_target", "doctrine_ready", "ship_owned", "contribution", "combat_first",
        "practical", "manual",
    ]
    # The suggestion status set must carry ``incorrect`` (spec §7.3).
    assert set(SuggestionStatus.values) == {
        "open", "accepted", "dismissed", "deferred", "not_interested", "incorrect",
    }
    assert set(Verification.values) == {"auto", "self", "mentor", "officer"}
    assert set(Visibility.values) == {"private", "mentor", "officers", "aggregate_only"}


# --- DB check constraints -----------------------------------------------------
def test_goal_progress_check_constraint(django_user_model):
    user = _member(django_user_model)
    with pytest.raises(IntegrityError), transaction.atomic():
        CareerGoal.objects.create(
            user=user, title="x", goal_type=GoalType.CUSTOM, progress_percent=101
        )


def test_template_difficulty_check_constraint():
    with pytest.raises(IntegrityError), transaction.atomic():
        CareerTemplate.objects.create(key="bad", name="Bad", category="mining", difficulty=4)


def test_snapshot_percent_check_constraint(django_user_model):
    g = _goal(_member(django_user_model))
    with pytest.raises(IntegrityError), transaction.atomic():
        ProgressSnapshot.objects.create(
            goal=g, percent=101, milestones_done=1, milestones_total=1
        )


# --- DB unique constraints ----------------------------------------------------
def test_milestone_order_unique_per_goal(django_user_model):
    g = _goal(_member(django_user_model))
    CareerMilestone.objects.create(goal=g, order=1, title="a", kind=MilestoneKind.MANUAL)
    with pytest.raises(IntegrityError), transaction.atomic():
        CareerMilestone.objects.create(goal=g, order=1, title="b", kind=MilestoneKind.MANUAL)


def test_template_key_unique():
    CareerTemplate.objects.create(key="dup", name="One", category="mining")
    with pytest.raises(IntegrityError), transaction.atomic():
        CareerTemplate.objects.create(key="dup", name="Two", category="mining")


def test_suggestion_dedupe_key_unique(django_user_model):
    user = _member(django_user_model)
    PathSuggestion.objects.create(
        user=user, kind="stalled_goal", title="x", reason="y", dedupe_key="u1:stalled_goal:goal:1"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        PathSuggestion.objects.create(
            user=user, kind="stalled_goal", title="z", reason="w",
            dedupe_key="u1:stalled_goal:goal:1",
        )


def test_goal_active_template_partial_unique(django_user_model):
    """One live goal per (user, template_key) where a template is set; a completed one frees the
    slot for a fresh instantiation (doc 07 §4.3)."""
    user = _member(django_user_model)
    tpl = CareerTemplate.objects.create(key="logi", name="Logi", category="combat_support")
    CareerGoal.objects.create(
        user=user, title="a", goal_type=GoalType.TEMPLATE, template=tpl, template_key="logi",
        status=GoalStatus.ACTIVE,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        CareerGoal.objects.create(
            user=user, title="b", goal_type=GoalType.TEMPLATE, template=tpl, template_key="logi",
            status=GoalStatus.CONSIDERING,
        )
    # Completing the live goal frees the slot — a new live instantiation is then allowed.
    CareerGoal.objects.filter(user=user, template_key="logi").update(status=GoalStatus.COMPLETED)
    CareerGoal.objects.create(
        user=user, title="d", goal_type=GoalType.TEMPLATE, template=tpl, template_key="logi",
        status=GoalStatus.CONSIDERING,
    )


# --- params validators (doc 07 §6 + the two amendments) ----------------------
def test_skill_target_validator():
    validate_milestone_params("skill_target", {"skills": [{"type_id": 3426, "level": 4}]}, "auto")
    with pytest.raises(ValidationError):
        validate_milestone_params("skill_target", {"skills": []}, "auto")
    with pytest.raises(ValidationError):
        validate_milestone_params("skill_target", {"skills": [{"type_id": 0, "level": 4}]}, "auto")
    with pytest.raises(ValidationError):
        validate_milestone_params("skill_target", {"skills": [{"type_id": 1, "level": 6}]}, "auto")


def test_doctrine_ready_tier_amendment():
    # tier is optional (defaults to viable) and, when present, must be viable|optimal.
    validate_milestone_params("doctrine_ready", {"doctrine_id": 42}, "auto")
    validate_milestone_params("doctrine_ready", {"doctrine_id": 42, "tier": "optimal"}, "auto")
    with pytest.raises(ValidationError):
        validate_milestone_params("doctrine_ready", {"doctrine_id": 42, "tier": "bogus"}, "auto")
    with pytest.raises(ValidationError):
        validate_milestone_params("doctrine_ready", {"doctrine_id": 0}, "auto")


def test_ship_owned_validator():
    validate_milestone_params("ship_owned", {"type_ids": [11985]}, "auto")
    validate_milestone_params("ship_owned", {"type_ids": [1, 2], "require_fitted": True}, "auto")
    with pytest.raises(ValidationError):
        validate_milestone_params("ship_owned", {"type_ids": []}, "auto")


def test_contribution_baseline_is_rejected():
    validate_milestone_params("contribution", {"kinds": ["fleet"], "count": 3}, "auto")
    # baseline_count is system-stamped, never author-supplied.
    with pytest.raises(ValidationError):
        validate_milestone_params(
            "contribution", {"kinds": ["fleet"], "count": 3, "baseline_count": 5}, "auto"
        )


def test_combat_first_validator():
    validate_milestone_params("combat_first", {"milestone_key": "first_kill"}, "auto")
    with pytest.raises(ValidationError):
        validate_milestone_params("combat_first", {"milestone_key": ""}, "auto")


def test_practical_instructions_optional_amendment():
    # instructions is optional (amendment); a bare practical with only self-verification is valid.
    validate_milestone_params("practical", {}, "self")
    validate_milestone_params("practical", {"instructions": "Hold cap chain."}, "mentor")
    # unknown keys rejected, and practical can never be auto-verified.
    with pytest.raises(ValidationError):
        validate_milestone_params("practical", {"bogus": 1}, "self")
    with pytest.raises(ValidationError):
        validate_milestone_params("practical", {}, "auto")


def test_manual_rejects_any_key_and_auto():
    validate_milestone_params("manual", {}, "self")
    with pytest.raises(ValidationError):
        validate_milestone_params("manual", {"x": 1}, "self")
    with pytest.raises(ValidationError):
        validate_milestone_params("manual", {}, "auto")


def test_auto_only_for_auto_capable_kinds():
    # skill_target may be auto; manual/practical may not (verification/kind agreement).
    validate_milestone_params("skill_target", {"skills": [{"type_id": 1, "level": 1}]}, "auto")
    with pytest.raises(ValidationError):
        validate_milestone_params("manual", {}, "auto")


# --- built-in templates + seed idempotency (doc 07 §7, doc 15) ---------------
def test_all_builtin_structures_are_valid():
    assert len(BUILTIN) == 13
    for template in BUILTIN:
        validate_structure(template["structure"])


def test_seed_is_idempotent():
    # The initial data migration already seeded; re-running the sync must not duplicate rows.
    first = sync_builtin_templates()
    assert first == 13
    sync_builtin_templates()
    assert CareerTemplate.objects.filter(source="builtin").count() == 13
    assert set(
        CareerTemplate.objects.filter(source="builtin").values_list("key", flat=True)
    ) == set(BUILTIN_KEYS)


def test_wormhole_explorer_advanced_from_explorer():
    sync_builtin_templates()
    wh = CareerTemplate.objects.get(key="wormhole_explorer")
    explorer = CareerTemplate.objects.get(key="explorer")
    assert wh.advanced_from_id == explorer.pk


def test_str_methods(django_user_model):
    g = CareerGoal.objects.create(
        user=_member(django_user_model), title="Fly logi", goal_type=GoalType.CUSTOM
    )
    assert str(g) == "Fly logi"
    tpl = CareerTemplate.objects.create(key="k", name="Tackle Pilot", category="tackle_scout")
    assert str(tpl) == "Tackle Pilot"


def test_money_scale_is_two_decimals(django_user_model):
    user = _member(django_user_model)
    g = CareerGoal.objects.create(
        user=user, title="x", goal_type=GoalType.CUSTOM, budget_isk=Decimal("1000000000.50")
    )
    g.refresh_from_db()
    assert g.budget_isk == Decimal("1000000000.50")
