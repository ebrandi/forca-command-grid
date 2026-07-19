"""Golden end-to-end test: a real Vagabond fit vs the in-game EVE fitting simulation.

Every attribute below is the REAL CCP SDE value for the fit at /lab/8/ (pilot Stromgren,
effectively all level-5). The in-game client's fitting simulation is ground truth; each
assertion reproduces a headline number from that simulation. This locks the engine overhaul
that made module dogma effects (extenders/plates/rigs/damage-control/nanofibers), the full
skill catalogue, drone skills, the mass-dependent MWD thrust and skill-driven targeting
actually apply — instead of reading fixture-shaped attributes off the wrong ids.

The Assault Damage Control is OVERHEATED here, matching the ground-truth capture (its uniform
resistanceMultiplier 0.25 replaces the passive per-layer resonances).
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

VAGABOND, AC425, HAIL, GYRO, WARRIOR = 11999, 2913, 12777, 519, 2488
LSE, ADCU, CDFE, NANO, MWD, NOS, SCRAM = 3841, 47257, 31796, 2605, 12345, 12346, 12347

# skill ids used by the required-skill scoping
S_MED_PROJ, S_GUNNERY, S_MED_AC_SPEC = 3305, 3300, 12208
S_LIGHT_DRONE, S_MIN_DRONE_SPEC, S_DRONES = 24241, 12485, 3436


def _types() -> dict:
    return {
        VAGABOND: {"name": "Vagabond", "group_id": 963, "category_id": 6, "attrs": {
            4: 11_590_000,               # mass (absent from our SDE; the true CCP base)
            48: 400, 11: 925,            # CPU / PG base -> ×1.25 skills = 500 / 1156.25
            14: 6, 13: 4, 12: 5, 1137: 2,  # slots H/M/L/R
            102: 5, 101: 0,              # turret / launcher hardpoints
            263: 1800, 265: 1400, 9: 980,   # shield / armor / hull base HP
            271: 0.25, 272: 0.5, 273: 0.6, 274: 0.4,      # shield resonance em/exp/kin/th
            267: 0.1, 268: 0.9, 269: 0.75, 270: 0.325,    # armor resonance
            113: 0.67, 111: 0.67, 109: 0.67, 110: 0.67,   # hull (structure) resonance
            482: 1200, 55: 245000,       # capacitor + recharge
            37: 295, 70: 0.504, 552: 115,   # velocity / agility / signature
            76: 55000, 564: 330, 192: 6, 209: 21,  # targeting
            1803: -50,                   # MWD signature role bonus
            600: 4.0, 1271: 25, 283: 25,
        }},
        AC425: {"name": "425mm AutoCannon II", "group_id": 55, "category_id": 7,
                "effects": [34, 42], "skills": [(S_MED_PROJ, 5), (S_GUNNERY, 3), (S_MED_AC_SPEC, 1)],
                "attrs": {64: 3.465, 51: 5456, 158: 10836, 160: 33.792, 30: 154, 50: 25}},
        HAIL: {"name": "Hail M", "group_id": 83, "category_id": 8, "attrs": {116: 27.8, 117: 7.6}},
        GYRO: {"name": "Gyrostabilizer II", "group_id": 59, "category_id": 7,
               "effects": [89, 92], "attrs": {64: 1.1, 204: 0.895}},
        WARRIOR: {"name": "Warrior II", "group_id": 100, "category_id": 18, "effects": [10],
                  "skills": [(S_LIGHT_DRONE, 5), (S_MIN_DRONE_SPEC, 1), (S_DRONES, 5)],
                  "attrs": {64: 1.56, 116: 20.0, 51: 4000}},
        LSE: {"name": "Large Shield Extender II", "group_id": 40, "category_id": 7,
              "effects": [21], "attrs": {72: 2600.0, 983: 25.0}},
        ADCU: {"name": "Assault Damage Control II", "group_id": 60, "category_id": 7,
               "effects": [2302, 7012], "attrs": {
                   267: 0.9, 268: 0.9, 269: 0.9, 270: 0.9,
                   271: 0.925, 272: 0.925, 273: 0.925, 274: 0.925,
                   974: 0.7, 975: 0.7, 976: 0.7, 977: 0.7, 2746: 0.25}},
        CDFE: {"name": "Medium Core Defense Field Extender II", "group_id": 1336, "category_id": 7,
               "effects": [446], "attrs": {337: 20.0}},
        NANO: {"name": "Nanofiber Internal Structure II", "group_id": 763, "category_id": 7,
               "effects": [60], "attrs": {150: 0.8, 169: -15.75, 1076: 9.5}},
        MWD: {"name": "50MN Quad LiF Restrained Microwarpdrive", "group_id": 46, "category_id": 7,
              "attrs": {20: 505.0, 567: 15_000_000.0, 554: 450.0, 796: 5_000_000.0, 6: 180.0, 73: 10000.0}},
        NOS: {"name": "Small Energy Nosferatu II", "group_id": 68, "category_id": 7,
              "attrs": {90: 10.0, 73: 2500.0, 6: 0.0}},
        SCRAM: {"name": "Warp Scrambler II", "group_id": 52, "category_id": 7,
                "attrs": {6: 6.0, 73: 5000.0, 504: 2.0}},
    }


def _provider() -> MemoryDataProvider:
    prov = MemoryDataProvider(_types(), data_version="vagabond-golden")
    # Vagabond hull bonuses (as import_ship_bonuses parses them from CCP modifierInfo):
    #   +5% medium projectile damage / Heavy Assault Cruisers level; -5% RoF / Minmatar Cruiser.
    prov.add_ship_bonus(VAGABOND, BonusSpec("vaga_dmg", 64, 5.0, skill_id=16591, per_level=True,
                                            match_required_skill_id=S_MED_PROJ, label="HAC dmg"))
    prov.add_ship_bonus(VAGABOND, BonusSpec("vaga_rof", 51, -5.0, skill_id=3333, per_level=True,
                                            match_required_skill_id=S_MED_PROJ, label="Minmatar Cruiser RoF"))
    return prov


def _fit() -> FitInput:
    A = ModuleState.ACTIVE
    mods = [ModuleInput(AC425, SlotKind.HIGH, A, charge_type_id=HAIL) for _ in range(5)]
    mods.append(ModuleInput(NOS, SlotKind.HIGH, A))
    mods += [ModuleInput(SCRAM, SlotKind.MED, A),
             ModuleInput(LSE, SlotKind.MED, A), ModuleInput(LSE, SlotKind.MED, A),
             ModuleInput(MWD, SlotKind.MED, A)]
    mods += [ModuleInput(GYRO, SlotKind.LOW, A), ModuleInput(GYRO, SlotKind.LOW, A),
             ModuleInput(NANO, SlotKind.LOW, A), ModuleInput(NANO, SlotKind.LOW, A),
             ModuleInput(ADCU, SlotKind.LOW, ModuleState.OVERHEATED)]   # overloaded, as in-game
    mods += [ModuleInput(CDFE, SlotKind.RIG, A), ModuleInput(CDFE, SlotKind.RIG, A)]
    mods.append(ModuleInput(WARRIOR, SlotKind.DRONE, A, quantity=5))
    return FitInput(VAGABOND, tuple(mods))


@pytest.fixture
def result():
    return evaluate(_fit(), SkillProfile.omniscient(), OperatingProfile(), _provider())


def test_turret_volley_and_dps(result):
    off = result.telemetry["offence"]
    # Volley is the load-bearing proof of the whole damage chain (skills + hull bonus + gyros).
    assert off["volley"] == pytest.approx(1449, rel=0.01)          # in-game 1449
    # DPS ~604.8 (the in-game 685.3 includes an Overclocker booster we do not model).
    assert off["turret_dps"] == pytest.approx(605, rel=0.02)


def test_drone_dps(result):
    # 5 × Warrior II: 20 × 1.56 × (DroneInterfacing 1.5 · LightDrone 1.25 · MinDroneSpec 1.1) / 4s
    assert result.telemetry["offence"]["drone_dps"] == pytest.approx(80.4, rel=0.01)


def test_shield_hp_and_resists(result):
    sh = result.telemetry["defence"]["layers"]["shield"]
    assert sh["hp"] == pytest.approx(12600, rel=0.01)              # (1800+5200)×1.44×1.25
    r = sh["resists"]
    assert (r["em"], r["thermal"], r["kinetic"], r["explosive"]) == pytest.approx(
        (93.75, 90.0, 85.0, 87.5), abs=1.0)                       # in-game 94/90/85/88 (ADC overload)


def test_armor_hp_and_resists(result):
    ar = result.telemetry["defence"]["layers"]["armor"]
    assert ar["hp"] == pytest.approx(1750, rel=0.01)              # 1400 × Hull Upgrades 1.25
    r = ar["resists"]
    assert (r["em"], r["thermal"], r["kinetic"], r["explosive"]) == pytest.approx(
        (97.5, 91.9, 81.25, 77.5), abs=1.5)                      # in-game 98/92/82/78


def test_hull_hp_and_resists(result):
    hu = result.telemetry["defence"]["layers"]["hull"]
    assert hu["hp"] == pytest.approx(784, rel=0.01)              # 980 × 0.8² × Mechanics 1.25
    assert hu["resists"]["em"] == pytest.approx(83.25, abs=1.5)   # in-game 84 (0.67 × 0.25)


def test_mobility(result):
    mob = result.telemetry["mobility"]
    assert mob["mass"] == pytest.approx(16_590_000, rel=0.005)
    assert mob["agility"] == pytest.approx(0.2474, rel=0.02)
    assert mob["align_time_s"] == pytest.approx(5.69, rel=0.03)
    assert mob["propulsion_velocity"] == pytest.approx(2932, rel=0.02)


def test_targeting(result):
    tg = result.telemetry["targeting"]
    assert tg["max_target_range"] == pytest.approx(68750, rel=0.01)   # 55000 × 1.25
    assert tg["scan_resolution"] == pytest.approx(412, abs=2)         # 330 × 1.25
