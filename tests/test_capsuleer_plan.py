"""Capsuleer Path plan generation + template instantiation (build_plan, instantiate_template).

Covers the shared prerequisite expander wired into a real plan, the minimum/recommended/mastery
split, SkillPlan writing + idempotent refresh, already-trained pruning, honest (unknown) costing,
the activation side-effects (plan build + contribution baseline stamp), and doctrine/template
instantiation including the degraded no-doctrine path.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.capsuleer import plan as plan_mod
from apps.capsuleer import services
from apps.capsuleer.models import GoalStatus, GoalType, MilestoneKind, Verification
from apps.capsuleer.templates_builtin import sync_builtin_templates
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.skills.models import SkillPlan

from ._capsuleer_utils import (
    _character,
    _member,
    _milestone,
    _ship_type,
    _skill_type,
    _snapshot,
)

pytestmark = pytest.mark.django_db

# skill ids
LOGI, CAP, CRUISER, ENERGY = 3416, 3417, 3418, 3419
HULL = 11985


def _pilot(django_user_model, cid=5001):
    user = _member(django_user_model, str(cid))
    char = _character(user, cid, "Plan Pilot")
    return user, char


# --- build_plan: ship goal with prereqs -------------------------------------
def test_build_plan_expands_prereqs_and_prunes_trained(django_user_model):
    user, char = _pilot(django_user_model)
    # HULL needs Logistics Cruisers IV; that skill needs Cruiser III (prereq).
    _skill_type(CRUISER, "Racial Cruiser", rank=5)
    _skill_type(LOGI, "Logistics Cruisers", rank=6, prereqs=[(CRUISER, 3)])
    _ship_type(HULL, "Osprey", required_skills=[(LOGI, 4)])
    _snapshot(char, {CRUISER: 3})  # cruiser already trained → pruned

    goal = services.create_goal(user, title="Fly Osprey", goal_type=GoalType.SHIP,
                                character=char, ship_type_id=HULL)
    plan = plan_mod.build_plan(goal)
    assert plan is not None and plan.goal == SkillPlan.Goal.CUSTOM
    goal.refresh_from_db()
    assert goal.skill_plan_id == plan.pk
    steps = {s.skill_type_id: s for s in plan.steps.all()}
    # Cruiser III is already trained (pruned); Logistics Cruisers IV remains.
    assert CRUISER not in steps
    assert LOGI in steps and steps[LOGI].target_level == 4
    assert steps[LOGI].reason == "minimum qualification"
    assert plan.estimated_total_seconds > 0


def test_build_plan_idempotent_refresh(django_user_model):
    user, char = _pilot(django_user_model)
    _skill_type(LOGI, "Logistics Cruisers", rank=6)
    _ship_type(HULL, "Osprey", required_skills=[(LOGI, 4)])
    _snapshot(char, {})
    goal = services.create_goal(user, title="Fly Osprey", goal_type=GoalType.SHIP,
                                character=char, ship_type_id=HULL)
    first = plan_mod.build_plan(goal)
    goal.refresh_from_db()
    second = plan_mod.build_plan(goal)
    # The prior plan is replaced, not duplicated.
    assert SkillPlan.objects.filter(character=char, goal=SkillPlan.Goal.CUSTOM).count() == 1
    assert first.pk != second.pk
    goal.refresh_from_db()
    assert goal.skill_plan_id == second.pk


def test_build_plan_none_without_skill_targets(django_user_model):
    user, char = _pilot(django_user_model)
    goal = services.create_goal(user, title="Be a mentor", goal_type=GoalType.CUSTOM,
                                character=char)
    assert plan_mod.build_plan(goal) is None


# --- costing honesty ---------------------------------------------------------
def test_cost_unknown_without_price(django_user_model):
    user, char = _pilot(django_user_model)
    _ship_type(HULL, "Osprey")  # no market price
    goal = services.create_goal(user, title="Fly Osprey", goal_type=GoalType.SHIP,
                                character=char, ship_type_id=HULL)
    cost = plan_mod.estimate_initial_cost(goal)
    assert cost["unknown"] is True and cost["isk"] == Decimal("0")


def test_cost_known_with_market_price(django_user_model):
    from apps.market.models import MarketPrice

    user, char = _pilot(django_user_model)
    _ship_type(HULL, "Osprey")
    MarketPrice.objects.create(type_id=HULL, location=None,
                               profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal("42000000"))
    goal = services.create_goal(user, title="Fly Osprey", goal_type=GoalType.SHIP,
                                character=char, ship_type_id=HULL)
    cost = plan_mod.estimate_initial_cost(goal)
    assert cost["unknown"] is False and cost["isk"] == Decimal("42000000")


# --- doctrine goal: min/recommended split -----------------------------------
def test_build_plan_doctrine_minimum_and_recommended(django_user_model):
    user, char = _pilot(django_user_model)
    _skill_type(LOGI, "Logistics Cruisers", rank=6)
    _skill_type(CAP, "Capacitor Management", rank=3)
    cat, _ = DoctrineCategory.objects.get_or_create(key="logi", label="Logi")
    doctrine = Doctrine.objects.create(name="Armor Logi", category=cat, priority=80)
    fit = DoctrineFit.objects.create(doctrine=doctrine, name="Guardian", ship_type_id=HULL)
    SkillRequirement.objects.create(fit=fit, skill_type_id=LOGI, min_level=4, optimal_level=5)
    SkillRequirement.objects.create(fit=fit, skill_type_id=CAP, min_level=4, optimal_level=4)
    _snapshot(char, {})

    goal = services.create_goal(user, title="Fly the logi doctrine", goal_type=GoalType.DOCTRINE,
                                character=char, doctrine_id=doctrine.id)
    plan = plan_mod.build_plan(goal)
    reasons = {s.skill_type_id: s.reason for s in plan.steps.all()}
    levels = {s.skill_type_id: s.target_level for s in plan.steps.all()}
    # Logistics Cruisers has optimal>min → it lands in the recommended band at level 5.
    assert levels[LOGI] == 5 and reasons[LOGI] == "recommended support"
    # Capacitor Management has optimal==min → stays minimum at level 4.
    assert levels[CAP] == 4 and reasons[CAP] == "minimum qualification"


# --- activation wiring: plan + contribution baseline ------------------------
def test_activation_builds_plan_and_stamps_baseline(django_user_model):
    user, char = _pilot(django_user_model)
    _skill_type(LOGI, "Logistics Cruisers", rank=6)
    _ship_type(HULL, "Osprey", required_skills=[(LOGI, 4)])
    _snapshot(char, {})
    # A prior fleet contribution exists — the baseline must capture it so only future fleets count.
    from ._capsuleer_utils import _contribution
    _contribution(user, "fleet", 2)

    goal = services.create_goal(user, title="Fly Osprey", goal_type=GoalType.SHIP,
                                character=char, ship_type_id=HULL)
    ms = _milestone(goal, kind=MilestoneKind.CONTRIBUTION, verification=Verification.AUTO,
                    params={"kinds": ["fleet"], "count": 3})
    goal = services.set_goal_status(goal, GoalStatus.ACTIVE, user)

    goal.refresh_from_db()
    assert goal.skill_plan_id is not None                       # plan built on activation
    ms.refresh_from_db()
    assert ms.params["baseline_count"] == 2                     # future-only baseline stamped


# --- template instantiation --------------------------------------------------
def test_instantiate_builtin_template_makes_valid_milestones(django_user_model):
    sync_builtin_templates()
    from apps.capsuleer.models import CareerTemplate

    user, char = _pilot(django_user_model)
    template = CareerTemplate.objects.get(key="tackle_pilot")
    # Resolve the ship names the template references so ship_owned milestones instantiate.
    for tid, name in [(11400, "Atron"), (11401, "Slasher"), (11402, "Executioner"),
                      (11403, "Merlin")]:
        _ship_type(tid, name)
    _skill_type(3435, "Propulsion Jamming", rank=2)

    goal = plan_mod.instantiate_template(template, user, character=char)
    assert goal.goal_type == GoalType.TEMPLATE
    assert goal.milestones.count() >= 6
    # Every materialised milestone carries validator-passing params (no exception raised above).
    skill_ms = goal.milestones.filter(kind=MilestoneKind.SKILL_TARGET).first()
    assert skill_ms and skill_ms.params["skills"][0]["type_id"] == 3435


def test_instantiate_doctrine_linked_resolves_placeholder(django_user_model):
    sync_builtin_templates()
    from apps.capsuleer.models import CareerTemplate

    user, char = _pilot(django_user_model)
    cat, _ = DoctrineCategory.objects.get_or_create(key="logi", label="Logi")
    doctrine = Doctrine.objects.create(name="Corp Logistics", category=cat, priority=90)
    DoctrineFit.objects.create(doctrine=doctrine, name="Logi", ship_type_id=HULL, role="logi")

    template = CareerTemplate.objects.get(key="logistics_pilot")
    goal = plan_mod.instantiate_template(template, user, character=char)
    assert goal.doctrine_id == doctrine.id
    dr = goal.milestones.filter(kind=MilestoneKind.DOCTRINE_READY).first()
    assert dr and dr.params.get("doctrine_id") == doctrine.id
    assert dr.params.get("tier") in ("viable", "optimal")


def test_instantiate_doctrine_linked_degrades_without_doctrine(django_user_model):
    sync_builtin_templates()
    from apps.capsuleer.models import CareerTemplate

    user, char = _pilot(django_user_model)  # no matching doctrine exists
    template = CareerTemplate.objects.get(key="logistics_pilot")
    goal = plan_mod.instantiate_template(template, user, character=char)
    assert goal.doctrine_id is None
    dr = goal.milestones.filter(kind=MilestoneKind.DOCTRINE_READY).first()
    assert dr and dr.params.get("unresolved") is True
    assert dr.check_state == "unknown"
