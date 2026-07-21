"""FSD modifierInfo → SdeModifier graph + skill-dogma import (Tocha's Lab Phase 1).

Exercises the new data-only import path on synthetic FSD structures shaped exactly like the
real data for two skills the plan calls out as spot-checks — CPU Management (ItemModifier on
cpuOutput) and Rapid Launch (LocationRequiredSkillModifier on rate-of-fire) — so the verbatim,
unfiltered normalisation and the scoped skill-dogma replace are locked without downloading
CCP's 110MB SDE. Contrast test_ship_bonus_import.py, which asserts the *filtered* ship-bonus
projection of the same graph.
"""
from __future__ import annotations

import pytest

from apps.admin_audit.models import AppSetting
from apps.sde.management.commands.import_ship_bonuses import Command
from apps.sde.models import (
    SdeCategory,
    SdeDbuff,
    SdeDbuffModifier,
    SdeGroup,
    SdeModifier,
    SdeType,
    SdeTypeAttribute,
    SdeTypeEffect,
)

CPU_MGMT, RAPID_LAUNCH = 3426, 21071
EFF_CPU, EFF_RAPID, EFF_MISC = 5100, 1763, 9000
ATTR_CPU_BONUS, ATTR_ROF_BONUS = 202, 293       # per-level bonus attrs on the skills
CPU_OUTPUT, ROF_SPEED = 48, 51                   # the attributes those skills modify
MISSILE_LAUNCHER_SKILL = 3319                    # LocationRequiredSkillModifier target skill
MODULE_TYPE = 484                                # a non-skill (fittable) type, for scope tests


def _fsd():
    """(effects, type_dogma) shaped like the FSD dogmaEffects.yaml / typeDogma.yaml members."""
    effects = {
        EFF_CPU: {"effectName": "cpuManagement", "modifierInfo": [
            {"func": "ItemModifier", "modifiedAttributeID": CPU_OUTPUT,
             "modifyingAttributeID": ATTR_CPU_BONUS, "operation": 6, "domain": "shipID"}]},
        EFF_RAPID: {"effectName": "rapidLaunch", "modifierInfo": [
            {"func": "LocationRequiredSkillModifier", "modifiedAttributeID": ROF_SPEED,
             "modifyingAttributeID": ATTR_ROF_BONUS, "operation": 6,
             "skillTypeID": MISSILE_LAUNCHER_SKILL, "domain": "shipID"}]},
        # Two modifiers that the *ship-bonus* import would DROP (non-postPercent, and a
        # postAssign) — the graph must keep them verbatim.
        EFF_MISC: {"effectName": "misc", "modifierInfo": [
            {"func": "LocationGroupModifier", "modifiedAttributeID": 30,
             "modifyingAttributeID": 400, "operation": 2, "groupID": 74},
            {"func": "ItemModifier", "modifiedAttributeID": 9,
             "modifyingAttributeID": 401, "operation": 7}]},
        # An effect with a malformed / func-less modifier that must be skipped.
        1: {"effectName": "empty", "modifierInfo": [
            {"modifiedAttributeID": 1, "operation": 6}, "not-a-dict"]},
    }
    type_dogma = {
        CPU_MGMT: {"dogmaAttributes": [{"attributeID": ATTR_CPU_BONUS, "value": 5.0},
                                       {"attributeID": 275, "value": 1.0}],   # 275 = rank
                   "dogmaEffects": [{"effectID": EFF_CPU, "isDefault": False}]},
        RAPID_LAUNCH: {"dogmaAttributes": [{"attributeID": ATTR_ROF_BONUS, "value": -3.0}],
                       "dogmaEffects": [{"effectID": EFF_RAPID, "isDefault": False}]},
        # A non-skill type present in typeDogma — must be ignored by _build_skill_dogma.
        MODULE_TYPE: {"dogmaAttributes": [{"attributeID": CPU_OUTPUT, "value": 400.0}],
                      "dogmaEffects": [{"effectID": EFF_MISC}]},
    }
    return effects, type_dogma


# --- graph normalisation (pure) ------------------------------------------- #

def test_build_modifiers_is_verbatim_and_unfiltered():
    effects, _ = _fsd()
    rows = Command()._build_modifiers(effects)
    # 1 (cpu) + 1 (rapid) + 2 (misc) = 4; the func-less / non-dict entries are dropped.
    assert len(rows) == 4
    by_eff = {}
    for r in rows:
        by_eff.setdefault(r["effect_id"], []).append(r)

    cpu = by_eff[EFF_CPU][0]
    assert cpu["func"] == "ItemModifier" and cpu["operation"] == 6
    assert cpu["modified_attribute_id"] == CPU_OUTPUT
    assert cpu["modifying_attribute_id"] == ATTR_CPU_BONUS
    assert cpu["domain"] == "shipID" and cpu["group_id"] is None and cpu["skill_type_id"] is None

    rapid = by_eff[EFF_RAPID][0]
    assert rapid["func"] == "LocationRequiredSkillModifier" and rapid["operation"] == 6
    assert rapid["modified_attribute_id"] == ROF_SPEED
    assert rapid["skill_type_id"] == MISSILE_LAUNCHER_SKILL

    # The modAdd + postAssign modifiers the ship-bonus mapping filters out are kept here.
    ops = {r["operation"] for r in by_eff[EFF_MISC]}
    assert ops == {2, 7}
    grp = next(r for r in by_eff[EFF_MISC] if r["operation"] == 2)
    assert grp["func"] == "LocationGroupModifier" and grp["group_id"] == 74


# --- skill dogma (pure) --------------------------------------------------- #

def test_build_skill_dogma_scoped_to_skills():
    _, type_dogma = _fsd()
    skill_ids = {CPU_MGMT, RAPID_LAUNCH}
    attr_rows, effect_rows = Command()._build_skill_dogma(type_dogma, skill_ids)

    attrs = {(r["type_id"], r["attribute_id"]): r["value"] for r in attr_rows}
    assert attrs[(CPU_MGMT, ATTR_CPU_BONUS)] == 5.0
    assert attrs[(CPU_MGMT, 275)] == 1.0
    assert attrs[(RAPID_LAUNCH, ATTR_ROF_BONUS)] == -3.0
    # The non-skill type in type_dogma must not leak into skill dogma.
    assert not any(r["type_id"] == MODULE_TYPE for r in attr_rows)
    assert not any(r["type_id"] == MODULE_TYPE for r in effect_rows)

    effs = {(r["type_id"], r["effect_id"]) for r in effect_rows}
    assert (CPU_MGMT, EFF_CPU) in effs and (RAPID_LAUNCH, EFF_RAPID) in effs


# --- write path (DB) ------------------------------------------------------ #

@pytest.fixture
def skills(db):
    SdeCategory.objects.get_or_create(category_id=16, defaults={"name": "Skill"})
    SdeCategory.objects.get_or_create(category_id=7, defaults={"name": "Module"})
    SdeGroup.objects.get_or_create(group_id=255, defaults={"category_id": 16, "name": "Skills"})
    SdeGroup.objects.get_or_create(group_id=55, defaults={"category_id": 7, "name": "Turret"})
    SdeType.objects.get_or_create(type_id=CPU_MGMT, defaults={"group_id": 255, "name": "CPU Management"})
    SdeType.objects.get_or_create(type_id=RAPID_LAUNCH, defaults={"group_id": 255, "name": "Rapid Launch"})
    SdeType.objects.get_or_create(type_id=MODULE_TYPE, defaults={"group_id": 55, "name": "200mm AC I"})
    return {CPU_MGMT, RAPID_LAUNCH}


def test_write_graph_full_replace_and_version(skills):
    effects, type_dogma = _fsd()
    cmd = Command()
    mods = cmd._build_modifiers(effects)
    attr_rows, effect_rows = cmd._build_skill_dogma(type_dogma, skills)

    cmd._write_graph(mods, attr_rows, effect_rows, skills)

    assert SdeModifier.objects.count() == 4
    # The exact CPU Management row the plan spot-checks.
    assert SdeModifier.objects.filter(
        effect_id=EFF_CPU, func="ItemModifier", modified_attribute_id=CPU_OUTPUT,
        modifying_attribute_id=ATTR_CPU_BONUS, operation=6).exists()
    # The exact Rapid Launch row the plan spot-checks.
    assert SdeModifier.objects.filter(
        effect_id=EFF_RAPID, func="LocationRequiredSkillModifier",
        modified_attribute_id=ROF_SPEED, skill_type_id=MISSILE_LAUNCHER_SKILL, operation=6).exists()

    assert SdeTypeAttribute.objects.get(type_id=CPU_MGMT, attribute_id=ATTR_CPU_BONUS).value == 5.0
    assert SdeTypeEffect.objects.filter(type_id=RAPID_LAUNCH, effect_id=EFF_RAPID).exists()
    assert AppSetting.objects.filter(key="dogma_graph_version").exists()

    # Idempotent: a second run is a clean full replace, no growth.
    cmd._write_graph(mods, attr_rows, effect_rows, skills)
    assert SdeModifier.objects.count() == 4
    assert SdeTypeAttribute.objects.filter(type_id=CPU_MGMT, attribute_id=ATTR_CPU_BONUS).count() == 1


def test_write_graph_does_not_touch_non_skill_type_attributes(skills):
    # A module attribute (owned by the Fuzzwork import) must survive the scoped skill replace.
    SdeTypeAttribute.objects.create(type_id=MODULE_TYPE, attribute_id=CPU_OUTPUT, value=17.0)
    effects, type_dogma = _fsd()
    cmd = Command()
    mods = cmd._build_modifiers(effects)
    attr_rows, effect_rows = cmd._build_skill_dogma(type_dogma, skills)

    cmd._write_graph(mods, attr_rows, effect_rows, skills)

    assert SdeTypeAttribute.objects.get(type_id=MODULE_TYPE, attribute_id=CPU_OUTPUT).value == 17.0


# --- WS-7 warfare buffs (dbuffCollections) -------------------------------- #

def _dbuffs():
    """Shaped like dbuffCollections.yaml: a shield-resist item buff (2 attrs), an AB/MWD
    location-required-skill buff (2 skills), an EWAR location-group buff, and two that must
    be skipped (no modifiers / no operation)."""
    return {
        10: {"aggregateMode": "Minimum", "operationName": "PostPercent",
             "developerDescription": "Shield Burst: Shield Harmonizing: Shield Resistance",
             "itemModifiers": [{"dogmaAttributeID": 271}, {"dogmaAttributeID": 272}]},
        22: {"aggregateMode": "Maximum", "operationName": "PostPercent",
             "developerDescription": "Skirmish Burst: Rapid Deployment: AB/MWD Speed",
             "locationRequiredSkillModifiers": [{"dogmaAttributeID": 20, "skillID": 3450},
                                                {"dogmaAttributeID": 20, "skillID": 3454}]},
        17: {"aggregateMode": "Maximum", "operationName": "PostPercent",
             "developerDescription": "Information Burst: Electronic Superiority",
             "locationGroupModifiers": [{"dogmaAttributeID": 54, "groupID": 201}]},
        999: {"aggregateMode": "Maximum", "operationName": "PostPercent",
              "developerDescription": "buff with no modifiers — skipped"},
        998: {"aggregateMode": "Maximum",   # no operationName — skipped
              "itemModifiers": [{"dogmaAttributeID": 1}]},
    }


def test_build_dbuffs_shapes_and_skips():
    buff_rows, mod_rows = Command()._build_dbuffs(_dbuffs())
    by_id = {r["buff_id"]: r for r in buff_rows}
    assert set(by_id) == {10, 22, 17}                       # 999 (no mods) + 998 (no op) skipped
    assert by_id[10]["aggregate_mode"] == "Minimum" and by_id[10]["operation"] == "PostPercent"

    mods_by_buff: dict[int, list] = {}
    for r in mod_rows:
        mods_by_buff.setdefault(r["buff_id"], []).append(r)
    # Shield resistance: two item modifiers on the shield resonance attrs.
    b10 = mods_by_buff[10]
    assert {m["modified_attribute_id"] for m in b10} == {271, 272}
    assert all(m["kind"] == "item" and m["group_id"] is None and m["skill_type_id"] is None
               for m in b10)
    # AB/MWD: two locationRequiredSkill modifiers carrying the skill ids.
    b22 = mods_by_buff[22]
    assert all(m["kind"] == "locationRequiredSkill" and m["modified_attribute_id"] == 20
               for m in b22)
    assert {m["skill_type_id"] for m in b22} == {3450, 3454}
    # EWAR: one locationGroup modifier carrying the group id.
    b17 = mods_by_buff[17]
    assert b17[0]["kind"] == "locationGroup" and b17[0]["group_id"] == 201


def test_write_graph_writes_dbuffs_full_replace(skills):
    cmd = Command()
    buff_rows, mod_rows = cmd._build_dbuffs(_dbuffs())
    cmd._write_graph([], [], [], skills, buff_rows, mod_rows)

    assert SdeDbuff.objects.count() == 3
    assert SdeDbuffModifier.objects.filter(buff_id=10).count() == 2
    assert SdeDbuffModifier.objects.filter(
        buff_id=22, kind="locationRequiredSkill", skill_type_id=3450).exists()
    # Idempotent full replace: a second run neither grows nor duplicates.
    cmd._write_graph([], [], [], skills, buff_rows, mod_rows)
    assert SdeDbuff.objects.count() == 3
    assert SdeDbuffModifier.objects.filter(buff_id=10).count() == 2


def test_write_graph_absent_dbuff_member_preserves_existing(skills):
    # A prior good dbuff import must survive a later run whose zip lacked the member
    # (dbuff_rows omitted / empty) — mirrors the ship-bonus "refuse to wipe" stance.
    cmd = Command()
    buff_rows, mod_rows = cmd._build_dbuffs(_dbuffs())
    cmd._write_graph([], [], [], skills, buff_rows, mod_rows)
    assert SdeDbuff.objects.count() == 3

    cmd._write_graph([], [], [], skills)          # no dbuff args (unit-test / pre-buff zip)
    assert SdeDbuff.objects.count() == 3          # preserved, not wiped
    cmd._write_graph([], [], [], skills, [], [])  # empty (absent member)
    assert SdeDbuff.objects.count() == 3
