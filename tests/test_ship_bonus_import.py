"""FSD modifierInfo → SdeShipBonus mapping (apps.sde import_ship_bonuses).

Exercises the mapping on synthetic FSD structures shaped exactly like the real data for the
Ferox (turret damage + role range) and Cerberus (charge-domain missile damage + elite ROF),
so the classification (shipBonus/eliteBonus/roleBonus → skill; func → domain/filter) is
locked without downloading CCP's 110MB SDE.
"""
from __future__ import annotations

import pytest

from apps.sde.management.commands.import_ship_bonuses import Command
from apps.sde.models import SdeCategory, SdeGroup, SdeShipBonus, SdeType

FEROX, CERBERUS = 16227, 11993


@pytest.fixture
def ships(db):
    SdeCategory.objects.get_or_create(category_id=6, defaults={"name": "Ship"})
    SdeGroup.objects.get_or_create(group_id=419, defaults={"category_id": 6, "name": "Combat BC"})
    SdeType.objects.get_or_create(type_id=FEROX, defaults={"group_id": 419, "name": "Ferox"})
    SdeType.objects.get_or_create(type_id=CERBERUS, defaults={"group_id": 419, "name": "Cerberus"})
    return True


def _fsd():
    attrs = {
        182: {"name": "requiredSkill1"}, 183: {"name": "requiredSkill2"},
        745: {"name": "shipBonusCBC2"}, 2043: {"name": "roleBonusCBC"},
        487: {"name": "shipBonusCC"}, 693: {"name": "eliteBonusHeavyGunship2"},
    }
    type_dogma = {
        FEROX: {"dogmaAttributes": [{"attributeID": 182, "value": 33096.0},
                                    {"attributeID": 745, "value": 5.0},
                                    {"attributeID": 2043, "value": 25.0}],
                "dogmaEffects": [{"effectID": 6177}, {"effectID": 6173}]},
        CERBERUS: {"dogmaAttributes": [{"attributeID": 182, "value": 3334.0},
                                       {"attributeID": 183, "value": 16591.0},
                                       {"attributeID": 487, "value": 5.0},
                                       {"attributeID": 693, "value": -5.0}],
                   "dogmaEffects": [{"effectID": 100}, {"effectID": 101}]},
    }
    effects = {
        6177: {"effectName": "shipHybridDmg1CBC2", "modifierInfo": [
            {"func": "LocationRequiredSkillModifier", "modifiedAttributeID": 64,
             "modifyingAttributeID": 745, "operation": 6, "skillTypeID": 3304}]},
        6173: {"effectName": "battlecruiserMHTRange", "modifierInfo": [
            {"func": "LocationRequiredSkillModifier", "modifiedAttributeID": 54,
             "modifyingAttributeID": 2043, "operation": 6, "skillTypeID": 3304}]},
        100: {"effectName": "shipMissileKineticDamageCC", "modifierInfo": [
            {"func": "OwnerRequiredSkillModifier", "modifiedAttributeID": 117,
             "modifyingAttributeID": 487, "operation": 6, "skillTypeID": 3319}]},
        101: {"effectName": "eliteRof", "modifierInfo": [
            {"func": "LocationGroupModifier", "modifiedAttributeID": 51,
             "modifyingAttributeID": 693, "operation": 6, "groupID": 510}]},
    }
    return attrs, type_dogma, effects


def test_ferox_damage_and_role(ships):
    rows = {(r["ship_type_id"], r["target_attribute_id"]): r for r in Command()._build_rows(*_fsd())}
    dmg = rows[(FEROX, 64)]                                   # +5% hybrid damage / Caldari BC level
    assert dmg["amount"] == 5.0 and dmg["per_level"] is True
    assert dmg["skill_type_id"] == 33096                     # shipBonus → requiredSkill1
    assert dmg["match_required_skill_id"] == 3304 and dmg["target_domain"] == "item"
    role = rows[(FEROX, 54)]                                  # role optimal bonus: flat, always-on
    assert role["per_level"] is False and role["skill_type_id"] is None and role["amount"] == 25.0


def test_cerberus_charge_damage_and_elite_rof(ships):
    rows = {(r["ship_type_id"], r["target_attribute_id"]): r for r in Command()._build_rows(*_fsd())}
    kin = rows[(CERBERUS, 117)]                               # kinetic missile damage → on the charge
    assert kin["target_domain"] == "charge" and kin["match_required_skill_id"] == 3319
    assert kin["skill_type_id"] == 3334                      # shipBonusCC → requiredSkill1 (racial)
    rof = rows[(CERBERUS, 51)]                                # elite ROF → scaled by the T2 skill
    assert rof["match_group_ids"] == [510] and rof["skill_type_id"] == 16591  # eliteBonus → requiredSkill2
    assert rof["amount"] == -5.0


def test_ship_bonus_role_attr_is_flat_not_per_level(ships):
    """A shipBonusRole* / eliteBonus*Role* attribute is a FLAT role bonus despite the
    shipBonus/eliteBonus prefix — treating it as per-level (×500%/level) would be catastrophic."""
    attrs = {182: {"name": "requiredSkill1"}, 183: {"name": "requiredSkill2"},
             770: {"name": "shipBonusRole7"}, 771: {"name": "eliteBonusViolatorsRole1"}}
    td = {FEROX: {"dogmaAttributes": [{"attributeID": 182, "value": 33096.0},
                                      {"attributeID": 183, "value": 16591.0},
                                      {"attributeID": 770, "value": 500.0},
                                      {"attributeID": 771, "value": 200.0}],
                  "dogmaEffects": [{"effectID": 1}, {"effectID": 2}]}}
    effects = {
        1: {"effectName": "shipBonusDroneDamageRole", "modifierInfo": [
            {"func": "OwnerRequiredSkillModifier", "modifiedAttributeID": 64,
             "modifyingAttributeID": 770, "operation": 6, "skillTypeID": 3436}]},
        2: {"effectName": "eliteViolatorsRole", "modifierInfo": [
            {"func": "LocationGroupModifier", "modifiedAttributeID": 51,
             "modifyingAttributeID": 771, "operation": 6, "groupID": 74}]},
    }
    rows = {r["target_attribute_id"]: r for r in Command()._build_rows(attrs, td, effects)}
    assert rows[64]["per_level"] is False and rows[64]["skill_type_id"] is None    # shipBonusRole → flat
    assert rows[64]["amount"] == 500.0
    assert rows[51]["per_level"] is False and rows[51]["skill_type_id"] is None    # eliteBonus*Role* → flat


def test_skips_non_postpercent_and_unknown_prefix(ships):
    attrs, td, effects = _fsd()
    # a modAdd modifier and an unrecognised bonus attribute must both be dropped
    attrs[999] = {"name": "someOtherAttr"}
    effects[6177]["modifierInfo"].append(
        {"func": "LocationRequiredSkillModifier", "modifiedAttributeID": 30,
         "modifyingAttributeID": 745, "operation": 2, "skillTypeID": 3304})   # modAdd → skip
    effects[6177]["modifierInfo"].append(
        {"func": "ItemModifier", "modifiedAttributeID": 30,
         "modifyingAttributeID": 999, "operation": 6})                        # unknown prefix → skip
    rows = Command()._build_rows(attrs, td, effects)
    ferox = [r for r in rows if r["ship_type_id"] == FEROX]
    assert all(r["target_attribute_id"] != 30 for r in ferox)                 # neither leaked in


def test_write_is_full_replace(ships):
    cmd = Command()
    rows = cmd._build_rows(*_fsd())
    cmd._write(rows)
    n = SdeShipBonus.objects.count()
    assert n == len(rows) and n > 0
    assert SdeShipBonus.objects.filter(ship_type_id=CERBERUS, target_domain="charge").exists()
    cmd._write(rows)                                          # idempotent: full replace, no growth
    assert SdeShipBonus.objects.count() == n
