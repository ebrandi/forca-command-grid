"""Golden fits: capacitor + mobility + targeting (engine v2, real SDE slices).

Domain matrix for the golden-fit programme: a Stabber hull exercised through the
capacitor model (stability quadratic, runtime, rechargers/CCC, battery, booster
injection), the mobility model (AB/MWD thrust formula, mass additions, signature
bloom, nanofiber/overdrive/istab stacking, align time, warp speed rigs) and the
targeting model (sensor boosters + script charges, signal amplifiers, max locked
targets, sensor strength).

Every expected value is DERIVED IN THE TEST from the fixture slice's base
attributes (tests/fixtures/fitting/stabber_capmob.json / stabber_sensors.json,
extracted from a live CCP SDE import) plus documented EVE mechanics:

* stacking penalty S(i) = exp(-(i/2.67)^2), applied per (target attribute,
  operator) bucket, positive and negative chains separately, sorted by
  |magnitude|, ONLY when the target attribute is stackable=false and the source
  is a module/rig (ship, charge, skill, implant, subsystem sources are exempt);
* capacitor peak recharge = 2.5*C/tau at 25% charge (recharge ODE
  dC/dt = (10*C/tau)*(sqrt(x) - x));
* capacitor equilibrium: with constant net drain d, s(1-s) = d*tau/(10*C) with
  s = sqrt(x); stable fraction x = s^2 taking the larger root;
* prop-mod thrust: v = v_base * (1 + (speedFactor/100) * (thrust/mass)) with the
  prop mod's massAddition folded into ship mass while it runs;
* align time = -ln(0.25) * mass * agility / 1e6.

Skill percentages (used only in the omniscient tests) are the skill's own dogma
values read from the live DB, cited per constant below. Nothing is ever read
back from the engine to build an expectation.
"""
from __future__ import annotations

import math

import pytest

from apps.fitting.engine import attributes as A
from apps.fitting.engine.types import (
    ModuleInput,
    ModuleState,
    OperatingProfile,
    SkillProfile,
    SlotKind,
)

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db

S1 = math.exp(-((1 / 2.67) ** 2))   # second stacked module effectiveness (0.8691...)
LN4 = -math.log(0.25)               # align-time constant

# --- Skill multipliers at level V; each value read from the skill's own dogma
# --- attribute in the live SDE (attribute id and per-level value cited inline).
NAVIGATION_V = 1.25        # Navigation 3449: attr 315 velocityBonus = +5%/lvl
EVASIVE_MANEUVERING_V = 0.75  # Evasive Maneuvering 3453: attr 151 agilityBonus = -5%/lvl
SPACESHIP_COMMAND_V = 0.90    # Spaceship Command 3327: attr 151 agilityBonus = -2%/lvl
CAP_MANAGEMENT_V = 1.25    # Capacitor Management 3418: attr 1079 = +5%/lvl capacity
CAP_SYSTEMS_OP_V = 0.75    # Capacitor Systems Operation 3417: attr 314 = -5%/lvl recharge
AFTERBURNER_DURATION_V = 0.75  # Afterburner 3450: attr 66 durationBonus = -5%/lvl
AFTERBURNER_CAP_V = 0.50   # Afterburner 3450: attr 317 capNeedBonus = -10%/lvl
FUEL_CONSERVATION_V = 0.50  # Fuel Conservation 3451: attr 317 capNeedBonus = -10%/lvl
ACCELERATION_CONTROL_V = 1.25  # Acceleration Control 3452: attr 318 = +5%/lvl speedFactor
LONG_RANGE_TARGETING_V = 1.25  # Long Range Targeting 3428: attr 309 = +5%/lvl range
SIGNATURE_ANALYSIS_V = 1.25    # Signature Analysis 3431: attr 566 = +5%/lvl scan res
LADAR_COMPENSATION_V = 1.20    # Ladar Sensor Compensation 33001: attr 1851 = +4%/lvl

# Module-local dogma attribute ids not named in apps.fitting.engine.attributes.
ATTR_CAP_RECHARGE_MULT = 144   # capacitorRechargeRateMultiplier (Cap Recharger)
ATTR_CAP_RECHARGE_PCT = 314    # capRechargeBonus (CCC rig, postPercent)
ATTR_CAP_BONUS = 67            # capacitorBonus (battery flat add / booster charge)
ATTR_CAP_CAPACITY_MULT = 147   # capacitorCapacityMultiplier (MWD fitting penalty)
ATTR_DURATION = 73
ATTR_RELOAD_TIME = 1795
ATTR_WARP_SPEED_PCT = 624      # WarpSBonus (hyperspatial rig, postPercent)
ATTR_RIG_DRAWBACK = 1138       # drawback (navigation rig: +% signature radius)
ATTR_SCAN_RES_PCT = 566        # scanResolutionBonus (sensor booster / signal amp)
ATTR_TARGET_RANGE_PCT = 309    # maxTargetRangeBonus (sensor booster / signal amp)
ATTR_LADAR_PCT = 1028          # scanLadarStrengthPercent (sensor booster / signal amp)
ATTR_MAX_TARGETS_ADD = 235     # maxLockedTargetsBonus (signal amp / auto targeter)
ATTR_LADAR_STRENGTH = A.SENSOR_STRENGTHS["ladar"]  # 209 (Stabber is a ladar hull)


@pytest.fixture()
def capmob():
    return load_graph_fixture("stabber_capmob")


@pytest.fixture()
def sensors():
    return load_graph_fixture("stabber_sensors")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _cap_peak(capacity, tau_s):
    """Peak recharge of the EVE capacitor ODE: 2.5*C/tau, reached at 25% charge."""
    return 2.5 * capacity / tau_s


def _stable_pct(capacity, tau_s, drain):
    """Equilibrium of dC/dt = (10*C/tau)*(sqrt(x)-x) - d: with s = sqrt(x),
    s*(1-s) = d*tau/(10*C); the fit settles at the larger root, x = s^2."""
    k = drain * tau_s / (10.0 * capacity)
    s = (1.0 + math.sqrt(1.0 - 4.0 * k)) / 2.0
    return s * s * 100.0


def _align_s(mass, agility):
    return LN4 * mass * agility / 1e6


# --------------------------------------------------------------------------- #
# 1. Bare hull (isolation): base capacitor / mobility / targeting readouts
# --------------------------------------------------------------------------- #
def test_bare_hull_no_skills(capmob):
    stabber = capmob["Stabber"]
    res = evaluate_fit(stabber, [], skills=SkillProfile.from_dict({}))
    t = res.telemetry

    capacity = _attr(stabber, A.CAP_CAPACITY)             # 1200 GJ
    tau = _attr(stabber, A.CAP_RECHARGE_RATE) / 1000.0    # 427.5 s
    assert t["capacitor"]["capacity"] == pytest.approx(capacity, rel=2e-3)
    assert t["capacitor"]["recharge_s"] == pytest.approx(tau, rel=2e-3)
    assert t["capacitor"]["peak_recharge"] == pytest.approx(
        _cap_peak(capacity, tau), rel=2e-3)
    assert t["capacitor"]["stable"] is True
    assert t["capacitor"]["stable_pct"] == 100.0          # zero drain

    mass = _attr(stabber, A.MASS)                         # 11,400,000 kg
    agility = _attr(stabber, A.AGILITY)                   # 0.5
    assert t["mobility"]["max_velocity"] == pytest.approx(
        _attr(stabber, A.MAX_VELOCITY), rel=2e-3)
    assert t["mobility"]["align_time_s"] == pytest.approx(
        _align_s(mass, agility), rel=2e-3)
    assert t["mobility"]["signature_radius"] == pytest.approx(
        _attr(stabber, A.SIGNATURE_RADIUS), rel=2e-3)
    assert t["mobility"]["warp_speed"] == pytest.approx(
        _attr(stabber, A.WARP_SPEED_MULT), rel=2e-3)

    assert t["targeting"]["max_target_range"] == pytest.approx(
        _attr(stabber, A.MAX_TARGET_RANGE), rel=2e-3)
    assert t["targeting"]["scan_resolution"] == pytest.approx(
        _attr(stabber, A.SCAN_RESOLUTION), rel=2e-3)
    assert t["targeting"]["max_locked_targets"] == int(
        _attr(stabber, A.MAX_LOCKED_TARGETS))             # exactly 5
    assert t["targeting"]["sensor_strength"] == pytest.approx(
        _attr(stabber, ATTR_LADAR_STRENGTH), abs=0.06)    # 13, ladar
    assert t["targeting"]["sensor_type"] == "ladar"

    # Slot layout is exact (Stabber: 6/4/4, 3 rigs).
    hull = t["resources"]["slots"]["hull"]
    assert (hull["high"], hull["med"], hull["low"], hull["rig"]) == (6, 4, 4, 3)


# --------------------------------------------------------------------------- #
# 2. Bare hull, All V: pure skill layer on cap / mobility
# --------------------------------------------------------------------------- #
def test_bare_hull_omniscient_skills(capmob):
    stabber = capmob["Stabber"]
    res = evaluate_fit(stabber, [])                        # omniscient default
    t = res.telemetry
    assert res.status.value in ("valid", "warnings")

    capacity = _attr(stabber, A.CAP_CAPACITY) * CAP_MANAGEMENT_V
    tau = _attr(stabber, A.CAP_RECHARGE_RATE) / 1000.0 * CAP_SYSTEMS_OP_V
    assert t["capacitor"]["capacity"] == pytest.approx(capacity, rel=2e-3)
    assert t["capacitor"]["recharge_s"] == pytest.approx(tau, rel=2e-3)
    assert t["capacitor"]["peak_recharge"] == pytest.approx(
        _cap_peak(capacity, tau), rel=2e-3)

    # Stabber's hull trait (shipBonusMC/MC2) only touches projectile turrets, so
    # velocity at All V is Navigation alone; agility takes both agility skills
    # (skill sources are stacking-exempt, so they multiply plainly).
    assert t["mobility"]["max_velocity"] == pytest.approx(
        _attr(stabber, A.MAX_VELOCITY) * NAVIGATION_V, rel=2e-3)
    agility = _attr(stabber, A.AGILITY) * EVASIVE_MANEUVERING_V * SPACESHIP_COMMAND_V
    assert t["mobility"]["align_time_s"] == pytest.approx(
        _align_s(_attr(stabber, A.MASS), agility), rel=2e-3)


# --------------------------------------------------------------------------- #
# 3. Stabber + AB running, All V: cap stability quadratic hand-solved
# --------------------------------------------------------------------------- #
def test_ab_running_cap_stability_quadratic_omniscient(capmob):
    stabber, ab = capmob["Stabber"], capmob["10MN Afterburner II"]
    res = evaluate_fit(stabber, [ModuleInput(type_id=ab, slot=SlotKind.MED,
                                             state=ModuleState.ACTIVE)],
                       op=OperatingProfile(propulsion_active=True))
    cap = res.telemetry["capacitor"]

    capacity = _attr(stabber, A.CAP_CAPACITY) * CAP_MANAGEMENT_V       # 1500
    tau = _attr(stabber, A.CAP_RECHARGE_RATE) / 1000.0 * CAP_SYSTEMS_OP_V  # 320.625
    # AB cycle: 10 s * Afterburner skill (-5%/lvl); AB cap need: 90 GJ * Afterburner
    # (-10%/lvl) * Fuel Conservation (-10%/lvl) — both keyed on required skill 3450.
    cycle_s = _attr(ab, ATTR_DURATION) / 1000.0 * AFTERBURNER_DURATION_V
    need = _attr(ab, A.CAP_NEED) * AFTERBURNER_CAP_V * FUEL_CONSERVATION_V
    drain = need / cycle_s                                             # 3.0 GJ/s

    assert cap["usage"] == pytest.approx(drain, rel=2e-3)
    peak = _cap_peak(capacity, tau)
    assert cap["peak_recharge"] == pytest.approx(peak, rel=2e-3)
    assert drain < peak
    assert cap["stable"] is True
    # Quadratic: s(1-s) = d*tau/(10C) -> s = (1+sqrt(1-4k))/2, stable % = s^2.
    assert cap["stable_pct"] == pytest.approx(
        _stable_pct(capacity, tau, drain), abs=0.06)      # 86.70 %

    # Mobility while it runs: +5M kg massAddition, thrust formula with the
    # Acceleration Control (+5%/lvl) boosted speedFactor and Navigation base.
    mob = res.telemetry["mobility"]
    mass = _attr(stabber, A.MASS) + _attr(ab, A.MASS_ADDITION)         # 16.4M kg
    base_v = _attr(stabber, A.MAX_VELOCITY) * NAVIGATION_V
    sf = _attr(ab, A.SPEED_BONUS) * ACCELERATION_CONTROL_V             # 168.75 %
    thrust = _attr(ab, A.SPEED_BOOST_FACTOR)                           # 1.5e7
    assert mob["propulsion_velocity"] == pytest.approx(
        base_v * (1.0 + (sf / 100.0) * (thrust / mass)), rel=2e-3)     # ~922 m/s
    agility = _attr(stabber, A.AGILITY) * EVASIVE_MANEUVERING_V * SPACESHIP_COMMAND_V
    assert mob["align_time_s"] == pytest.approx(_align_s(mass, agility), rel=2e-3)
    # AB has no signatureRadiusBonus: sig stays at hull value.
    assert mob["signature_radius"] == pytest.approx(
        _attr(stabber, A.SIGNATURE_RADIUS), rel=2e-3)


# --------------------------------------------------------------------------- #
# 4. AB vs MWD (no skills): thrust formula, sig bloom, MWD cap penalty
# --------------------------------------------------------------------------- #
def test_ab_vs_mwd_no_skills(capmob):
    stabber = capmob["Stabber"]
    ab, mwd = capmob["10MN Afterburner II"], capmob["50MN Microwarpdrive II"]
    prop_on = OperatingProfile(propulsion_active=True)
    no_skills = SkillProfile.from_dict({})

    res_ab = evaluate_fit(stabber, [ModuleInput(type_id=ab, slot=SlotKind.MED)],
                          skills=no_skills, op=prop_on)
    res_mwd = evaluate_fit(stabber, [ModuleInput(type_id=mwd, slot=SlotKind.MED)],
                           skills=no_skills, op=prop_on)

    base_v = _attr(stabber, A.MAX_VELOCITY)
    ship_mass = _attr(stabber, A.MASS)
    thrust = _attr(mwd, A.SPEED_BOOST_FACTOR)              # 1.5e7 (both mods)

    # AB: speedFactor 135%, +5M kg mass, no signature penalty.
    m_ab = ship_mass + _attr(ab, A.MASS_ADDITION)
    v_ab = base_v * (1.0 + (_attr(ab, A.SPEED_BONUS) / 100.0) * (thrust / m_ab))
    assert res_ab.telemetry["mobility"]["propulsion_velocity"] == pytest.approx(
        v_ab, rel=2e-3)                                    # ~648 m/s
    assert res_ab.telemetry["mobility"]["signature_radius"] == pytest.approx(
        _attr(stabber, A.SIGNATURE_RADIUS), rel=2e-3)
    assert res_ab.telemetry["capacitor"]["capacity"] == pytest.approx(
        _attr(stabber, A.CAP_CAPACITY), rel=2e-3)          # AB: no cap penalty

    # MWD: speedFactor 510%, +5M kg, +475% sig bloom, x0.8 cap capacity while
    # fitted (capacitorCapacityMultiplier 147, online effect 58, postMul).
    m_mwd = ship_mass + _attr(mwd, A.MASS_ADDITION)
    v_mwd = base_v * (1.0 + (_attr(mwd, A.SPEED_BONUS) / 100.0) * (thrust / m_mwd))
    mob = res_mwd.telemetry["mobility"]
    assert mob["propulsion_velocity"] == pytest.approx(v_mwd, rel=2e-3)  # ~1643 m/s
    assert mob["mass"] == pytest.approx(m_mwd, rel=2e-3)
    sig = _attr(stabber, A.SIGNATURE_RADIUS) * (
        1.0 + _attr(mwd, A.SIGNATURE_RADIUS_BONUS) / 100.0)
    assert mob["signature_radius"] == pytest.approx(sig, rel=2e-3)       # 575 m
    cap = res_mwd.telemetry["capacitor"]
    capacity = _attr(stabber, A.CAP_CAPACITY) * _attr(mwd, ATTR_CAP_CAPACITY_MULT)
    assert cap["capacity"] == pytest.approx(capacity, rel=2e-3)          # 960 GJ
    assert cap["peak_recharge"] == pytest.approx(
        _cap_peak(capacity, _attr(stabber, A.CAP_RECHARGE_RATE) / 1000.0), rel=2e-3)

    assert v_mwd > v_ab
    assert mob["propulsion_velocity"] > \
        res_ab.telemetry["mobility"]["propulsion_velocity"]


# --------------------------------------------------------------------------- #
# 5. Cap-unstable runtime: more drain -> strictly shorter runtime
# --------------------------------------------------------------------------- #
def test_mwd_unstable_runtime_more_drain_shorter(capmob):
    stabber = capmob["Stabber"]
    mwd, neut = capmob["50MN Microwarpdrive II"], capmob["Medium Energy Neutralizer II"]
    no_skills = SkillProfile.from_dict({})

    res_a = evaluate_fit(stabber, [ModuleInput(type_id=mwd, slot=SlotKind.MED)],
                         skills=no_skills)
    res_b = evaluate_fit(stabber, [ModuleInput(type_id=mwd, slot=SlotKind.MED),
                                   ModuleInput(type_id=neut, slot=SlotKind.HIGH)],
                         skills=no_skills)
    cap_a, cap_b = res_a.telemetry["capacitor"], res_b.telemetry["capacitor"]

    capacity = _attr(stabber, A.CAP_CAPACITY) * _attr(mwd, ATTR_CAP_CAPACITY_MULT)
    tau = _attr(stabber, A.CAP_RECHARGE_RATE) / 1000.0
    peak = _cap_peak(capacity, tau)
    drain_a = _attr(mwd, A.CAP_NEED) / (_attr(mwd, ATTR_DURATION) / 1000.0)  # 16
    drain_b = drain_a + _attr(neut, A.CAP_NEED) / (
        _attr(neut, ATTR_DURATION) / 1000.0)                                 # 28.5

    assert cap_a["usage"] == pytest.approx(drain_a, rel=2e-3)
    assert cap_b["usage"] == pytest.approx(drain_b, rel=2e-3)
    assert drain_a > peak and drain_b > peak
    assert cap_a["stable"] is False and cap_b["stable"] is False
    assert cap_a["stable_pct"] is None and cap_b["stable_pct"] is None

    # Bounds independent of any integrator: recharge is between 0 and the peak
    # everywhere, so C/d <= runtime <= C/(d - peak).
    assert cap_a["runtime_s"] is not None and cap_b["runtime_s"] is not None
    assert capacity / drain_a - 1 <= cap_a["runtime_s"] <= \
        capacity / (drain_a - peak) + 1
    assert capacity / drain_b - 1 <= cap_b["runtime_s"] <= \
        capacity / (drain_b - peak) + 1
    assert cap_b["runtime_s"] < cap_a["runtime_s"]


# --------------------------------------------------------------------------- #
# 6. Cap Recharger II x2 + CCC rig: rechargeRate is stackable -> NO penalty
# --------------------------------------------------------------------------- #
def test_cap_rechargers_and_ccc_no_stacking_penalty(capmob):
    stabber = capmob["Stabber"]
    cr, ccc = capmob["Cap Recharger II"], capmob["Medium Capacitor Control Circuit I"]
    mods = [ModuleInput(type_id=cr, slot=SlotKind.MED, state=ModuleState.ONLINE)
            for _ in range(2)]
    mods += [ModuleInput(type_id=ccc, slot=SlotKind.RIG, state=ModuleState.ONLINE)]
    res = evaluate_fit(stabber, mods, skills=SkillProfile.from_dict({}))
    cap = res.telemetry["capacitor"]

    # dogma attr 55 (rechargeRate) is stackable=true in the SDE, so the two
    # postMul x0.8 rechargers and the CCC's postPercent -15% multiply plainly —
    # cap recharge modules are famously exempt from the stacking penalty.
    mult = _attr(cr, ATTR_CAP_RECHARGE_MULT)               # 0.8
    pct = _attr(ccc, ATTR_CAP_RECHARGE_PCT)                # -15
    tau = (_attr(stabber, A.CAP_RECHARGE_RATE) / 1000.0) * mult * mult \
        * (1.0 + pct / 100.0)                              # 427.5*0.64*0.85 = 232.56
    capacity = _attr(stabber, A.CAP_CAPACITY)              # unchanged, 1200
    assert cap["capacity"] == pytest.approx(capacity, rel=2e-3)
    assert cap["recharge_s"] == pytest.approx(tau, rel=2e-3)
    assert cap["peak_recharge"] == pytest.approx(_cap_peak(capacity, tau), rel=2e-3)


# --------------------------------------------------------------------------- #
# 7. Cap battery: flat capacitorBonus modAdd
# --------------------------------------------------------------------------- #
def test_cap_battery_flat_capacity_add(capmob):
    stabber, batt = capmob["Stabber"], capmob["Medium Cap Battery II"]
    res = evaluate_fit(stabber, [ModuleInput(type_id=batt, slot=SlotKind.MED,
                                             state=ModuleState.ONLINE)],
                       skills=SkillProfile.from_dict({}))
    cap = res.telemetry["capacitor"]

    capacity = _attr(stabber, A.CAP_CAPACITY) + _attr(batt, ATTR_CAP_BONUS)  # 1825
    tau = _attr(stabber, A.CAP_RECHARGE_RATE) / 1000.0     # recharge time unchanged
    assert cap["capacity"] == pytest.approx(capacity, rel=2e-3)
    assert cap["recharge_s"] == pytest.approx(tau, rel=2e-3)
    assert cap["peak_recharge"] == pytest.approx(_cap_peak(capacity, tau), rel=2e-3)


# --------------------------------------------------------------------------- #
# 8. Cap booster injection: charge bonus / (cycle + reload)
# --------------------------------------------------------------------------- #
def test_cap_booster_injection_arithmetic(capmob):
    stabber = capmob["Stabber"]
    booster, charge = capmob["Medium Capacitor Booster II"], capmob["Navy Cap Booster 800"]
    res = evaluate_fit(stabber, [ModuleInput(type_id=booster, slot=SlotKind.MED,
                                             charge_type_id=charge)],
                       skills=SkillProfile.from_dict({}))
    cap = res.telemetry["capacitor"]

    # Sustained injection: one 800 GJ charge per (12 s cycle + 10 s reload).
    inj = _attr(charge, ATTR_CAP_BONUS) / (
        (_attr(booster, ATTR_DURATION) + _attr(booster, ATTR_RELOAD_TIME)) / 1000.0)
    assert cap["injection"] == pytest.approx(inj, rel=2e-3)   # 36.36 GJ/s
    assert cap["usage"] == pytest.approx(0.0, abs=1e-6)       # booster costs no cap
    assert cap["stable"] is True
    assert cap["stable_pct"] == 100.0                         # net drain <= 0


# --------------------------------------------------------------------------- #
# 9. Nanofiber + Overdrive: penalised velocity chain + drawbacks
# --------------------------------------------------------------------------- #
def test_nanofiber_overdrive_velocity_chain_no_skills(capmob):
    stabber = capmob["Stabber"]
    nano, od = capmob["Nanofiber Internal Structure II"], \
        capmob["Overdrive Injector System II"]
    res = evaluate_fit(stabber, [
        ModuleInput(type_id=nano, slot=SlotKind.LOW, state=ModuleState.ONLINE),
        ModuleInput(type_id=od, slot=SlotKind.LOW, state=ModuleState.ONLINE),
    ], skills=SkillProfile.from_dict({}))
    t = res.telemetry

    # maxVelocity (37) is stackable=false and both sources are modules: one
    # penalised positive chain, strongest first (OD +12.5% > nano +9.5%).
    v_od = _attr(od, A.VELOCITY_BONUS_MOD) / 100.0
    v_nano = _attr(nano, A.VELOCITY_BONUS_MOD) / 100.0
    assert v_od > v_nano
    velocity = _attr(stabber, A.MAX_VELOCITY) * (1 + v_od) * (1 + v_nano * S1)
    assert t["mobility"]["max_velocity"] == pytest.approx(velocity, rel=2e-3)

    # Only the nanofiber touches agility (-15.75%, single entry -> no penalty).
    agility = _attr(stabber, A.AGILITY) * (
        1.0 + _attr(nano, A.AGILITY_MULTIPLIER) / 100.0)
    assert t["mobility"]["align_time_s"] == pytest.approx(
        _align_s(_attr(stabber, A.MASS), agility), rel=2e-3)

    # Drawbacks: nanofiber x0.8 hull HP, overdrive x0.8 cargo.
    assert t["defence"]["layers"]["hull"]["hp"] == pytest.approx(
        _attr(stabber, A.HULL_HP) * _attr(nano, A.STRUCTURE_HP_MULTIPLIER), rel=2e-3)
    assert t["utility"]["cargo"] == pytest.approx(
        _attr(stabber, A.CAPACITY_CARGO) * 0.8, rel=2e-3)  # cargoCapacityMultiplier 149


# --------------------------------------------------------------------------- #
# 10. Double istab: penalised agility chain + penalised signature penalty
# --------------------------------------------------------------------------- #
def test_double_istab_align_and_sig_penalty_no_skills(capmob):
    stabber, istab = capmob["Stabber"], capmob["Inertial Stabilizers II"]
    res = evaluate_fit(stabber, [ModuleInput(type_id=istab, slot=SlotKind.LOW,
                                             state=ModuleState.ONLINE)
                                 for _ in range(2)],
                       skills=SkillProfile.from_dict({}))
    mob = res.telemetry["mobility"]

    # agility (70) stackable=false: two -20% entries penalised as one chain.
    ag_bonus = _attr(istab, A.AGILITY_MULTIPLIER) / 100.0          # -0.20
    agility = _attr(stabber, A.AGILITY) * (1 + ag_bonus) * (1 + ag_bonus * S1)
    assert mob["align_time_s"] == pytest.approx(
        _align_s(_attr(stabber, A.MASS), agility), rel=2e-3)

    # signatureRadius (552) stackable=false: two +11% penalties, penalised chain.
    sig_bonus = _attr(istab, A.SIGNATURE_RADIUS_BONUS) / 100.0     # +0.11
    sig = _attr(stabber, A.SIGNATURE_RADIUS) * (1 + sig_bonus) * (1 + sig_bonus * S1)
    assert mob["signature_radius"] == pytest.approx(sig, rel=2e-3)  # ~121.6 m


# --------------------------------------------------------------------------- #
# 11. Plate mass + MWD fitted-but-off: align, armor, and the online cap penalty
# --------------------------------------------------------------------------- #
def test_plate_align_time_with_mwd_off(capmob):
    stabber = capmob["Stabber"]
    plate, mwd = capmob["800mm Steel Plates II"], capmob["50MN Microwarpdrive II"]
    res = evaluate_fit(stabber, [
        ModuleInput(type_id=plate, slot=SlotKind.LOW, state=ModuleState.ONLINE),
        ModuleInput(type_id=mwd, slot=SlotKind.MED, state=ModuleState.ONLINE),
    ], skills=SkillProfile.from_dict({}),
        op=OperatingProfile(propulsion_active=False))
    t = res.telemetry

    # Plate massAddition arrives via modAdd; the idle MWD adds NO mass, NO sig
    # bloom and NO thrust — but its x0.8 capacitor penalty is an ONLINE effect.
    mass = _attr(stabber, A.MASS) + _attr(plate, A.MASS_ADDITION)   # 12.85M kg
    assert t["mobility"]["mass"] == pytest.approx(mass, rel=2e-3)
    assert t["mobility"]["align_time_s"] == pytest.approx(
        _align_s(mass, _attr(stabber, A.AGILITY)), rel=2e-3)        # ~8.91 s
    assert t["mobility"]["max_velocity"] == pytest.approx(
        _attr(stabber, A.MAX_VELOCITY), rel=2e-3)
    assert t["mobility"]["propulsion_velocity"] == pytest.approx(
        t["mobility"]["max_velocity"], rel=2e-3)
    assert t["mobility"]["signature_radius"] == pytest.approx(
        _attr(stabber, A.SIGNATURE_RADIUS), rel=2e-3)
    assert t["defence"]["layers"]["armor"]["hp"] == pytest.approx(
        _attr(stabber, A.ARMOR_HP) + _attr(plate, A.ARMOR_PLATE_HP_BONUS), rel=2e-3)
    assert t["capacitor"]["capacity"] == pytest.approx(
        _attr(stabber, A.CAP_CAPACITY) * _attr(mwd, ATTR_CAP_CAPACITY_MULT), rel=2e-3)


# --------------------------------------------------------------------------- #
# 12. Hyperspatial rig: warp speed postPercent + signature drawback
# --------------------------------------------------------------------------- #
def test_hyperspatial_rig_warp_speed_no_skills(capmob):
    stabber = capmob["Stabber"]
    rig = capmob["Medium Hyperspatial Velocity Optimizer I"]
    res = evaluate_fit(stabber, [ModuleInput(type_id=rig, slot=SlotKind.RIG,
                                             state=ModuleState.ONLINE)],
                       skills=SkillProfile.from_dict({}))
    mob = res.telemetry["mobility"]

    # +20% warpSpeedMultiplier (attr 624 postPercent); single entry, so the
    # stacking penalty on non-stackable attr 600 is a no-op factor of 1.
    warp = _attr(stabber, A.WARP_SPEED_MULT) * (
        1.0 + _attr(rig, ATTR_WARP_SPEED_PCT) / 100.0)              # 4 * 1.2 = 4.8
    assert mob["warp_speed"] == pytest.approx(warp, rel=2e-3)
    # Navigation-rig drawback: +10% signature radius (attr 1138 postPercent).
    sig = _attr(stabber, A.SIGNATURE_RADIUS) * (
        1.0 + _attr(rig, ATTR_RIG_DRAWBACK) / 100.0)                # 110 m
    assert mob["signature_radius"] == pytest.approx(sig, rel=2e-3)


# --------------------------------------------------------------------------- #
# 13. Realistic nano-Stabber: MWD + nano + overdrive + istab, everything at once
# --------------------------------------------------------------------------- #
def test_realistic_nano_mwd_stabber_no_skills(capmob):
    stabber = capmob["Stabber"]
    mwd, nano = capmob["50MN Microwarpdrive II"], \
        capmob["Nanofiber Internal Structure II"]
    od, istab = capmob["Overdrive Injector System II"], \
        capmob["Inertial Stabilizers II"]
    res = evaluate_fit(stabber, [
        ModuleInput(type_id=mwd, slot=SlotKind.MED, state=ModuleState.ACTIVE),
        ModuleInput(type_id=nano, slot=SlotKind.LOW, state=ModuleState.ONLINE),
        ModuleInput(type_id=od, slot=SlotKind.LOW, state=ModuleState.ONLINE),
        ModuleInput(type_id=istab, slot=SlotKind.LOW, state=ModuleState.ONLINE),
    ], skills=SkillProfile.from_dict({}), op=OperatingProfile(propulsion_active=True))
    mob = res.telemetry["mobility"]

    # Base velocity: OD (+12.5%) then nano (+9.5%) in one penalised chain.
    base_v = _attr(stabber, A.MAX_VELOCITY) \
        * (1 + _attr(od, A.VELOCITY_BONUS_MOD) / 100.0) \
        * (1 + _attr(nano, A.VELOCITY_BONUS_MOD) / 100.0 * S1)
    assert mob["max_velocity"] == pytest.approx(base_v, rel=2e-3)

    # MWD thrust on the boosted base, with its +5M kg mass while running.
    mass = _attr(stabber, A.MASS) + _attr(mwd, A.MASS_ADDITION)
    v = base_v * (1.0 + (_attr(mwd, A.SPEED_BONUS) / 100.0)
                  * (_attr(mwd, A.SPEED_BOOST_FACTOR) / mass))
    assert mob["propulsion_velocity"] == pytest.approx(v, rel=2e-3)   # ~2000 m/s

    # Agility: istab -20% before nano -15.75% in the penalised negative chain.
    agility = _attr(stabber, A.AGILITY) \
        * (1 + _attr(istab, A.AGILITY_MULTIPLIER) / 100.0) \
        * (1 + _attr(nano, A.AGILITY_MULTIPLIER) / 100.0 * S1)
    assert mob["align_time_s"] == pytest.approx(_align_s(mass, agility), rel=2e-3)

    # Signature: the MWD bloom (+475%) and the istab penalty (+11%) share ONE stacking-
    # penalised chain on signatureRadius (552, non-stackable), sorted by magnitude — the MWD
    # (strongest) at full, the weaker istab penalised by S1. Before the WS-15 sigfix the MWD
    # was multiplied on SEPARATELY, escaping the penalty (100×1.11×5.75 = 638); the correct
    # joint chain gives ~630 (matches pyfa, which groups them).
    mwd_sig = _attr(mwd, A.SIGNATURE_RADIUS_BONUS) / 100.0        # +4.75 (strongest → S0)
    istab_sig = _attr(istab, A.SIGNATURE_RADIUS_BONUS) / 100.0    # +0.11 (weaker → S1)
    sig = _attr(stabber, A.SIGNATURE_RADIUS) \
        * (1 + mwd_sig) * (1 + istab_sig * S1)
    assert mob["signature_radius"] == pytest.approx(sig, rel=2e-3)    # ~630 m


# --------------------------------------------------------------------------- #
# 14. Sensor Booster unscripted: all three sensor bonuses at face value
# --------------------------------------------------------------------------- #
def test_sensor_booster_unscripted_no_skills(sensors):
    stabber, sb = sensors["Stabber"], sensors["Sensor Booster II"]
    res = evaluate_fit(stabber, [ModuleInput(type_id=sb, slot=SlotKind.MED)],
                       skills=SkillProfile.from_dict({}))
    tgt = res.telemetry["targeting"]

    # One module -> penalised chains of length 1 -> full effect everywhere.
    assert tgt["max_target_range"] == pytest.approx(
        _attr(stabber, A.MAX_TARGET_RANGE)
        * (1 + _attr(sb, ATTR_TARGET_RANGE_PCT) / 100.0), rel=2e-3)   # 61750 m
    assert tgt["scan_resolution"] == pytest.approx(
        _attr(stabber, A.SCAN_RESOLUTION)
        * (1 + _attr(sb, ATTR_SCAN_RES_PCT) / 100.0), rel=2e-3)       # 416 mm
    assert tgt["sensor_strength"] == pytest.approx(
        _attr(stabber, ATTR_LADAR_STRENGTH)
        * (1 + _attr(sb, ATTR_LADAR_PCT) / 100.0), abs=0.06)          # 13*1.48
    assert res.telemetry["capacitor"]["usage"] == pytest.approx(
        _attr(sb, A.CAP_NEED) / (_attr(sb, ATTR_DURATION) / 1000.0), rel=2e-3)


# --------------------------------------------------------------------------- #
# 15. Sensor Booster scripts: the charge rewrites the module's own bonuses
# --------------------------------------------------------------------------- #
def test_sensor_booster_scripts_no_skills(sensors):
    stabber, sb = sensors["Stabber"], sensors["Sensor Booster II"]
    range_pct = _attr(sb, ATTR_TARGET_RANGE_PCT)      # 30
    res_pct = _attr(sb, ATTR_SCAN_RES_PCT)            # 30
    ladar_pct = _attr(sb, ATTR_LADAR_PCT)             # 48
    base_range = _attr(stabber, A.MAX_TARGET_RANGE)
    base_res = _attr(stabber, A.SCAN_RESOLUTION)
    base_ladar = _attr(stabber, ATTR_LADAR_STRENGTH)

    def run(script_name):
        return evaluate_fit(
            stabber, [ModuleInput(type_id=sb, slot=SlotKind.MED,
                                  charge_type_id=sensors[script_name])],
            skills=SkillProfile.from_dict({})).telemetry["targeting"]

    # Each script postPercents the module's OWN bonus attributes by +/-100%
    # (effects 3597/3598/6488, otherID domain; charges are stacking-exempt):
    # the boosted bonus doubles, the other two are zeroed.
    t = run("Targeting Range Script")
    assert t["max_target_range"] == pytest.approx(
        base_range * (1 + 2 * range_pct / 100.0), rel=2e-3)           # +60%
    assert t["scan_resolution"] == pytest.approx(base_res, rel=2e-3)  # zeroed
    assert t["sensor_strength"] == pytest.approx(base_ladar, abs=0.06)

    t = run("Scan Resolution Script")
    assert t["scan_resolution"] == pytest.approx(
        base_res * (1 + 2 * res_pct / 100.0), rel=2e-3)               # +60%
    assert t["max_target_range"] == pytest.approx(base_range, rel=2e-3)
    assert t["sensor_strength"] == pytest.approx(base_ladar, abs=0.06)

    # ECCM script: the modern "backup array" — doubles sensor strength only.
    t = run("ECCM Script")
    assert t["sensor_strength"] == pytest.approx(
        base_ladar * (1 + 2 * ladar_pct / 100.0), abs=0.06)           # 13*1.96
    assert t["sensor_type"] == "ladar"
    assert t["max_target_range"] == pytest.approx(base_range, rel=2e-3)
    assert t["scan_resolution"] == pytest.approx(base_res, rel=2e-3)


# --------------------------------------------------------------------------- #
# 16. Two sensor boosters: penalised chains on range/res/strength
# --------------------------------------------------------------------------- #
def test_dual_sensor_booster_stacking_no_skills(sensors):
    stabber, sb = sensors["Stabber"], sensors["Sensor Booster II"]
    res = evaluate_fit(stabber, [ModuleInput(type_id=sb, slot=SlotKind.MED)
                                 for _ in range(2)],
                       skills=SkillProfile.from_dict({}))
    tgt = res.telemetry["targeting"]

    # All three target attributes (76, 564, 209) are stackable=false and both
    # sources are modules: identical bonuses, second one at S1 effectiveness.
    def chain(base, pct):
        return base * (1 + pct / 100.0) * (1 + pct / 100.0 * S1)

    assert tgt["max_target_range"] == pytest.approx(
        chain(_attr(stabber, A.MAX_TARGET_RANGE),
              _attr(sb, ATTR_TARGET_RANGE_PCT)), rel=2e-3)
    assert tgt["scan_resolution"] == pytest.approx(
        chain(_attr(stabber, A.SCAN_RESOLUTION),
              _attr(sb, ATTR_SCAN_RES_PCT)), rel=2e-3)
    assert tgt["sensor_strength"] == pytest.approx(
        chain(_attr(stabber, ATTR_LADAR_STRENGTH),
              _attr(sb, ATTR_LADAR_PCT)), abs=0.06)                   # ~27.3


# --------------------------------------------------------------------------- #
# 17. Signal Amplifier + Auto Targeting System: flat max-target adds
# --------------------------------------------------------------------------- #
def test_signal_amp_auto_targeter_max_targets_no_skills(sensors):
    stabber = sensors["Stabber"]
    amp, ats = sensors["Signal Amplifier II"], sensors["Auto Targeting System I"]
    res = evaluate_fit(stabber, [
        ModuleInput(type_id=amp, slot=SlotKind.LOW, state=ModuleState.ONLINE),
        ModuleInput(type_id=ats, slot=SlotKind.HIGH, state=ModuleState.ONLINE),
    ], skills=SkillProfile.from_dict({}))
    tgt = res.telemetry["targeting"]

    # maxLockedTargets (192) is a plain modAdd target: 5 + 2 (amp) + 2 (ATS).
    expected_targets = int(_attr(stabber, A.MAX_LOCKED_TARGETS)
                           + _attr(amp, ATTR_MAX_TARGETS_ADD)
                           + _attr(ats, ATTR_MAX_TARGETS_ADD))
    assert tgt["max_locked_targets"] == expected_targets              # exactly 9

    # The amp's percentage bonuses apply at face value (single module).
    assert tgt["max_target_range"] == pytest.approx(
        _attr(stabber, A.MAX_TARGET_RANGE)
        * (1 + _attr(amp, ATTR_TARGET_RANGE_PCT) / 100.0), rel=2e-3)  # +30%
    assert tgt["scan_resolution"] == pytest.approx(
        _attr(stabber, A.SCAN_RESOLUTION)
        * (1 + _attr(amp, ATTR_SCAN_RES_PCT) / 100.0), rel=2e-3)      # +15%
    assert tgt["sensor_strength"] == pytest.approx(
        _attr(stabber, ATTR_LADAR_STRENGTH)
        * (1 + _attr(amp, ATTR_LADAR_PCT) / 100.0), abs=0.06)         # +48%


# --------------------------------------------------------------------------- #
# 18. Targeting skill layer at All V (skills are stacking-exempt)
# --------------------------------------------------------------------------- #
def test_bare_hull_targeting_omniscient(sensors):
    stabber = sensors["Stabber"]
    res = evaluate_fit(stabber, [])
    tgt = res.telemetry["targeting"]

    assert tgt["max_target_range"] == pytest.approx(
        _attr(stabber, A.MAX_TARGET_RANGE) * LONG_RANGE_TARGETING_V, rel=2e-3)
    assert tgt["scan_resolution"] == pytest.approx(
        _attr(stabber, A.SCAN_RESOLUTION) * SIGNATURE_ANALYSIS_V, rel=2e-3)
    assert tgt["sensor_strength"] == pytest.approx(
        _attr(stabber, ATTR_LADAR_STRENGTH) * LADAR_COMPENSATION_V, abs=0.06)
    # Target Management skills raise the CHARACTER's cap, not the hull's
    # maxLockedTargets: the ship-side readout stays at the hull value.
    assert tgt["max_locked_targets"] == int(_attr(stabber, A.MAX_LOCKED_TARGETS))
