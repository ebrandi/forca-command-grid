"""SKL-2 — plan auto-reconciliation on skill import.

Acceptance: a trained planned skill auto-marks its step done on the next import, and
the plan's remaining time recalculates. Idempotent.
"""
from __future__ import annotations

import pytest
import responses
from django.utils import timezone

from apps.characters.models import CharacterSkillSnapshot
from apps.characters.services import import_character_skills
from apps.skills.models import SkillPlan, SkillPlanStep
from apps.skills.services import reconcile_plans_from_snapshot
from apps.sso.models import AuthToken

pytestmark = pytest.mark.django_db


def _plan_with_steps(character, steps):
    """steps: list of (skill_type_id, target_level, estimated_seconds)."""
    plan = SkillPlan.objects.create(character=character, name="Test plan", goal="custom")
    for order, (sid, level, secs) in enumerate(steps):
        SkillPlanStep.objects.create(
            plan=plan, order=order, skill_type_id=sid, target_level=level,
            estimated_seconds=secs, status=SkillPlanStep.Status.PENDING,
        )
    return plan


def _snapshot(character, levels):
    return CharacterSkillSnapshot.objects.create(
        character=character, is_latest=True, total_sp=1,
        skills={str(sid): {"trained_level": lvl, "sp": 1} for sid, lvl in levels.items()},
    )


def test_trained_step_marked_done(character):
    plan = _plan_with_steps(character, [(3331, 4, 3600), (3301, 5, 7200)])
    snap = _snapshot(character, {3331: 4, 3301: 3})  # first trained, second not (3 < 5)
    done = reconcile_plans_from_snapshot(character, snap)
    assert done == 1
    steps = {s.skill_type_id: s.status for s in plan.steps.all()}
    assert steps[3331] == SkillPlanStep.Status.DONE
    assert steps[3301] == SkillPlanStep.Status.PENDING


def test_remaining_time_recalculates(character):
    plan = _plan_with_steps(character, [(3331, 4, 3600), (3301, 5, 7200)])
    plan.estimated_total_seconds = 10800
    plan.save(update_fields=["estimated_total_seconds"])
    snap = _snapshot(character, {3331: 4})  # only the 3600s step is done
    reconcile_plans_from_snapshot(character, snap)
    plan.refresh_from_db()
    assert plan.estimated_total_seconds == 7200  # only the not-done step remains


def test_partial_level_stays_pending(character):
    plan = _plan_with_steps(character, [(3331, 5, 3600)])
    snap = _snapshot(character, {3331: 4})  # trained to 4, target 5 → not done
    assert reconcile_plans_from_snapshot(character, snap) == 0
    assert plan.steps.get().status == SkillPlanStep.Status.PENDING


def test_reconcile_is_idempotent(character):
    plan = _plan_with_steps(character, [(3331, 4, 3600)])
    snap = _snapshot(character, {3331: 4})
    assert reconcile_plans_from_snapshot(character, snap) == 1
    assert reconcile_plans_from_snapshot(character, snap) == 0  # already done
    assert plan.steps.get().status == SkillPlanStep.Status.DONE


def test_none_snapshot_is_noop(character):
    _plan_with_steps(character, [(3331, 4, 3600)])
    assert reconcile_plans_from_snapshot(character, None) == 0


@responses.activate
def test_import_triggers_reconcile(character):
    plan = _plan_with_steps(character, [(3331, 4, 3600), (3301, 5, 7200)])
    token = AuthToken(
        character=character, scopes=["esi-skills.read_skills.v1"],
        access_expires_at=timezone.now() + timezone.timedelta(hours=1),
    )
    token.refresh_token = "r"
    token.access_token = "valid-access"
    token.save()
    responses.add(
        responses.GET, "https://esi.evetech.net/characters/1001/skills/",
        json={"total_sp": 1_000_000, "skills": [
            {"skill_id": 3331, "trained_skill_level": 4, "skillpoints_in_skill": 90510},
        ]},
        status=200,
    )
    import_character_skills(character)
    steps = {s.skill_type_id: s.status for s in plan.steps.all()}
    assert steps[3331] == SkillPlanStep.Status.DONE  # auto-ticked by the import
    assert steps[3301] == SkillPlanStep.Status.PENDING
