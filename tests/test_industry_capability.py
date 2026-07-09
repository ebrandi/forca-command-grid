"""Gap C1 — real manufacturing-skill import + pilot industry recommendations.

Verifies the per-pilot "can I build this?" check against the imported blueprint
manufacturing skills (SdeBlueprintSkill), and the readiness industry recommendation:
a doctrine hull below corp min stock that the pilot has the skills to build.
"""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.industry.capability import can_manufacture, manufacturing_skill_requirements
from apps.sde.models import SdeBlueprintSkill, SdeCategory, SdeGroup, SdeType
from apps.sso.models import EveCharacter

FEROX = 16227          # the product (hull)
INDUSTRY = 3380        # a manufacturing skill
GALLENTE_BC = 12209    # a second required skill


def _types():
    cat, _ = SdeCategory.objects.get_or_create(category_id=6, defaults={"name": "Ship"})
    grp, _ = SdeGroup.objects.get_or_create(group_id=419, defaults={"category": cat, "name": "Battlecruiser"})
    for tid, name in [(FEROX, "Ferox"), (INDUSTRY, "Industry"), (GALLENTE_BC, "Caldari Battlecruiser")]:
        SdeType.objects.get_or_create(type_id=tid, defaults={"name": name, "group": grp, "published": True})


def _snapshot(skills: dict):
    """A standalone latest snapshot for a throwaway character."""
    ch = EveCharacter.objects.create(character_id=8801, name="Builder", is_main=True, is_corp_member=True)
    return CharacterSkillSnapshot.objects.create(character=ch, is_latest=True, total_sp=1, skills=skills)


# --- can_manufacture ---------------------------------------------------------
@pytest.mark.django_db
def test_unknown_when_no_blueprint_skill_data():
    _types()
    snap = _snapshot({str(INDUSTRY): {"trained_level": 5}})
    assert manufacturing_skill_requirements(FEROX) == []
    assert can_manufacture(snap, FEROX) is None  # honest: no data → don't claim capability


@pytest.mark.django_db
def test_can_build_when_skills_met():
    _types()
    SdeBlueprintSkill.objects.create(blueprint_type_id=999, product_type_id=FEROX, skill_type_id=INDUSTRY, level=1)
    SdeBlueprintSkill.objects.create(blueprint_type_id=999, product_type_id=FEROX, skill_type_id=GALLENTE_BC, level=3)
    met = _snapshot({str(INDUSTRY): {"trained_level": 1}, str(GALLENTE_BC): {"trained_level": 4}})
    assert can_manufacture(met, FEROX) is True


@pytest.mark.django_db
def test_cannot_build_when_short_a_skill():
    _types()
    SdeBlueprintSkill.objects.create(blueprint_type_id=999, product_type_id=FEROX, skill_type_id=GALLENTE_BC, level=3)
    short = _snapshot({str(GALLENTE_BC): {"trained_level": 2}})  # needs 3
    assert can_manufacture(short, FEROX) is False
    assert can_manufacture(None, FEROX) is False  # no snapshot → can't


# --- pilot industry recommendation -------------------------------------------
def _doctrine_hull(ship_type_id):
    cat, _ = DoctrineCategory.objects.get_or_create(key="bc", label="BC")
    d = Doctrine.objects.create(name="Ferox Fleet", category=cat, priority=100)
    DoctrineFit.objects.create(doctrine=d, name="Ferox", ship_type_id=ship_type_id)
    return d


def _corp_short(type_id, current, target):
    from apps.stockpile.models import Stockpile, StockpileItem

    sp = Stockpile.objects.create(name="Home", kind=Stockpile.Kind.CORP)
    StockpileItem.objects.create(stockpile=sp, type_id=type_id, quantity_current=current, quantity_target=target)


@pytest.mark.django_db
def test_industry_reco_for_buildable_short_hull(sde):
    from apps.readiness.pilot import _industry

    _types()
    _doctrine_hull(FEROX)
    _corp_short(FEROX, current=2, target=10)  # deficit 8
    SdeBlueprintSkill.objects.create(blueprint_type_id=999, product_type_id=FEROX, skill_type_id=INDUSTRY, level=1)
    snap = _snapshot({str(INDUSTRY): {"trained_level": 5}})
    recos = _industry(snap)
    assert len(recos) == 1
    assert recos[0]["category"] == "industry"
    assert recos[0]["ref_id"] == str(FEROX)
    assert "Ferox" in recos[0]["title"] and "8" in recos[0]["title"]


@pytest.mark.django_db
def test_no_industry_reco_when_cannot_build(sde):
    from apps.readiness.pilot import _industry

    _types()
    _doctrine_hull(FEROX)
    _corp_short(FEROX, current=2, target=10)
    SdeBlueprintSkill.objects.create(blueprint_type_id=999, product_type_id=FEROX, skill_type_id=INDUSTRY, level=5)
    snap = _snapshot({str(INDUSTRY): {"trained_level": 1}})  # below required
    assert _industry(snap) == []


@pytest.mark.django_db
def test_no_industry_reco_when_hull_not_short(sde):
    from apps.readiness.pilot import _industry

    _types()
    _doctrine_hull(FEROX)
    _corp_short(FEROX, current=10, target=10)  # no deficit
    SdeBlueprintSkill.objects.create(blueprint_type_id=999, product_type_id=FEROX, skill_type_id=INDUSTRY, level=1)
    snap = _snapshot({str(INDUSTRY): {"trained_level": 5}})
    assert _industry(snap) == []
