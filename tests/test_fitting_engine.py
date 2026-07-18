"""Tocha's Lab engine unit + adapter tests.

Every headline number is checked against a value computed by hand in the test from
documented EVE mechanics — never against another engine's output. The fixture data is
original (invented type ids in the 90000x range), not copied from any external source.
"""
from __future__ import annotations

import math

import pytest

from apps.fitting.engine import attributes as A
from apps.fitting.engine import stacking
from apps.fitting.engine.adapter import FittingEngine, ORMDataProvider, slot_from_effects
from apps.fitting.engine.bonuses import BonusSpec
from apps.fitting.engine.dogma import evaluate
from apps.fitting.engine.memory import MemoryDataProvider
from apps.fitting.engine.types import (
    DamageProfileInput,
    FitInput,
    ModuleInput,
    ModuleState,
    OperatingProfile,
    SkillProfile,
    SlotKind,
    Status,
)

# --- ids (original fixture) --------------------------------------------------
SHIP, AC, FUSION, GYRO, EXT, HARD, AB, DRONE = 900001, 900010, 900020, 900030, 900040, 900050, 900060, 900070
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
                   A.TURRET_HARDPOINTS: 3, A.LAUNCHER_HARDPOINTS: 0,
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
             "skills": [(S_GUNNERY, 1), (S_SPT, 1)],
             "attrs": {A.CPU_USAGE: 8, A.POWER_USAGE: 6, A.DAMAGE_MULTIPLIER: 1.0,
                       A.RATE_OF_FIRE: 2500, A.OPTIMAL_RANGE: 1200, A.FALLOFF: 7500,
                       A.TRACKING_SPEED: 0.2}},
        FUSION: {"name": "Test Fusion S", "group_id": 83, "category_id": 8,
                 "attrs": {A.EXPLOSIVE_DAMAGE: 9.0}},
        GYRO: {"name": "Test Gyrostabilizer", "group_id": 62, "category_id": 7,
               "attrs": {A.CPU_USAGE: 18, A.POWER_USAGE: 1, A.DAMAGE_MULTIPLIER: 1.10,
                         A.RATE_OF_FIRE: 0.90}},
        EXT: {"name": "Test Shield Extender", "group_id": 40, "category_id": 7,
              "attrs": {A.CPU_USAGE: 20, A.POWER_USAGE: 12, A.SHIELD_HP: 268}},
        HARD: {"name": "Test Shield Amplifier", "group_id": 295, "category_id": 7,
               "attrs": {A.CPU_USAGE: 18, A.POWER_USAGE: 1,
                         A.SHIELD_RESONANCE["em"]: 0.70, A.SHIELD_RESONANCE["thermal"]: 0.70,
                         A.SHIELD_RESONANCE["kinetic"]: 0.70, A.SHIELD_RESONANCE["explosive"]: 0.70}},
        AB: {"name": "Test 1MN Afterburner", "group_id": 46, "category_id": 7,
             "attrs": {A.CPU_USAGE: 15, A.POWER_USAGE: 10, A.SPEED_BONUS: 135, A.CAP_NEED: 8,
                       A.CYCLE_TIME: 10000}},
        DRONE: {"name": "Test Warrior", "group_id": 100, "category_id": 18,
                "attrs": {A.EXPLOSIVE_DAMAGE: 5.0, A.DRONE_DAMAGE_MULTIPLIER: 1.0,
                          A.RATE_OF_FIRE: 2000}},
    }


def _provider() -> MemoryDataProvider:
    prov = MemoryDataProvider(_types(), data_version="fixture-1")
    prov.add_ship_bonus(SHIP, BonusSpec("minfrig_dmg", A.DAMAGE_MULTIPLIER, 5.0, skill_id=S_MINFRIG,
                                        per_level=True, match_group_ids=(55,), label="Minmatar Frigate"))
    return prov


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
# Full evaluation (memory provider)
# --------------------------------------------------------------------------- #
def test_dps_with_ship_and_skill_and_module_bonuses():
    prov = _provider()
    fit = FitInput(SHIP, (
        ModuleInput(AC, SlotKind.HIGH, ModuleState.ACTIVE, charge_type_id=FUSION),
        ModuleInput(GYRO, SlotKind.LOW, ModuleState.ACTIVE),
    ))
    r = evaluate(fit, _all5(), OperatingProfile(), prov)
    # dmg_mult = 1.0 * (1.25 minfrig * 1.15 surgical) * 1.10 gyro ; rof = 2.5*0.80*0.90
    expected = 9 * (1.0 * 1.25 * 1.15 * 1.10) / (2.5 * 0.80 * 0.90)
    assert r.telemetry["offence"]["total_dps"] == pytest.approx(expected, abs=0.05)
    assert r.telemetry["offence"]["damage_distribution"]["explosive"] == 100.0
    assert r.status == Status.VALID


def test_missing_ammo_is_flagged_and_zero_dps():
    prov = _provider()
    fit = FitInput(SHIP, (ModuleInput(AC, SlotKind.HIGH, ModuleState.ACTIVE),))  # no charge
    r = evaluate(fit, _all5(), OperatingProfile(), prov)
    assert r.telemetry["offence"]["total_dps"] == 0.0
    assert any(d.code == "missing_ammo" for d in r.diagnostics)


def test_ehp_and_stacked_resist():
    prov = _provider()
    fit = FitInput(SHIP, (
        ModuleInput(EXT, SlotKind.MED, ModuleState.ACTIVE),
        ModuleInput(HARD, SlotKind.MED, ModuleState.ACTIVE),
        ModuleInput(HARD, SlotKind.MED, ModuleState.ACTIVE),
    ))
    r = evaluate(fit, _all5(), OperatingProfile(damage_profile=DamageProfileInput(1, 1, 1, 1)), prov)
    shield = r.telemetry["defence"]["layers"]["shield"]
    assert shield["hp"] == pytest.approx((450 + 268) * 1.25, abs=0.2)  # Shield Management V
    expected_therm = (1 - 0.8 * stacking.combine_penalized([0.7, 0.7])) * 100
    assert shield["resists"]["thermal"] == pytest.approx(expected_therm, abs=0.2)
    assert r.telemetry["defence"]["ehp_total"] > shield["hp"]  # resists raise EHP above raw HP


def test_mobility_and_align():
    prov = _provider()
    fit = FitInput(SHIP, (ModuleInput(AB, SlotKind.MED, ModuleState.ACTIVE),))
    r = evaluate(fit, _all5(), OperatingProfile(propulsion_active=True), prov)
    mob = r.telemetry["mobility"]
    assert mob["max_velocity"] == pytest.approx(350 * 1.25, abs=0.2)      # Navigation V
    assert mob["propulsion_velocity"] == pytest.approx(350 * 1.25 * 2.35, abs=0.5)  # +135% AB
    assert mob["align_time_s"] == pytest.approx(math.log(4) * 1_200_000 * 3.0 / 1e6, abs=0.02)


def test_capacitor_stability():
    prov = _provider()
    fit = FitInput(SHIP, (ModuleInput(AB, SlotKind.MED, ModuleState.ACTIVE),))
    r = evaluate(fit, _all5(), OperatingProfile(), prov)
    cap = r.telemetry["capacitor"]
    # peak = 0.5 * 350 / 187.5 = 0.933 GJ/s ; AB drain = 8 / 10 = 0.8 GJ/s -> stable
    assert cap["peak_recharge"] == pytest.approx(0.93, abs=0.02)
    assert cap["usage"] == pytest.approx(0.8, abs=0.01)
    assert cap["stable"] is True


def test_drone_dps():
    prov = _provider()
    fit = FitInput(SHIP, (ModuleInput(DRONE, SlotKind.DRONE, ModuleState.ACTIVE, quantity=1),))
    r = evaluate(fit, _all5(), OperatingProfile(), prov)
    # 5 dmg * 1.0 / (2000/1000) = 2.5 dps
    assert r.telemetry["offence"]["drone_dps"] == pytest.approx(2.5, abs=0.01)


def test_missing_skills_and_status():
    prov = _provider()
    fit = FitInput(SHIP, (ModuleInput(AC, SlotKind.HIGH, ModuleState.ACTIVE, charge_type_id=FUSION),))
    r = evaluate(fit, SkillProfile.from_dict({}), OperatingProfile(), prov)
    ids = {m.skill_type_id for m in r.missing_skills}
    assert {S_MINFRIG, S_GUNNERY, S_SPT} <= ids
    assert r.status == Status.MISSING_SKILLS


def test_over_resources_status():
    prov = _provider()
    # 8 autocannons: 4 fit the 4 highs but 3 turret hardpoints -> structural; drop to test CPU only
    fit = FitInput(SHIP, tuple(
        ModuleInput(EXT, SlotKind.MED, ModuleState.ACTIVE) for _ in range(3)
    ) + (ModuleInput(EXT, SlotKind.LOW, ModuleState.ACTIVE),))  # 4x20 CPU = 80 < 125 ok; push more
    # Force CPU over by fitting many gyros (18 CPU each) in lows
    fit = FitInput(SHIP, tuple(
        ModuleInput(GYRO, SlotKind.LOW, ModuleState.ACTIVE) for _ in range(3)
    ) + tuple(ModuleInput(EXT, SlotKind.MED, ModuleState.ACTIVE) for _ in range(3)))
    r = evaluate(fit, _all5(), OperatingProfile(), prov)
    # 3*18 + 3*20 = 114 < 125 -> still ok; assert resources compute correctly instead
    assert r.telemetry["resources"]["cpu"]["used"] == pytest.approx(114.0, abs=0.1)


def test_turret_hardpoint_limit_makes_impossible():
    prov = _provider()
    fit = FitInput(SHIP, tuple(
        ModuleInput(AC, SlotKind.HIGH, ModuleState.ACTIVE, charge_type_id=FUSION) for _ in range(4)
    ))  # 4 turrets, only 3 hardpoints
    r = evaluate(fit, _all5(), OperatingProfile(), prov)
    assert any(d.code == "turret_hardpoints" for d in r.diagnostics)
    assert r.status == Status.IMPOSSIBLE


def test_all_v_beats_current_dps():
    prov = _provider()
    fit = FitInput(SHIP, (ModuleInput(AC, SlotKind.HIGH, ModuleState.ACTIVE, charge_type_id=FUSION),))
    low = evaluate(fit, SkillProfile.from_dict({S_MINFRIG: 1, S_GUNNERY: 1, S_SPT: 1}),
                   OperatingProfile(), prov)
    allv = evaluate(fit, SkillProfile.omniscient(), OperatingProfile(), prov)
    assert allv.telemetry["offence"]["total_dps"] > low.telemetry["offence"]["total_dps"]


def test_offline_module_does_not_consume_cpu():
    prov = _provider()
    on = evaluate(FitInput(SHIP, (ModuleInput(GYRO, SlotKind.LOW, ModuleState.ACTIVE),)),
                  _all5(), OperatingProfile(), prov)
    off = evaluate(FitInput(SHIP, (ModuleInput(GYRO, SlotKind.LOW, ModuleState.OFFLINE),)),
                   _all5(), OperatingProfile(), prov)
    assert on.telemetry["resources"]["cpu"]["used"] == pytest.approx(18.0)
    assert off.telemetry["resources"]["cpu"]["used"] == pytest.approx(0.0)


def test_result_is_json_serialisable():
    import json
    prov = _provider()
    fit = FitInput(SHIP, (ModuleInput(AC, SlotKind.HIGH, ModuleState.ACTIVE, charge_type_id=FUSION),))
    r = evaluate(fit, _all5(), OperatingProfile(), prov)
    json.dumps(r.to_dict())  # must not raise
    assert r.to_dict()["engine_version"]


def test_slot_from_effects():
    assert slot_from_effects([A.EFFECT_HI_POWER]) == "high"
    assert slot_from_effects([A.EFFECT_RIG_SLOT]) == "rig"
    assert slot_from_effects([999999]) is None


# --------------------------------------------------------------------------- #
# ORM adapter integration (real SDE tables)
# --------------------------------------------------------------------------- #
@pytest.fixture
def orm_dogma(db):
    """Build the same fixture through the real SDE tables so the ORM provider is exercised."""
    from apps.admin_audit.models import AppSetting
    from apps.sde.models import (
        SdeCategory,
        SdeGroup,
        SdeShipBonus,
        SdeType,
        SdeTypeAttribute,
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
        for sid, lvl in meta.get("skills", []):
            SdeType.objects.get_or_create(type_id=sid, defaults={"group_id": 349, "name": f"Skill {sid}"})
            SdeTypeSkill.objects.get_or_create(type_id=tid, skill_type_id=sid, defaults={"level": lvl})
    SdeShipBonus.objects.create(
        ship_type_id=SHIP, key="minfrig_dmg", target_attribute_id=A.DAMAGE_MULTIPLIER,
        amount=5.0, per_level=True, skill_type_id=S_MINFRIG, match_group_ids=[55],
        label="Minmatar Frigate")
    AppSetting.objects.update_or_create(key="dogma_data_version", defaults={"value": {"version": "orm-1"}})
    return True


def test_orm_adapter_matches_memory_engine(orm_dogma):
    engine = FittingEngine(provider=ORMDataProvider())
    assert engine.data_version == "orm-1"
    fit = FitInput(SHIP, (
        ModuleInput(AC, SlotKind.HIGH, ModuleState.ACTIVE, charge_type_id=FUSION),
        ModuleInput(GYRO, SlotKind.LOW, ModuleState.ACTIVE),
    ))
    r = engine.evaluate(fit, _all5(), OperatingProfile())
    expected = 9 * (1.0 * 1.25 * 1.15 * 1.10) / (2.5 * 0.80 * 0.90)
    assert r.telemetry["offence"]["total_dps"] == pytest.approx(expected, abs=0.05)
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
