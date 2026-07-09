"""Phase 6c — strategic & fleet_comp dimensions (mandatory ships + role coverage)."""
from __future__ import annotations

import copy

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.readiness import config
from apps.readiness.models import MandatoryShip, StrategicRoleTarget
from apps.readiness.services import compute_dimension, compute_readiness
from apps.sso.models import EveCharacter
from apps.stockpile.models import Asset

GUNNERY = 3300
INTERCEPTOR = 11176


def _member(django_user_model, cid, gunnery=0):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    ch = EveCharacter.objects.create(
        character_id=cid, name=f"P{cid}", is_main=True, is_corp_member=True, user=user
    )
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": gunnery, "sp": 0}}
    )
    return ch


def _owns(character_id, type_id, qty=1):
    Asset.objects.create(
        owner_type=Asset.Owner.CHARACTER, owner_id=character_id, type_id=type_id, quantity=qty
    )


def _skills_role(role_key, desired, level=5):
    return StrategicRoleTarget.objects.create(
        role_key=role_key, label=role_key.title(), desired_count=desired,
        detection=StrategicRoleTarget.Detection.SKILLS,
        detection_params={"skills": {str(GUNNERY): level}},
    )


# --- strategic ---------------------------------------------------------------
@pytest.mark.django_db
def test_strategic_unavailable_without_config():
    assert compute_dimension("strategic").status == "unavailable"


@pytest.mark.django_db
def test_strategic_mandatory_ship_coverage(django_user_model):
    c1 = _member(django_user_model, 8001)
    _member(django_user_model, 8002)  # doesn't own it
    _owns(c1.character_id, INTERCEPTOR)
    MandatoryShip.objects.create(label="Travel Ceptor", category="travel", ship_type_id=INTERCEPTOR)
    result = compute_dimension("strategic")
    cov = next(k for k in result.kpis if k.key == "strategic.mandatory_ship_coverage")
    assert cov.score == 50  # 1 of 2 own it
    assert any(f.kpi_key == "strategic.mandatory_ship_coverage" for f in result.findings)


@pytest.mark.django_db
def test_strategic_capital_bench(django_user_model):
    _member(django_user_model, 8101, gunnery=5)  # qualifies
    _member(django_user_model, 8102, gunnery=1)
    _skills_role("dread", desired=2)
    result = compute_dimension("strategic")
    assert any(k.key == "strategic.capital_bench" for k in result.kpis)


# --- fleet_comp --------------------------------------------------------------
@pytest.mark.django_db
def test_fleet_comp_unavailable_without_role_targets():
    assert compute_dimension("fleet_comp").status == "unavailable"


@pytest.mark.django_db
def test_fleet_comp_role_coverage_and_logi(django_user_model):
    _member(django_user_model, 8201, gunnery=5)
    _member(django_user_model, 8202, gunnery=1)
    _skills_role("logi", desired=2)
    result = compute_dimension("fleet_comp")
    keys = {k.key for k in result.kpis}
    assert "fleet_comp.role_coverage" in keys
    assert "fleet_comp.logi_ratio" in keys
    logi = next(k for k in result.kpis if k.key == "fleet_comp.logi_ratio")
    assert logi.detail["qualified"] == 1 and logi.detail["desired"] == 2


# --- index integration -------------------------------------------------------
@pytest.mark.django_db
def test_final_dims_disabled_by_default(django_user_model, sde):
    _member(django_user_model, 8301)
    result = compute_readiness(use_cache=False)
    assert "strategic" not in result["dimensions"]
    assert "fleet_comp" not in result["dimensions"]


@pytest.mark.django_db
def test_all_twelve_dimensions_registered():
    from apps.readiness.engine import registry

    # The four v1 dims + the eight net-new ones across phases 3/6a/6b/6c.
    expected = {
        "doctrine", "skill", "stock", "logistics",  # v1 (mapped to doctrine/combat/industrial)
        "financial", "srp", "activity", "recruitment",
        "leadership", "infrastructure", "strategic", "fleet_comp",
    }
    assert expected <= set(registry.keys())


@pytest.mark.django_db
def test_enabling_fleet_comp_with_targets_adds_it(django_user_model):
    _member(django_user_model, 8401, gunnery=5)
    _skills_role("logi", desired=1)
    doc = copy.deepcopy(config.DEFAULTS["dimensions"])
    doc["fleet_comp"]["enabled"] = True
    config.set("dimensions", doc, user=None)
    result = compute_readiness(use_cache=False)
    assert "fleet_comp" in result["dimensions"]
    assert result["dimensions"]["fleet_comp"] is not None
