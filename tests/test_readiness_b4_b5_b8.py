"""Gap B4/B5/B8 — Fleet Support + Asset Staging dimensions and the build_capacity KPI.

B4 (``support``) and B5 (``staging``) are config-gated dimensions: unavailable until
leadership populates their config, and they ship disabled so the live index is
unchanged. B8 adds a display-only ``stock.build_capacity`` KPI (score unaffected).
"""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.readiness.models import FleetSupportSkill, StagingSystem
from apps.readiness.services import compute_dimension, compute_readiness
from apps.sde.models import SdeCategory, SdeGroup, SdeType
from apps.sso.models import EveCharacter

GUNNERY, ARMORED_CMD, RIFTER, BP = 3300, 20494, 587, 681


def _type(type_id, name, category_id):
    cat, _ = SdeCategory.objects.get_or_create(category_id=category_id, defaults={"name": str(category_id)})
    grp, _ = SdeGroup.objects.get_or_create(
        group_id=category_id * 100, defaults={"category": cat, "name": f"g{category_id}"})
    return SdeType.objects.get_or_create(
        type_id=type_id, defaults={"name": name, "group": grp, "published": True})[0]


def _char(cid, skills=None):
    ch = EveCharacter.objects.create(character_id=cid, name=f"P{cid}", is_main=True, is_corp_member=True)
    if skills is not None:
        CharacterSkillSnapshot.objects.create(
            character=ch, is_latest=True,
            skills={str(s): {"trained_level": lvl, "sp": 0} for s, lvl in skills.items()})
    return ch


# --- B4: Fleet Support dimension ---------------------------------------------
@pytest.mark.django_db
def test_support_unavailable_without_skills():
    _char(7001, {GUNNERY: 5})
    result = compute_dimension("support")
    assert result.score is None and result.status == "unavailable"


@pytest.mark.django_db
def test_support_scores_skill_coverage():
    _type(ARMORED_CMD, "Armored Command", 16)
    FleetSupportSkill.objects.create(skill_type_id=ARMORED_CMD, skill_name="Armored Command", min_level=4)
    _char(7101, {ARMORED_CMD: 5})   # meets it
    _char(7102, {ARMORED_CMD: 2})   # below level
    _char(7103, None)               # unknown — excluded from denominator
    result = compute_dimension("support")
    # 1 of the 2 known members has it at L4 → 50.
    assert result.score == 50
    assert f"support.skill_{ARMORED_CMD}" in {k.key for k in result.kpis}


@pytest.mark.django_db
def test_support_ships_disabled_so_index_excludes_it():
    """Even fully configured, support is disabled by default → not in the index payload."""
    _type(ARMORED_CMD, "Armored Command", 16)
    FleetSupportSkill.objects.create(skill_type_id=ARMORED_CMD, min_level=1)
    _char(7201, {ARMORED_CMD: 5})
    payload = compute_readiness(use_cache=False)
    assert "support" not in payload["dimensions"]


# --- B5: Asset Staging dimension ---------------------------------------------
def _doctrine_hull(name=" hull"):
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    d = Doctrine.objects.create(name=name, category=cat, priority=10)
    DoctrineFit.objects.create(doctrine=d, name=name, ship_type_id=RIFTER)
    return d


@pytest.mark.django_db
def test_staging_unavailable_without_system():
    _doctrine_hull()
    assert compute_dimension("staging").score is None


@pytest.mark.django_db
def test_staging_scores_hulls_at_system():
    from apps.stockpile.models import Asset, AssetLocation

    _doctrine_hull()
    ch = _char(7301, {GUNNERY: 5})
    StagingSystem.objects.create(system_id=30000142, system_name="Jita", active=True)
    here = AssetLocation.objects.create(location_id=60003760, system_id=30000142, kind="station")
    away = AssetLocation.objects.create(location_id=60003761, system_id=30002187, kind="station")
    Asset.objects.create(owner_type=Asset.Owner.CHARACTER, owner_id=ch.character_id,
                         location=here, type_id=RIFTER, quantity=3)
    Asset.objects.create(owner_type=Asset.Owner.CHARACTER, owner_id=ch.character_id,
                         location=away, type_id=RIFTER, quantity=1)
    result = compute_dimension("staging")
    assert result.score == 75  # 3 of 4 doctrine hulls are at Jita
    assert {k.key for k in result.kpis} == {"staging.hulls_at_staging"}


# --- B8: build_capacity KPI in the industrial (stock) dimension --------------
@pytest.mark.django_db
def test_build_capacity_kpi_owns_blueprint_and_skilled():
    from apps.industry.models import Blueprint
    from apps.sde.models import SdeBlueprintSkill

    _type(RIFTER, "Rifter", 6)
    _type(GUNNERY, "Industry", 16)
    _doctrine_hull()
    SdeBlueprintSkill.objects.create(
        blueprint_type_id=BP, product_type_id=RIFTER, skill_type_id=GUNNERY,
        level=1, activity=SdeBlueprintSkill.MANUFACTURING)
    Blueprint.objects.create(type_id=BP, is_corp=True, is_original=True)
    _char(7401, {GUNNERY: 5})   # has the manufacturing skill
    kpis = {k.key: k.score for k in compute_dimension("stock").kpis}
    assert kpis.get("stock.build_capacity") == 100  # 1/1 hull buildable


@pytest.mark.django_db
def test_build_capacity_zero_without_blueprint():
    from apps.sde.models import SdeBlueprintSkill

    _type(RIFTER, "Rifter", 6)
    _type(GUNNERY, "Industry", 16)
    _doctrine_hull()
    SdeBlueprintSkill.objects.create(
        blueprint_type_id=BP, product_type_id=RIFTER, skill_type_id=GUNNERY,
        level=1, activity=SdeBlueprintSkill.MANUFACTURING)
    _char(7501, {GUNNERY: 5})   # skilled, but corp owns no blueprint
    kpis = {k.key: k.score for k in compute_dimension("stock").kpis}
    assert kpis.get("stock.build_capacity") == 0


@pytest.mark.django_db
def test_build_capacity_absent_without_recipe_data():
    _doctrine_hull()
    _char(7601, {GUNNERY: 5})
    kpis = {k.key for k in compute_dimension("stock").kpis}
    assert "stock.build_capacity" not in kpis  # no SdeBlueprintSkill rows → honest absence
