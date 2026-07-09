"""Phase 6b — infrastructure & leadership dimensions (providers + role qualification)."""
from __future__ import annotations

import copy
import datetime as dt

import pytest
from django.utils import timezone

from apps.characters.models import CharacterSkillSnapshot
from apps.corporation.models import CorpStructure
from apps.operations.models import SovStructure
from apps.readiness import config
from apps.readiness.models import StrategicRoleTarget
from apps.readiness.services import compute_dimension, compute_readiness
from apps.sso.models import EveCharacter

GUNNERY = 3300


def _member(django_user_model, cid, gunnery=0):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(
        character_id=cid, name=f"P{cid}", is_main=True, is_corp_member=True, user=user
    )
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": gunnery, "sp": 0}}
    )
    return ch


# --- infrastructure ----------------------------------------------------------
@pytest.mark.django_db
def test_infrastructure_unavailable_without_structures():
    assert compute_dimension("infrastructure").status == "unavailable"


@pytest.mark.django_db
def test_infrastructure_scores_fuel_and_sov():
    CorpStructure.objects.create(
        structure_id=1, name="Fortizar", type_id=35833,
        fuel_expires=timezone.now() + dt.timedelta(days=20),
    )
    SovStructure.objects.create(structure_id=2, alliance_id=1, solar_system_id=30000142, adm=5.0)
    result = compute_dimension("infrastructure")
    keys = {k.key for k in result.kpis}
    assert "infrastructure.fuel_cover" in keys
    assert "infrastructure.sov_health" in keys
    assert result.score is not None


@pytest.mark.django_db
def test_infrastructure_low_fuel_emits_finding():
    CorpStructure.objects.create(
        structure_id=3, name="Astrahus", type_id=35832,
        fuel_expires=timezone.now() + dt.timedelta(days=1),  # < 3d red
    )
    result = compute_dimension("infrastructure")
    assert any(f.kpi_key == "infrastructure.fuel_cover" for f in result.findings)


# --- leadership --------------------------------------------------------------
@pytest.mark.django_db
def test_leadership_officer_coverage_from_config(django_user_model):
    # Default responsibilities define 6 owner tags, all with empty users → 0% filled.
    result = compute_dimension("leadership")
    cov = next(k for k in result.kpis if k.key == "leadership.officer_coverage")
    assert cov.detail["filled"] == 0 and cov.detail["defined"] == 6
    assert cov.score == 0
    assert any(f.kpi_key == "leadership.officer_coverage" for f in result.findings)


@pytest.mark.django_db
def test_leadership_fc_bench_from_skills_role_target(django_user_model):
    # Two members; one meets the FC skill threshold.
    _member(django_user_model, 7001, gunnery=5)
    _member(django_user_model, 7002, gunnery=1)
    StrategicRoleTarget.objects.create(
        role_key="fc", label="Fleet Commander", desired_count=2,
        detection=StrategicRoleTarget.Detection.SKILLS,
        detection_params={"skills": {str(GUNNERY): 5}},
    )
    result = compute_dimension("leadership")
    fc = next(k for k in result.kpis if k.key == "leadership.fc_bench")
    assert fc.detail["qualified"] == 1 and fc.detail["desired"] == 2
    assert fc.score == 50  # 1 of 2


@pytest.mark.django_db
def test_leadership_manual_role_target_excluded(django_user_model):
    # A manual-detection target can't be auto-counted → no fc_bench KPI (honest).
    StrategicRoleTarget.objects.create(
        role_key="fc", label="FC", desired_count=3,
        detection=StrategicRoleTarget.Detection.MANUAL,
    )
    result = compute_dimension("leadership")
    assert not any(k.key == "leadership.fc_bench" for k in result.kpis)


# --- index integration -------------------------------------------------------
@pytest.mark.django_db
def test_new_dims_disabled_by_default(django_user_model, sde):
    _member(django_user_model, 7101)
    result = compute_readiness(use_cache=False)
    assert "infrastructure" not in result["dimensions"]
    assert "leadership" not in result["dimensions"]


@pytest.mark.django_db
def test_enabling_leadership_adds_it_to_index(django_user_model):
    doc = copy.deepcopy(config.DEFAULTS["dimensions"])
    doc["leadership"]["enabled"] = True
    config.set("dimensions", doc, user=None)
    result = compute_readiness(use_cache=False)
    assert "leadership" in result["dimensions"]
    assert result["dimensions"]["leadership"] is not None  # officer_coverage always scores
