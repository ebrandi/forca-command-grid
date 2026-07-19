"""Generic dogma-graph → BonusSpec translation (Tocha's Lab Phase 2).

Locks the func → domain/target classification against the exact modifier shapes the real CCP
graph carries (verified against the prod SdeModifier table): turret-group damage (Surgical
Strike), required-skill damage/RoF (Medium Projectile / Gunnery), ship-attribute skills
(Shield Management / Evasive Maneuvering), charge-domain missile damage (Warhead Upgrades),
owner-required drone damage (Drone Interfacing). No DB needed — the translator is pure.
"""
from __future__ import annotations

from dataclasses import dataclass

from apps.fitting.engine.modifiers import build_skill_bonus_specs, spec_from_skill_modifier


@dataclass
class _M:
    """A stand-in for one SdeModifier row (only the fields the translator reads)."""
    func: str
    operation: int | None
    modified_attribute_id: int | None
    modifying_attribute_id: int | None
    group_id: int | None = None
    skill_type_id: int | None = None


DMG_BONUS_ATTR = 292        # damageMultiplierBonus — the skill's per-level value lives here
DMG_MULT, SPEED, CPU_OUT, AGILITY = 64, 51, 48, 70
EM_DMG = 114                # a charge-damage attribute


def test_location_group_modifier_is_item_group_scoped():
    # Surgical Strike: +3% turret damage, hitting turret GROUP 55.
    m = _M("LocationGroupModifier", 6, DMG_MULT, DMG_BONUS_ATTR, group_id=55)
    spec = spec_from_skill_modifier(3315, 3.0, m, "Surgical Strike")
    assert spec.target_domain == "item" and spec.match_group_ids == (55,)
    assert spec.target_attr == DMG_MULT and spec.amount == 3.0 and spec.per_level
    assert spec.skill_id == 3315 and spec.penalised is False
    assert spec.factor(5) == 1.15                                  # +3%/level × 5


def test_required_skill_modifier_is_item_reqskill_scoped():
    # Medium Projectile: +5% damage on turrets that REQUIRE Medium Projectile Turret (3305).
    m = _M("LocationRequiredSkillModifier", 6, DMG_MULT, DMG_BONUS_ATTR, skill_type_id=3305)
    spec = spec_from_skill_modifier(3305, 5.0, m, "Medium Projectile")
    assert spec.target_domain == "item" and spec.match_required_skill_id == 3305
    assert spec.match_group_ids == () and spec.amount == 5.0


def test_item_modifier_is_ship_domain():
    # Shield Management: +5% shieldCapacity on the ship; Evasive Maneuvering: -5% agility.
    sm = spec_from_skill_modifier(3419, 5.0, _M("ItemModifier", 6, 263, 337), "Shield Management")
    assert sm.target_domain == "ship" and sm.target_attr == 263 and sm.amount == 5.0
    ev = spec_from_skill_modifier(3453, -5.0, _M("ItemModifier", 6, AGILITY, 151), "Evasive")
    assert ev.target_domain == "ship" and ev.factor(5) == 0.75     # -5%/level × 5


def test_owner_required_charge_damage_is_charge_domain():
    # Warhead Upgrades: +2% missile (charge) damage, owner's charges requiring Missile Launcher Op.
    m = _M("OwnerRequiredSkillModifier", 6, EM_DMG, DMG_BONUS_ATTR, skill_type_id=3319)
    spec = spec_from_skill_modifier(20315, 2.0, m, "Warhead Upgrades")
    assert spec.target_domain == "charge" and spec.match_required_skill_id == 3319


def test_owner_required_drone_damage_is_item_domain():
    # Drone Interfacing: +10% drone damage on owner's drones requiring the Drones skill (3436).
    m = _M("OwnerRequiredSkillModifier", 6, DMG_MULT, DMG_BONUS_ATTR, skill_type_id=3436)
    spec = spec_from_skill_modifier(3442, 10.0, m, "Drone Interfacing")
    assert spec.target_domain == "item" and spec.match_required_skill_id == 3436
    assert spec.skill_id == 3442                                   # scales with Drone Interfacing


def test_non_postpercent_and_missing_value_and_unknown_func_are_skipped():
    base = dict(modified_attribute_id=CPU_OUT, modifying_attribute_id=202)
    assert spec_from_skill_modifier(3426, 5.0, _M("ItemModifier", 2, **base), "x") is None  # modAdd
    assert spec_from_skill_modifier(3426, 9, _M("ItemModifier", 9, **base), "x") is None     # skill-infra op
    assert spec_from_skill_modifier(3426, None, _M("ItemModifier", 6, **base), "x") is None  # no value
    assert spec_from_skill_modifier(3426, 0.0, _M("ItemModifier", 6, **base), "x") is None   # zero value
    assert spec_from_skill_modifier(3426, 5.0, _M("EffectStopper", 6, **base), "x") is None  # unknown func


def test_build_catalogue_excludes_hardcoded_and_dedups():
    skill_attrs = {
        3305: {DMG_BONUS_ATTR: 5.0},        # hand-coded (Medium Projectile) — must be excluded
        9999: {DMG_BONUS_ATTR: 4.0},        # a NEW skill — must be included
    }
    effect_ids_by_skill = {3305: [1], 9999: [2, 3]}
    mod = _M("LocationRequiredSkillModifier", 6, DMG_MULT, DMG_BONUS_ATTR, skill_type_id=9999)
    modifiers_by_effect = {1: [mod], 2: [mod], 3: [mod]}   # effect 2 & 3 carry the SAME modifier
    labels = {9999: "New Skill"}
    specs = build_skill_bonus_specs(
        skill_attrs, effect_ids_by_skill, modifiers_by_effect, labels, frozenset({3305}))
    assert [s.skill_id for s in specs] == [9999]           # 3305 excluded
    assert len(specs) == 1                                 # deduped across effects 2 and 3
    assert specs[0].match_required_skill_id == 9999 and specs[0].amount == 4.0
