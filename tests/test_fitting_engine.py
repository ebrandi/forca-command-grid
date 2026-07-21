"""Tocha's Lab engine unit + ORM-adapter wiring tests.

The pure stacking-penalty maths and the ``slot_from_effects`` helper are checked here
against hand-computed values; the ORM provider's data-version resolution and its bridging
of the hull's legacy slot-count columns are exercised through the real SDE tables. The
headline calculation numbers (DPS, EHP, mobility, capacitor, EWAR, missile application)
are checked against hand-derived values in the golden suite (tests/test_fitting_golden_*),
which drives the v2 graph evaluator with full dogma-graph fixtures. The fixture data here
is original (invented type ids in the 90000x range), not copied from any external source.
"""
from __future__ import annotations

import pytest

from apps.fitting.engine import attributes as A
from apps.fitting.engine import stacking
from apps.fitting.engine.adapter import FittingEngine, ORMDataProvider, slot_from_effects
from apps.fitting.engine.types import (
    FitInput,
    ModuleInput,
    ModuleState,
    OperatingProfile,
    SkillProfile,
    SlotKind,
)

# --- ids (original fixture) --------------------------------------------------
SHIP, AC, FUSION, GYRO, EXT, HARD, AB, DRONE = 900001, 900010, 900020, 900030, 900040, 900050, 900060, 900070
LAUNCHER, ROCKET, BCS = 900080, 900090, 900100
WEB, SCRAM, NEUT, ECM_M = 900110, 900120, 900130, 900140
S_MINFRIG, S_GUNNERY, S_SPT, S_SURGICAL, S_RAPID, S_SHIELDMGMT, S_NAV = 3331, 3300, 3320, 3315, 3310, 3419, 3449


def _types() -> dict:
    R = A.SHIELD_RESONANCE
    AR = A.ARMOR_RESONANCE
    HR = A.HULL_RESONANCE
    return {
        SHIP: {"name": "Test Frigate", "group_id": 25, "category_id": 6, "skills": [(S_MINFRIG, 1)],
               "attrs": {
                   A.CPU_OUTPUT: 125, A.POWER_OUTPUT: 42, A.CALIBRATION: 400,
                   A.HI_SLOTS: 4, A.MED_SLOTS: 3, A.LOW_SLOTS: 3, A.RIG_SLOTS: 3,
                   A.TURRET_HARDPOINTS: 3, A.LAUNCHER_HARDPOINTS: 1,
                   A.SHIELD_HP: 450, A.ARMOR_HP: 400, A.HULL_HP: 350,
                   R["em"]: 1.0, R["thermal"]: 0.8, R["kinetic"]: 0.6, R["explosive"]: 0.5,
                   AR["em"]: 0.5, AR["thermal"]: 0.65, AR["kinetic"]: 0.75, AR["explosive"]: 0.9,
                   HR["em"]: 1.0, HR["thermal"]: 1.0, HR["kinetic"]: 1.0, HR["explosive"]: 1.0,
                   A.CAP_CAPACITY: 350, A.CAP_RECHARGE_RATE: 187500,
                   A.MASS: 1200000, A.AGILITY: 3.0, A.MAX_VELOCITY: 350, A.SIGNATURE_RADIUS: 35,
                   A.WARP_SPEED_MULT: 5.0, A.MAX_TARGET_RANGE: 25000, A.MAX_LOCKED_TARGETS: 5,
                   A.SCAN_RESOLUTION: 650, A.SENSOR_STRENGTHS["gravimetric"]: 11, A.CAPACITY_CARGO: 140,
                   A.DRONE_BANDWIDTH: 5, A.DRONE_CAPACITY: 5,
               }},
        AC: {"name": "Test 200mm AutoCannon", "group_id": 55, "category_id": 7,
             "skills": [(S_GUNNERY, 1), (S_SPT, 1)], "effects": [A.EFFECT_TURRET],
             "attrs": {A.CPU_USAGE: 8, A.POWER_USAGE: 6, A.DAMAGE_MULTIPLIER: 1.0,
                       A.RATE_OF_FIRE: 2500, A.OPTIMAL_RANGE: 1200, A.FALLOFF: 7500,
                       A.TRACKING_SPEED: 0.2}},
        FUSION: {"name": "Test Fusion S", "group_id": 83, "category_id": 8,
                 "attrs": {A.EXPLOSIVE_DAMAGE: 9.0}},
        GYRO: {"name": "Test Gyrostabilizer", "group_id": 59, "category_id": 7,
               "attrs": {A.CPU_USAGE: 18, A.POWER_USAGE: 1, A.DAMAGE_MULTIPLIER: 1.10,
                         A.ROF_MULTIPLIER: 0.90}},
        EXT: {"name": "Test Shield Extender", "group_id": 40, "category_id": 7,
              "attrs": {A.CPU_USAGE: 20, A.POWER_USAGE: 12, A.SHIELD_EXTENDER_HP_BONUS: 268}},
        HARD: {"name": "Test Shield Amplifier", "group_id": 295, "category_id": 7,
               "attrs": {A.CPU_USAGE: 18, A.POWER_USAGE: 1,
                         A.SHIELD_RESONANCE["em"]: 0.70, A.SHIELD_RESONANCE["thermal"]: 0.70,
                         A.SHIELD_RESONANCE["kinetic"]: 0.70, A.SHIELD_RESONANCE["explosive"]: 0.70}},
        AB: {"name": "Test 1MN Afterburner", "group_id": 46, "category_id": 7,
             "attrs": {A.CPU_USAGE: 15, A.POWER_USAGE: 10, A.SPEED_BONUS: 135, A.CAP_NEED: 8,
                       A.CYCLE_TIME: 10000, A.SPEED_BOOST_FACTOR: 1200000}},
        DRONE: {"name": "Test Warrior", "group_id": 100, "category_id": 18,
                "attrs": {A.EXPLOSIVE_DAMAGE: 5.0, A.DRONE_DAMAGE_MULTIPLIER: 1.0,
                          A.RATE_OF_FIRE: 2000}},
        LAUNCHER: {"name": "Test Rocket Launcher", "group_id": 507, "category_id": 7,
                   "effects": [A.EFFECT_LAUNCHER],
                   "attrs": {A.CPU_USAGE: 4, A.POWER_USAGE: 2, A.RATE_OF_FIRE: 3000}},
        ROCKET: {"name": "Test Rocket", "group_id": 507, "category_id": 8,
                 "skills": [(3319, 1)],   # requires Missile Launcher Operation -> Warhead Upgrades applies
                 "attrs": {A.KINETIC_DAMAGE: 20.0,
                           A.AOE_CLOUD_SIZE: 50.0, A.AOE_VELOCITY: 100.0,
                           A.AOE_DAMAGE_REDUCTION_FACTOR: 0.85,
                           A.AOE_DAMAGE_REDUCTION_SENSITIVITY: 0.70}},
        BCS: {"name": "Test Ballistic Control", "group_id": 367, "category_id": 7,
              "attrs": {A.CPU_USAGE: 18, A.POWER_USAGE: 1, A.DAMAGE_MULTIPLIER: 1.10,
                        A.ROF_MULTIPLIER: 0.90}},
        WEB: {"name": "Test Stasis Webifier", "group_id": 65, "category_id": 7,
              "attrs": {A.CPU_USAGE: 3, A.POWER_USAGE: 1, A.SPEED_BONUS: -60.0,
                        A.OPTIMAL_RANGE: 10000.0, A.FALLOFF: 5000.0}},
        SCRAM: {"name": "Test Warp Scrambler", "group_id": 52, "category_id": 7,
                "attrs": {A.CPU_USAGE: 5, A.POWER_USAGE: 1, A.WARP_SCRAMBLE_STRENGTH: 2.0,
                          A.OPTIMAL_RANGE: 9000.0}},
        NEUT: {"name": "Test Energy Neutralizer", "group_id": 71, "category_id": 7,
               "attrs": {A.CPU_USAGE: 20, A.POWER_USAGE: 5,
                         A.ENERGY_NEUTRALISER_AMOUNT: 48.0, A.CYCLE_TIME: 12000.0,
                         A.OPTIMAL_RANGE: 6000.0}},
        ECM_M: {"name": "Test Multispectral ECM", "group_id": 201, "category_id": 7,
                "attrs": {A.CPU_USAGE: 22, A.POWER_USAGE: 1, A.OPTIMAL_RANGE: 24000.0,
                          A.ECM_STRENGTH["gravimetric"]: 2.0, A.ECM_STRENGTH["radar"]: 3.0,
                          A.ECM_STRENGTH["ladar"]: 2.0, A.ECM_STRENGTH["magnetometric"]: 2.0}},
    }


def _all5() -> SkillProfile:
    return SkillProfile.from_dict({S_MINFRIG: 5, S_GUNNERY: 5, S_SPT: 5, S_SURGICAL: 5,
                                   S_RAPID: 5, S_SHIELDMGMT: 5, S_NAV: 5})


# --------------------------------------------------------------------------- #
# Stacking-penalty maths
# --------------------------------------------------------------------------- #
def test_penalty_table_matches_eve():
    assert [round(stacking.penalty_factor(i), 3) for i in range(6)] == \
        [1.0, 0.869, 0.571, 0.283, 0.106, 0.03]


def test_stacking_is_order_independent():
    assert stacking.combine_penalized([0.7, 0.85, 0.7]) == \
        pytest.approx(stacking.combine_penalized([0.85, 0.7, 0.7]))


def test_three_identical_hardeners():
    # 0.7 * (1-0.3*0.869) * (1-0.3*0.571) = 0.4289 resonance -> 57.1% resist
    assert stacking.combine_penalized([0.7, 0.7, 0.7]) == pytest.approx(0.4289, abs=1e-3)


# --------------------------------------------------------------------------- #
# Adapter helpers
# --------------------------------------------------------------------------- #
def test_slot_from_effects():
    assert slot_from_effects([A.EFFECT_HI_POWER]) == "high"
    assert slot_from_effects([A.EFFECT_RIG_SLOT]) == "rig"
    assert slot_from_effects([999999]) is None


# --------------------------------------------------------------------------- #
# ORM adapter integration (real SDE tables)
# --------------------------------------------------------------------------- #
@pytest.fixture
def orm_dogma(db):
    """Build the fixture through the real SDE tables so the ORM provider is exercised."""
    from apps.admin_audit.models import AppSetting
    from apps.sde.models import (
        SdeCategory,
        SdeGroup,
        SdeShipBonus,
        SdeType,
        SdeTypeAttribute,
        SdeTypeEffect,
        SdeTypeSkill,
    )

    data = _types()
    # Derive the categories/groups the fixture needs straight from the data.
    cats = {meta["category_id"] for meta in data.values()} | {16}
    for cid in cats:
        SdeCategory.objects.get_or_create(category_id=cid, defaults={"name": f"Category {cid}"})
    groups = {(meta["group_id"], meta["category_id"]) for meta in data.values()} | {(349, 16)}
    for gid, cid in groups:
        SdeGroup.objects.get_or_create(group_id=gid, defaults={"category_id": cid, "name": f"Group {gid}"})
    for tid, meta in data.items():
        SdeType.objects.get_or_create(
            type_id=tid, defaults={"group_id": meta["group_id"], "name": meta["name"]})
        SdeTypeAttribute.objects.bulk_create(
            [SdeTypeAttribute(type_id=tid, attribute_id=aid, value=val)
             for aid, val in meta["attrs"].items()], ignore_conflicts=True)
        SdeTypeEffect.objects.bulk_create(
            [SdeTypeEffect(type_id=tid, effect_id=eid) for eid in meta.get("effects", [])],
            ignore_conflicts=True)
        for sid, lvl in meta.get("skills", []):
            SdeType.objects.get_or_create(type_id=sid, defaults={"group_id": 349, "name": f"Skill {sid}"})
            SdeTypeSkill.objects.get_or_create(type_id=tid, skill_type_id=sid, defaults={"level": lvl})
    SdeShipBonus.objects.create(
        ship_type_id=SHIP, key="minfrig_dmg", target_attribute_id=A.DAMAGE_MULTIPLIER,
        amount=5.0, per_level=True, skill_type_id=S_MINFRIG, match_group_ids=[55],
        label="Minmatar Frigate")
    AppSetting.objects.update_or_create(key="dogma_data_version", defaults={"value": {"version": "orm-1"}})
    return True


def test_orm_adapter_wiring(orm_dogma):
    """ORM-provider wiring: the adapter resolves the data version and bridges the hull's
    legacy slot-count columns into the v2 evaluator, even though this fixture stores the
    slots as dogma attributes and deliberately omits the full modifier graph. The bonus
    *numbers* are covered against real graph fixtures by the golden suite
    (tests/test_fitting_golden_*)."""
    engine = FittingEngine(provider=ORMDataProvider())
    assert engine.data_version == "orm-1"
    fit = FitInput(SHIP, (
        ModuleInput(AC, SlotKind.HIGH, ModuleState.ACTIVE, charge_type_id=FUSION),
        ModuleInput(GYRO, SlotKind.LOW, ModuleState.ACTIVE),
    ))
    r = engine.evaluate(fit, _all5(), OperatingProfile())
    # slot-count bridge: hull slots come through even though we stored them as dogma attrs
    assert r.telemetry["resources"]["slots"]["hull"]["high"] == 4


def test_engine_cache_roundtrip(orm_dogma):
    engine = FittingEngine(provider=ORMDataProvider())
    fit = FitInput(SHIP, (ModuleInput(AC, SlotKind.HIGH, ModuleState.ACTIVE, charge_type_id=FUSION),))
    first = engine.evaluate_cached(fit, _all5(), OperatingProfile())
    second = engine.evaluate_cached(fit, _all5(), OperatingProfile())  # served from cache
    assert first == second
    assert first["engine_version"] == engine.engine_version
    assert first["data_version"] == "orm-1"
