"""Capsuleer Path verification engine — per-kind checkers (doc 11 §8-§9).

Each checker's hit / miss / unknown-when-no-data behaviour, the freshness tri-state and monotonic
credit rule, future-only contribution baselines, and the evidence-snapshot shapes frozen at credit.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.capsuleer import verify
from apps.capsuleer.models import GoalType, MilestoneKind, Verification
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement

from ._capsuleer_utils import (
    _character,
    _contribution,
    _goal,
    _member,
    _milestone,
    _skill_type,
    _snapshot,
)

pytestmark = pytest.mark.django_db

SKILL, CAP = 3416, 3417
HULL, ALT_HULL = 11985, 11987


def _pilot(django_user_model, cid=6001):
    user = _member(django_user_model, str(cid))
    return user, _character(user, cid, "Verify Pilot")


def _ms(user, char, kind, params, **kw):
    goal = _goal(user, character=char, goal_type=GoalType.CUSTOM)
    return _milestone(goal, kind=kind, verification=Verification.AUTO, params=params, **kw)


def _asset(char, type_id, qty, *, as_of=None):
    from apps.stockpile.models import Asset

    a = Asset.objects.create(owner_type=Asset.Owner.CHARACTER, owner_id=char.character_id,
                             type_id=type_id, quantity=qty)
    if as_of is not None:
        Asset.objects.filter(pk=a.pk).update(as_of=as_of)
    return a


# --- skill_target ------------------------------------------------------------
def test_skill_target_hit_miss_and_unknown(django_user_model):
    user, char = _pilot(django_user_model)
    _skill_type(SKILL, "Propulsion Jamming")
    m = _ms(user, char, MilestoneKind.SKILL_TARGET, {"skills": [{"type_id": SKILL, "level": 3}]})

    # No snapshot → unknown.
    assert verify.check_safely(m, verify.context_for(char)).state == "unknown"

    _snapshot(char, {SKILL: 2})
    miss = verify.check_safely(m, verify.context_for(char))
    assert miss.met is False and miss.state == "ok"

    _snapshot(char, {SKILL: 4})
    hit = verify.check_safely(m, verify.context_for(char))
    assert hit.met is True and hit.state == "ok"
    assert hit.evidence["kind"] == "skill_target"
    assert hit.evidence["skills"][0]["trained"] == 4


def test_skill_target_stale_is_monotonic(django_user_model):
    user, char = _pilot(django_user_model)
    _skill_type(SKILL, "Propulsion Jamming")
    _snapshot(char, {SKILL: 5}, as_of=timezone.now() - timedelta(days=10))  # past the 7-day threshold
    m = _ms(user, char, MilestoneKind.SKILL_TARGET, {"skills": [{"type_id": SKILL, "level": 3}]})
    result = verify.check_safely(m, verify.context_for(char))
    assert result.met is True and result.state == "stale"
    # Monotonic → stale still credits.
    assert verify.should_credit(MilestoneKind.SKILL_TARGET, result) is True


# --- doctrine_ready ----------------------------------------------------------
def _doctrine_fixture(char, min_level=4, optimal_level=4, trained=None):
    _skill_type(SKILL, "Logistics Cruisers", rank=6)
    cat, _ = DoctrineCategory.objects.get_or_create(key="logi", label="Logi")
    doctrine = Doctrine.objects.create(name="Armor Logi", category=cat, priority=80)
    fit = DoctrineFit.objects.create(doctrine=doctrine, name="Guardian", ship_type_id=HULL)
    SkillRequirement.objects.create(fit=fit, skill_type_id=SKILL, min_level=min_level,
                                    optimal_level=optimal_level)
    _snapshot(char, trained or {})
    return doctrine


def test_doctrine_ready_viable_hit(django_user_model):
    user, char = _pilot(django_user_model)
    doctrine = _doctrine_fixture(char, min_level=4, optimal_level=5, trained={SKILL: 4})
    m = _ms(user, char, MilestoneKind.DOCTRINE_READY,
            {"doctrine_id": doctrine.id, "tier": "viable"})
    result = verify.check_safely(m, verify.context_for(char))
    assert result.met is True
    assert result.evidence["doctrine_id"] == doctrine.id


def test_doctrine_ready_optimal_not_met_when_only_viable(django_user_model):
    user, char = _pilot(django_user_model)
    doctrine = _doctrine_fixture(char, min_level=4, optimal_level=5, trained={SKILL: 4})
    m = _ms(user, char, MilestoneKind.DOCTRINE_READY,
            {"doctrine_id": doctrine.id, "tier": "optimal"})
    assert verify.check_safely(m, verify.context_for(char)).met is False


def test_doctrine_ready_missing_doctrine_is_structural_unknown(django_user_model):
    user, char = _pilot(django_user_model)
    _snapshot(char, {})
    m = _ms(user, char, MilestoneKind.DOCTRINE_READY, {"doctrine_id": 999999, "tier": "viable"})
    result = verify.check_safely(m, verify.context_for(char))
    assert result.met is None and result.state == "unknown" and result.structural is True


def test_doctrine_ready_unresolved_short_circuits(django_user_model):
    user, char = _pilot(django_user_model)
    _snapshot(char, {})
    m = _ms(user, char, MilestoneKind.DOCTRINE_READY, {"unresolved": True, "tier": "viable"})
    result = verify.check_safely(m, verify.context_for(char))
    assert result.met is None and result.structural is True


# --- contribution (future-only baseline) ------------------------------------
def test_contribution_future_only_baseline(django_user_model):
    user, char = _pilot(django_user_model)
    _contribution(user, "fleet", 2)  # pre-existing, captured in the baseline
    m = _ms(user, char, MilestoneKind.CONTRIBUTION,
            {"kinds": ["fleet"], "count": 3, "baseline_count": 2})
    # 2 total − 2 baseline = 0 < 3 → not met.
    assert verify.check_safely(m, verify.context_for(char)).met is False
    _contribution(user, "fleet", 3)  # three new fleets after activation
    hit = verify.check_safely(m, verify.context_for(char))
    assert hit.met is True and hit.state == "ok"
    assert hit.evidence["count_at_credit"] == 5 and hit.evidence["baseline_count"] == 2


# --- combat_first ------------------------------------------------------------
def test_combat_first_hit_and_absent(django_user_model):
    from apps.killboard.models import PilotMilestone

    user, char = _pilot(django_user_model)
    m = _ms(user, char, MilestoneKind.COMBAT_FIRST, {"milestone_key": "first_kill"})
    absent = verify.check_safely(m, verify.context_for(char))
    assert absent.met is False and absent.state == "ok"  # durable store → honest "not yet"

    PilotMilestone.objects.create(character_id=char.character_id, kind="first_kill",
                                  achieved_at=timezone.now(), killmail_id=12345)
    hit = verify.check_safely(m, verify.context_for(char))
    assert hit.met is True and hit.evidence["killmail_id"] == 12345


# --- ship_owned (non-monotonic) ---------------------------------------------
def test_ship_owned_unknown_without_asset_scope(django_user_model):
    user, char = _pilot(django_user_model)
    m = _ms(user, char, MilestoneKind.SHIP_OWNED, {"type_ids": [HULL, ALT_HULL]})
    # No asset rows for the character = no scope → unknown, never "not owned".
    assert verify.check_safely(m, verify.context_for(char)).state == "unknown"


def test_ship_owned_hit_when_owned_fresh(django_user_model):
    user, char = _pilot(django_user_model)
    _asset(char, ALT_HULL, 1)  # owns one of the acceptable hulls, fresh mirror
    m = _ms(user, char, MilestoneKind.SHIP_OWNED, {"type_ids": [HULL, ALT_HULL]})
    result = verify.check_safely(m, verify.context_for(char))
    assert result.met is True and result.state == "ok"
    assert result.evidence["ship_type_id"] == ALT_HULL


def test_ship_owned_stale_does_not_credit(django_user_model):
    user, char = _pilot(django_user_model)
    _asset(char, HULL, 1, as_of=timezone.now() - timedelta(days=3))  # past the 24h asset threshold
    m = _ms(user, char, MilestoneKind.SHIP_OWNED, {"type_ids": [HULL]})
    result = verify.check_safely(m, verify.context_for(char))
    assert result.met is True and result.state == "stale"
    # Non-monotonic → stale must NOT credit.
    assert verify.should_credit(MilestoneKind.SHIP_OWNED, result) is False


def test_check_safely_swallows_checker_errors(django_user_model, monkeypatch):
    user, char = _pilot(django_user_model)
    m = _ms(user, char, MilestoneKind.SKILL_TARGET, {"skills": [{"type_id": SKILL, "level": 3}]})

    def _boom(milestone, ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(verify.CHECKERS, MilestoneKind.SKILL_TARGET, _boom)
    result = verify.check_safely(m, verify.context_for(char))
    assert result.met is None and result.state == "unknown"
