"""SKL-1 — attribute-aware training estimates + remap advisor."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.characters.models import CharacterAttributes
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit, SkillRequirement
from apps.sde.models import SdeType
from apps.skills.services import generate_plan_for_doctrine, plan_remap_advice
from apps.skills.training import remap_advice, sp_per_hour
from apps.sso.models import EveCharacter

# attribute type ids
PER, WIL, INT, MEM, CHA = 167, 168, 165, 166, 164
GUNNERY = 3300


def _attrs(**kw):
    base = dict(intelligence=17, memory=17, perception=17, willpower=17, charisma=17)
    base.update(kw)
    return SimpleNamespace(**base)


# --- sp_per_hour (pure) ------------------------------------------------------
def test_sp_per_hour_uses_attributes():
    attrs = _attrs(perception=27, willpower=21)
    # (27 + 21/2) * 60 = 2250
    assert sp_per_hour(attrs, PER, WIL, 2000) == 2250


def test_sp_per_hour_falls_back_without_data():
    attrs = _attrs(perception=27, willpower=21)
    assert sp_per_hour(None, PER, WIL, 2000) == 2000          # no attributes
    assert sp_per_hour(attrs, None, None, 2000) == 2000        # no skill mapping
    assert sp_per_hour(attrs, 999, WIL, 2000) == 2000          # unknown attribute id


# --- remap_advice (pure) -----------------------------------------------------
def test_remap_advice_recommends_dominant_attributes():
    attrs = _attrs()  # flat 17s → a remap helps a lot
    # a big perception/willpower plan
    specs = [(4_000_000, PER, WIL), (2_000_000, PER, WIL)]
    advice = remap_advice(attrs, specs)
    assert advice is not None
    assert advice["primary"] == "Perception"
    assert advice["secondary"] == "Willpower"
    assert advice["saved_seconds"] > 86400
    assert advice["remapped_seconds"] < advice["current_seconds"]


def test_remap_advice_none_for_short_plan():
    attrs = _attrs()
    assert remap_advice(attrs, [(50_000, PER, WIL)]) is None   # trivially short


def test_remap_advice_none_without_attributes():
    assert remap_advice(None, [(4_000_000, PER, WIL)]) is None


def test_remap_advice_ignores_unmapped_skills():
    attrs = _attrs()
    # skills with no attribute data contribute nothing → no advice
    assert remap_advice(attrs, [(9_000_000, None, None)]) is None


# --- integration: attribute-aware plan estimate ------------------------------
@pytest.mark.django_db
def test_plan_estimate_is_attribute_aware(django_user_model, sde):
    SdeType.objects.filter(type_id=GUNNERY).update(
        rank=1, primary_attribute_id=PER, secondary_attribute_id=WIL
    )
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    doctrine = Doctrine.objects.create(name="Gunline", category=cat, priority=90)
    fit = DoctrineFit.objects.create(doctrine=doctrine, name="Gunline", ship_type_id=587)
    SkillRequirement.objects.create(fit=fit, skill_type_id=GUNNERY, min_level=5, optimal_level=5)

    def _char(cid, attrs=None):
        u = django_user_model.objects.create(username=f"eve:{cid}")
        ch = EveCharacter.objects.create(character_id=cid, user=u, name=f"P{cid}",
                                         is_main=True, is_corp_member=True)
        from apps.characters.models import CharacterSkillSnapshot
        CharacterSkillSnapshot.objects.create(character=ch, is_latest=True, skills={})
        if attrs:
            CharacterAttributes.objects.create(character=ch, **attrs)
        return ch

    fast = _char(9101, {"perception": 27, "willpower": 21})  # trains gunnery faster
    plain = _char(9102)                                       # no attributes → flat rate

    fast_plan = generate_plan_for_doctrine(fast, doctrine)
    plain_plan = generate_plan_for_doctrine(plain, doctrine)
    assert fast_plan.estimated_total_seconds < plain_plan.estimated_total_seconds
    # the fast pilot's rate is 2250 SP/hr vs the flat 2000 → ~11% faster
    assert fast_plan.estimated_total_seconds == pytest.approx(
        plain_plan.estimated_total_seconds * 2000 / 2250, rel=0.01
    )


@pytest.mark.django_db
def test_plan_remap_advice_surfaces_for_long_plan(django_user_model, sde):
    SdeType.objects.filter(type_id=GUNNERY).update(
        rank=8, primary_attribute_id=PER, secondary_attribute_id=WIL
    )
    cat, _ = DoctrineCategory.objects.get_or_create(key="dps", label="DPS")
    doctrine = Doctrine.objects.create(name="Big Guns", category=cat, priority=90)
    fit = DoctrineFit.objects.create(doctrine=doctrine, name="Big Guns", ship_type_id=587)
    SkillRequirement.objects.create(fit=fit, skill_type_id=GUNNERY, min_level=5, optimal_level=5)

    u = django_user_model.objects.create(username="eve:9200")
    ch = EveCharacter.objects.create(character_id=9200, user=u, name="P", is_main=True,
                                     is_corp_member=True)
    from apps.characters.models import CharacterSkillSnapshot
    CharacterSkillSnapshot.objects.create(character=ch, is_latest=True, skills={})
    CharacterAttributes.objects.create(character=ch, intelligence=17, memory=17,
                                       perception=17, willpower=17, charisma=17)
    plan = generate_plan_for_doctrine(ch, doctrine)
    advice = plan_remap_advice(plan)
    assert advice is not None and advice["primary"] == "Perception"
