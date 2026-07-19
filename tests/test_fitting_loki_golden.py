"""Golden test: a real Loki (T3 strategic cruiser) — subsystem assembly + missiles + drones.

Real CCP SDE values for the DPS Loki at /lab/9/ (pilot ~all-V). Locks the T3 mechanics the
Vagabond didn't exercise: subsystems ADD CPU/PG/slots/structure-HP to the bare hull and
contribute per-subsystem-skill bonuses; missile damage lives on the charge (Heavy Missiles +
Warhead Upgrades) with the BCS bonus on attr 213; an active shield hardener's resist is a
% bonus on attrs 984-987; medium-drone skills scale Valkyrie damage.
"""
from __future__ import annotations

import pytest

from apps.fitting.engine.bonuses import BonusSpec
from apps.fitting.engine.dogma import evaluate
from apps.fitting.engine.memory import MemoryDataProvider
from apps.fitting.engine.types import (
    FitInput,
    ModuleInput,
    ModuleState,
    OperatingProfile,
    SkillProfile,
    SlotKind,
)

LOKI = 29990
SUB_OFF, SUB_CORE, SUB_DEF, SUB_PROP = 45608, 45633, 45595, 45620
HML, NOVA, BCS, DCU, HARDENER, LSE, CDFE, MWD, VALKYRIE = 2521, 24507, 22291, 2046, 2281, 3841, 31796, 12345, 21640
S_HEAVY_MISSILES, S_MISSILE_OP, S_MIN_OFFENSIVE, S_MIN_PROPULSION = 3324, 3319, 30551, 30554
S_MED_DRONE, S_MIN_DRONE_SPEC = 33699, 12485


def _types() -> dict:
    return {
        LOKI: {"name": "Loki", "group_id": 963, "category_id": 6, "attrs": {
            4: 13_800_000, 48: 300, 11: 550, 9: 1000, 263: 2500, 265: 2500,
            14: 0, 13: 0, 12: 0, 1137: 3, 102: 0, 101: 0,
            271: 0.5, 272: 0.5, 273: 0.6, 274: 0.6,      # shield resonance em/exp/kin/th
            267: 0.2, 268: 0.9, 269: 0.75, 270: 0.4875,  # armor resonance
            113: 1.0, 111: 1.0, 109: 1.0, 110: 1.0,      # hull resonance (base)
            482: 1300, 37: 185, 70: 0.54, 552: 160, 564: 240, 192: 6, 600: 4.0}},
        # --- subsystems: stat adds + (offensive/propulsion) bonuses -----------------------
        SUB_OFF: {"name": "Loki Offensive", "group_id": 956, "category_id": 32, "attrs": {
            48: 150.0, 11: 150.0, 1374: 7.0, 1368: 2.0, 1369: 5.0}},
        SUB_CORE: {"name": "Loki Core", "group_id": 958, "category_id": 32, "attrs": {
            1375: 3.0, 1376: 1.0}},
        SUB_DEF: {"name": "Loki Defensive", "group_id": 954, "category_id": 32, "attrs": {
            1374: 1.0, 1375: 2.0, 1376: 1.0, 2688: 1000.0}},
        SUB_PROP: {"name": "Loki Propulsion", "group_id": 957, "category_id": 32, "attrs": {1376: 2.0}},
        # --- weapons / mods ---------------------------------------------------------------
        HML: {"name": "Heavy Missile Launcher II", "group_id": 510, "category_id": 7,
              "effects": [101], "attrs": {51: 12000.0, 50: 27.0}},
        NOVA: {"name": "Nova Fury Heavy Missile", "group_id": 655, "category_id": 8,
               "skills": [(S_HEAVY_MISSILES, 5), (S_MISSILE_OP, 5)], "attrs": {116: 201.0}},
        BCS: {"name": "Ballistic Control System II", "group_id": 367, "category_id": 7,
              "attrs": {213: 1.1, 204: 0.895}},
        DCU: {"name": "Damage Control II", "group_id": 60, "category_id": 7, "effects": [2302],
              "attrs": {271: 0.875, 272: 0.875, 273: 0.875, 274: 0.875,
                        267: 0.85, 268: 0.85, 269: 0.85, 270: 0.85,
                        974: 0.4, 975: 0.4, 976: 0.4, 977: 0.4}},
        HARDENER: {"name": "Multispectrum Shield Hardener II", "group_id": 77, "category_id": 7,
                   "effects": [5230], "attrs": {984: -32.5, 985: -32.5, 986: -32.5, 987: -32.5}},
        LSE: {"name": "Large Shield Extender II", "group_id": 40, "category_id": 7, "attrs": {72: 2600.0}},
        CDFE: {"name": "Core Defense Field Extender II", "group_id": 1336, "category_id": 7, "attrs": {337: 20.0}},
        MWD: {"name": "50MN MWD", "group_id": 46, "category_id": 7,
              "attrs": {20: 505.0, 567: 15_000_000.0, 796: 5_000_000.0}},
        VALKYRIE: {"name": "Valkyrie II", "group_id": 100, "category_id": 18, "effects": [10],
                   "skills": [(S_MED_DRONE, 5), (S_MIN_DRONE_SPEC, 1)],
                   "attrs": {64: 1.56, 116: 32.0, 51: 4000.0}},
    }


def _provider() -> MemoryDataProvider:
    prov = MemoryDataProvider(_types(), data_version="loki-golden")
    # Offensive subsystem: -10% launcher RoF / Minmatar Offensive Systems level.
    prov.add_ship_bonus(SUB_OFF, BonusSpec("loki_off_rof", 51, -10.0, skill_id=S_MIN_OFFENSIVE,
                                           per_level=True, match_effect_id=101, label="Loki launcher RoF"))
    # Propulsion subsystem: +5% velocity, -5% agility / Minmatar Propulsion Systems level.
    prov.add_ship_bonus(SUB_PROP, BonusSpec("loki_prop_vel", 37, 5.0, target_domain="ship",
                                            skill_id=S_MIN_PROPULSION, per_level=True, label="Loki velocity"))
    prov.add_ship_bonus(SUB_PROP, BonusSpec("loki_prop_agi", 70, -5.0, target_domain="ship",
                                            skill_id=S_MIN_PROPULSION, per_level=True, label="Loki agility"))
    return prov


def _fit() -> FitInput:
    A = ModuleState.ACTIVE
    mods = [ModuleInput(HML, SlotKind.HIGH, A, charge_type_id=NOVA) for _ in range(5)]
    mods += [ModuleInput(BCS, SlotKind.LOW, A) for _ in range(3)]
    mods += [ModuleInput(DCU, SlotKind.LOW, A), ModuleInput(HARDENER, SlotKind.MED, A),
             ModuleInput(LSE, SlotKind.MED, A), ModuleInput(LSE, SlotKind.MED, A),
             ModuleInput(MWD, SlotKind.MED, A)]
    mods += [ModuleInput(CDFE, SlotKind.RIG, A) for _ in range(3)]
    mods += [ModuleInput(SUB_OFF, SlotKind.SUBSYSTEM, A), ModuleInput(SUB_CORE, SlotKind.SUBSYSTEM, A),
             ModuleInput(SUB_DEF, SlotKind.SUBSYSTEM, A), ModuleInput(SUB_PROP, SlotKind.SUBSYSTEM, A)]
    mods.append(ModuleInput(VALKYRIE, SlotKind.DRONE, A, quantity=4))
    return FitInput(LOKI, tuple(mods))


@pytest.fixture
def result():
    return evaluate(_fit(), SkillProfile.omniscient(), OperatingProfile(propulsion_active=False), _provider())


def test_subsystems_assemble_cpu_pg_slots_hp(result):
    r = result.telemetry["resources"]
    assert r["cpu"]["output"] == pytest.approx(562.5)        # (300 + 150 offensive) × 1.25
    assert r["powergrid"]["output"] == pytest.approx(875.0)  # (550 + 150) × 1.25
    assert r["slots"]["hull"] == {"high": 8, "med": 5, "low": 4, "rig": 3}
    assert result.telemetry["defence"]["layers"]["hull"]["hp"] == pytest.approx(2500, rel=0.01)  # (1000+1000)×1.25


def test_shield_hp_with_extenders_and_rigs(result):
    # (2500 base + 2×2600 extenders) × Shield Management 1.25 × three 20% rigs (1.2³ = 1.728)
    assert result.telemetry["defence"]["layers"]["shield"]["hp"] == pytest.approx(16632, rel=0.01)


def test_missile_volley_and_dps(result):
    off = result.telemetry["offence"]
    # 5 × Nova 201 × Heavy Missiles 1.25 × Warhead 1.10 × BCS(3×1.1 penalised ≈1.264)
    assert off["missile_dps"] > 0 and off["volley"] == pytest.approx(1746, rel=0.02)


def test_drone_dps_medium(result):
    # 4 × Valkyrie 32 × 1.56 × (DroneInterfacing 1.5 · MedDroneOp 1.25 · MinDroneSpec 1.1) / 4s
    assert result.telemetry["offence"]["drone_dps"] == pytest.approx(103, rel=0.02)


def test_active_shield_hardener_resist(result):
    # base shield em 0.5 × DCU 0.875 × hardener (1 - 0.325) → ~70% resist
    assert result.telemetry["defence"]["layers"]["shield"]["resists"]["em"] == pytest.approx(70.5, abs=2.0)
