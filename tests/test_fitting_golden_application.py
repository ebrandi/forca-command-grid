"""Golden fits: application model — turret/drone applied DPS, lock time, warp time.

Engine v2, real SDE slices, hand-derived numbers. Every expected value is DERIVED IN THE
TEST from the fixture slice's base attributes plus documented EVE mechanics (studied for
behaviour only in pyfa's GPL eos — never read back from the engine).

Formulas (see apps/fitting/engine/evaluator.py for the citations to the pyfa source):

* Turret/drone chance-to-hit CTH = rangeFactor × trackingFactor, both of the form
  0.5**exponent so the exponents add:
    CTH = 0.5 ** ( (max(0, distance − optimal)/falloff)²
                   + ((angular × optimalSigRadius)/(tracking × targetSig))² )
* Expected per-shot damage multiplier from CTH (wrecking shots included):
    E(cth) = min(cth,0.01)·3 + max(0, cth−0.01)·((0.01+cth)/2 + 0.49)
  E(1.0) = 1.01505 (a perfect shot averages slightly above paper DPS via wrecking). We
  NORMALISE by E(1.0) so applied ≤ raw and a perfect shot reports applied == raw — the
  same convention as the missile path (applied = raw × min(1, …)).
* Lock time  = min(40000 / scanRes / asinh(sig)², 30 min).
* Warp time  = the CCP "Warp Drive Active" piecewise accel/cruise/decel model.
"""
from __future__ import annotations

import math

import pytest

from apps.fitting.engine.types import (
    ModuleInput,
    ModuleState,
    OperatingProfile,
    SkillProfile,
    SlotKind,
    TargetProfile,
)

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db

NO_SKILLS = SkillProfile.from_dict({})
AU_METERS = 149_597_870_700

# --- dogma attribute ids used by the derivations -----------------------------------
TRACKING = 160
OPT_SIG = 620
MAX_RANGE = 54
FALLOFF = 158
DMG_MULT = 64
ROF = 51
SCAN_RES = 564
MAX_VEL = 37
WARP_MULT = 600
BASE_WARP = 1281
RANGE_MULT = 120     # ammo weaponRangeMultiplier (preMul maxRange)
TRACK_MULT = 244     # ammo trackingSpeedMultiplier (postMul tracking)
FALLOFF_MULT = 517   # ammo fallofMultiplier (postMul falloff); absent on EMP S


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _has(type_id, attr_id) -> bool:
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.filter(type_id=type_id, attribute_id=attr_id).exists()


def _shot(type_id) -> float:
    return sum(_attr(type_id, a) for a in (114, 116, 117, 118) if _has(type_id, a))


# --- independent implementation of the documented closed form ----------------------
def _emult(cth: float) -> float:
    wrecking = min(cth, 0.01) * 3.0
    normal = cth - min(cth, 0.01)
    return normal * ((0.01 + cth) / 2.0 + 0.49) + wrecking if normal > 0 else wrecking


PERFECT = _emult(1.0)


def _cth(tracking, opt_sig, optimal, falloff, angular, tgt_sig, distance) -> float:
    range_exp = (max(0.0, distance - optimal) / falloff) ** 2 if falloff > 0 else 0.0
    track_exp = ((angular * opt_sig) / (tracking * tgt_sig)) ** 2 \
        if tracking > 0 and tgt_sig > 0 else 0.0
    return 0.5 ** (range_exp + track_exp)


def _applied(cth: float) -> float:
    return _emult(cth) / PERFECT


def _warp_time(warp_speed_au, subwarp, dist_au) -> float:
    dropout = min(subwarp / 2.0, 100.0)
    warp_dist = dist_au * AU_METERS
    k_accel = warp_speed_au
    k_decel = min(warp_speed_au / 3.0, 2.0)
    max_ms = warp_speed_au * AU_METERS
    minimum_dist = AU_METERS + max_ms / k_decel
    cruise = 0.0
    if minimum_dist > warp_dist:
        max_ms = warp_dist * k_accel * k_decel / (k_accel + k_decel)
    else:
        cruise = (warp_dist - minimum_dist) / max_ms
    return cruise + math.log(max_ms / k_accel) / k_accel \
        + math.log(max_ms / dropout) / k_decel


def test_perfect_turret_mult_baseline_is_1_01505():
    """The wrecking-shot bonus makes a perfect shot average 1.01505× paper damage; the
    engine normalises by this so applied == raw at perfect application (never > raw)."""
    assert PERFECT == pytest.approx(1.01505, abs=1e-9)


# =====================================================================================
# Turrets — Rifter + 150mm Light AutoCannon II + Republic Fleet EMP S, NO skills
# =====================================================================================
@pytest.fixture()
def rif():
    return load_graph_fixture("rifter_ac")


def _gun_attrs(gun, ammo):
    """Evaluated turret application attrs with no skills = base × ammo multipliers."""
    tracking = _attr(gun, TRACKING) * _attr(ammo, TRACK_MULT)          # 362 × 1.0
    opt_sig = _attr(gun, OPT_SIG)                                       # 40000 (no ammo mod)
    optimal = _attr(gun, MAX_RANGE) * _attr(ammo, RANGE_MULT)          # 1080 × 0.5 = 540
    falloff = _attr(gun, FALLOFF) * (_attr(ammo, FALLOFF_MULT)
                                     if _has(ammo, FALLOFF_MULT) else 1.0)   # 4730
    return tracking, opt_sig, optimal, falloff


def _raw_dps(gun, ammo) -> float:
    return _shot(ammo) * _attr(gun, DMG_MULT) / (_attr(gun, ROF) / 1000.0)


def test_turret_applied_dps_mid_range_hand_derived(rif):
    """CASE (a): distance = optimal + falloff (range exponent 1) and angular 0.362 rad/s
    chosen so the tracking exponent is exactly 1 → CTH = 0.5**(1+1) = 0.25.
        E(0.25) = 0.03 + (0.25−0.01)·((0.01+0.25)/2 + 0.49)
                = 0.03 + 0.24·0.62 = 0.1788
        applied_multiplier = 0.1788 / 1.01505 = 0.176149…
    Both the range and tracking terms are exercised. Derived-angular equivalence is
    checked separately below."""
    ship, gun, ammo = rif["Rifter"], rif["150mm Light AutoCannon II"], \
        rif["Republic Fleet EMP S"]
    tracking, opt_sig, optimal, falloff = _gun_attrs(gun, ammo)
    distance = optimal + falloff
    angular = 0.362
    # angular chosen so (angular·optSig)/(tracking·sig) == 1 with sig 40:
    tgt_sig = 40.0
    assert (angular * opt_sig) / (tracking * tgt_sig) == pytest.approx(1.0, rel=1e-9)
    cth = _cth(tracking, opt_sig, optimal, falloff, angular, tgt_sig, distance)
    assert cth == pytest.approx(0.25, abs=1e-9)
    amult = _applied(cth)

    op = OperatingProfile(propulsion_active=False, target=TargetProfile(
        signature_radius=tgt_sig, target_distance_m=distance, target_angular=angular))
    res = evaluate_fit(ship, [ModuleInput(gun, SlotKind.HIGH, ModuleState.ACTIVE,
                                          charge_type_id=ammo)], skills=NO_SKILLS, op=op)
    off = res.telemetry["offence"]
    w = off["weapons"][0]
    assert w["applied_multiplier"] == pytest.approx(amult, abs=1e-3)
    assert w["applied_dps"] == pytest.approx(_raw_dps(gun, ammo) * amult, rel=2e-3, abs=0.1)
    assert w["applied_dps"] == pytest.approx(w["dps"] * amult, rel=2e-3, abs=0.1)
    # applied ≤ raw (normalised convention).
    assert w["applied_dps"] < w["dps"]
    assert off["turret_dps_applied"] == pytest.approx(w["applied_dps"], rel=2e-3, abs=0.1)
    assert off["turret_application"] == pytest.approx(amult, abs=1e-3)
    assert off["applied_complete"] is True


def test_turret_angular_derived_from_velocity_matches_explicit(rif):
    """The orbit assumption: angular absent → derived velocity/distance. Setting
    velocity = 0.362 × distance with no explicit angular reproduces CASE (a)'s CTH."""
    ship, gun, ammo = rif["Rifter"], rif["150mm Light AutoCannon II"], \
        rif["Republic Fleet EMP S"]
    _t, _s, optimal, falloff = _gun_attrs(gun, ammo)
    distance = optimal + falloff
    velocity = 0.362 * distance
    op = OperatingProfile(propulsion_active=False, target=TargetProfile(
        signature_radius=40.0, velocity=velocity, target_distance_m=distance))
    res = evaluate_fit(ship, [ModuleInput(gun, SlotKind.HIGH, ModuleState.ACTIVE,
                                          charge_type_id=ammo)], skills=NO_SKILLS, op=op)
    w = res.telemetry["offence"]["weapons"][0]
    tracking, opt_sig, optimal, falloff = _gun_attrs(gun, ammo)
    cth = _cth(tracking, opt_sig, optimal, falloff, velocity / distance, 40.0, distance)
    assert w["applied_multiplier"] == pytest.approx(_applied(cth), rel=2e-3)
    assert w["applied_multiplier"] == pytest.approx(0.176149, rel=2e-3)


def test_turret_beyond_falloff_is_tiny_wrecking_only(rif):
    """CASE (b): distance = optimal + 3×falloff (range exponent 9), stationary target
    (angular 0). CTH = 0.5**9 = 0.001953 ≤ 0.01, so the whole hit region is wrecking:
        E = CTH·3 = 0.005859 ; applied_multiplier = 0.005859 / 1.01505 = 0.005772."""
    ship, gun, ammo = rif["Rifter"], rif["150mm Light AutoCannon II"], \
        rif["Republic Fleet EMP S"]
    tracking, opt_sig, optimal, falloff = _gun_attrs(gun, ammo)
    distance = optimal + 3 * falloff
    cth = _cth(tracking, opt_sig, optimal, falloff, 0.0, 40.0, distance)
    assert cth == pytest.approx(2 ** -9, abs=1e-9)
    assert cth <= 0.01                                   # wrecking-only branch
    amult = _applied(cth)
    assert amult == pytest.approx(0.005772, rel=2e-3)

    op = OperatingProfile(propulsion_active=False, target=TargetProfile(
        signature_radius=40.0, velocity=0.0, target_distance_m=distance))
    res = evaluate_fit(ship, [ModuleInput(gun, SlotKind.HIGH, ModuleState.ACTIVE,
                                          charge_type_id=ammo)], skills=NO_SKILLS, op=op)
    w = res.telemetry["offence"]["weapons"][0]
    assert w["applied_multiplier"] == pytest.approx(amult, abs=5e-4)
    assert w["applied_dps"] == pytest.approx(_raw_dps(gun, ammo) * amult, rel=3e-3, abs=0.1)


def test_turret_perfect_application_equals_raw(rif):
    """A stationary target at optimal range (angular 0, range exponent 0) → CTH = 1 →
    applied_multiplier = E(1)/E(1) = 1.0, so applied DPS equals raw DPS exactly."""
    ship, gun, ammo = rif["Rifter"], rif["150mm Light AutoCannon II"], \
        rif["Republic Fleet EMP S"]
    _t, _s, optimal, _f = _gun_attrs(gun, ammo)
    op = OperatingProfile(propulsion_active=False, target=TargetProfile(
        signature_radius=1000.0, velocity=0.0, target_distance_m=optimal))
    res = evaluate_fit(ship, [ModuleInput(gun, SlotKind.HIGH, ModuleState.ACTIVE,
                                          charge_type_id=ammo)], skills=NO_SKILLS, op=op)
    w = res.telemetry["offence"]["weapons"][0]
    assert w["applied_multiplier"] == pytest.approx(1.0, abs=1e-3)
    assert w["applied_dps"] == pytest.approx(w["dps"], rel=1e-3)


def test_turret_incomplete_profile_is_null_not_faked(rif):
    """CASE (c): a target with sig but NO distance can't give turrets a range term →
    applied_dps null with reason, applied_complete False, excluded from the total."""
    ship, gun, ammo = rif["Rifter"], rif["150mm Light AutoCannon II"], \
        rif["Republic Fleet EMP S"]
    op = OperatingProfile(propulsion_active=False,
                          target=TargetProfile(signature_radius=40.0, velocity=200.0))
    res = evaluate_fit(ship, [ModuleInput(gun, SlotKind.HIGH, ModuleState.ACTIVE,
                                          charge_type_id=ammo)], skills=NO_SKILLS, op=op)
    off = res.telemetry["offence"]
    w = off["weapons"][0]
    assert w["applied_dps"] is None
    assert w["applied_reason"] == "target_profile_incomplete"
    assert off["applied_complete"] is False
    assert off["turret_dps_applied"] == 0.0
    assert off["total_applied_dps"] == 0.0
    # The engine no longer flags turret application as unmodelled in v2.
    assert "turret_application_not_modelled" not in res.unsupported


def test_total_applied_dps_sums_per_weapon(rif):
    """CASE (h): four identical guns, complete profile → total_applied_dps equals the
    sum of the per-weapon applied_dps and turret_dps_applied, exactly."""
    ship, gun, ammo = rif["Rifter"], rif["150mm Light AutoCannon II"], \
        rif["Republic Fleet EMP S"]
    _t, _s, optimal, falloff = _gun_attrs(gun, ammo)
    op = OperatingProfile(propulsion_active=False, target=TargetProfile(
        signature_radius=40.0, target_distance_m=optimal + falloff, target_angular=0.362))
    mods = [ModuleInput(gun, SlotKind.HIGH, ModuleState.ACTIVE, charge_type_id=ammo)
            for _ in range(4)]
    res = evaluate_fit(ship, mods, skills=NO_SKILLS, op=op)
    off = res.telemetry["offence"]
    per_weapon = sum(w["applied_dps"] for w in off["weapons"])
    assert off["total_applied_dps"] == pytest.approx(per_weapon, abs=0.3)
    assert off["turret_dps_applied"] == pytest.approx(per_weapon, abs=0.3)
    assert off["applied_complete"] is True


# =====================================================================================
# Drones — Vexor + Garde II (sentry) / Hammerhead II (mobile), NO skills
# =====================================================================================
@pytest.fixture()
def vex():
    return load_graph_fixture("vexor_drones")


def _drone_dps(dr) -> float:
    return _shot(dr) * _attr(dr, DMG_MULT) / (_attr(dr, ROF) / 1000.0)


def test_sentry_drone_applied_at_30km_hand_derived(vex):
    """CASE (d): one Garde II sentry (maxVelocity≈0 → always the turret formula) at 30 km
    against a 400 m target moving 302.4 m/s. Base sentry attrs (no skills): tracking
    0.0336, optimalSigRadius 400, optimal 18000, falloff 30000.
        angular = 302.4 / 30000 = 0.01008 rad/s
        range exp = ((30000−18000)/30000)² = 0.16 ; track exp = (0.01008·400/(0.0336·400))²
        CTH = 0.5**(0.16+0.09) = 0.840896 ; applied_multiplier = 0.778920."""
    vexor, garde = vex["Vexor"], vex["Garde II"]
    tracking = _attr(garde, TRACKING)
    opt_sig = _attr(garde, OPT_SIG)
    optimal = _attr(garde, MAX_RANGE)
    falloff = _attr(garde, FALLOFF)
    distance, tgt_sig, velocity = 30000.0, 400.0, 302.4
    angular = velocity / distance
    cth = _cth(tracking, opt_sig, optimal, falloff, angular, tgt_sig, distance)
    amult = _applied(cth)
    assert amult == pytest.approx(0.778920, rel=2e-3)

    op = OperatingProfile(propulsion_active=False, target=TargetProfile(
        signature_radius=tgt_sig, velocity=velocity, target_distance_m=distance))
    res = evaluate_fit(vexor, [ModuleInput(garde, SlotKind.DRONE, ModuleState.ACTIVE)],
                       skills=NO_SKILLS, op=op)
    off = res.telemetry["offence"]
    assert off["drone_dps_applied"] == pytest.approx(_drone_dps(garde) * amult,
                                                     rel=2e-3, abs=0.1)
    assert off["drone_application"] == pytest.approx(amult, abs=1e-3)
    assert off["applied_complete"] is True


def test_mobile_drone_full_vs_slow_target(vex):
    """CASE (e-i): a Hammerhead II (maxVelocity 2016) at least as fast as a 300 m/s target
    chases it down — cth = 1 → applied == raw (full application), independent of range."""
    vexor, hh = vex["Vexor"], vex["Hammerhead II"]
    assert _attr(hh, MAX_VEL) > 300.0
    op = OperatingProfile(propulsion_active=False, target=TargetProfile(
        signature_radius=400.0, velocity=300.0, target_distance_m=5000.0))
    res = evaluate_fit(vexor, [ModuleInput(hh, SlotKind.DRONE, ModuleState.ACTIVE)],
                       skills=NO_SKILLS, op=op)
    off = res.telemetry["offence"]
    assert off["drone_dps_applied"] == pytest.approx(_drone_dps(hh), rel=2e-3, abs=0.1)
    assert off["drone_application"] == pytest.approx(1.0, abs=2e-3)


def test_mobile_drone_degraded_vs_fast_target(vex):
    """CASE (e-ii): the same Hammerhead II against a 3000 m/s target it cannot outrun
    (2016 < 3000) → treated as a stationary turret tracking the full orbit angular.
        angular = 3000/5000 = 0.6 ; base attrs tracking 0.696, optSig 125, optimal 4200,
        falloff 3000 → CTH = 0.905204, applied_multiplier = 0.865274 (< 1, degraded)."""
    vexor, hh = vex["Vexor"], vex["Hammerhead II"]
    tracking = _attr(hh, TRACKING)
    opt_sig = _attr(hh, OPT_SIG)
    optimal = _attr(hh, MAX_RANGE)
    falloff = _attr(hh, FALLOFF)
    distance, tgt_sig, velocity = 5000.0, 400.0, 3000.0
    assert _attr(hh, MAX_VEL) < velocity
    cth = _cth(tracking, opt_sig, optimal, falloff, velocity / distance, tgt_sig, distance)
    amult = _applied(cth)
    assert amult == pytest.approx(0.865274, rel=2e-3)
    assert amult < 1.0

    op = OperatingProfile(propulsion_active=False, target=TargetProfile(
        signature_radius=tgt_sig, velocity=velocity, target_distance_m=distance))
    res = evaluate_fit(vexor, [ModuleInput(hh, SlotKind.DRONE, ModuleState.ACTIVE)],
                       skills=NO_SKILLS, op=op)
    off = res.telemetry["offence"]
    assert off["drone_dps_applied"] == pytest.approx(_drone_dps(hh) * amult,
                                                     rel=2e-3, abs=0.1)
    assert off["drone_dps_applied"] < off["drone_dps"]


# =====================================================================================
# Lock time + warp time — bare Rifter, NO skills
# =====================================================================================
def test_lock_time_hand_computed_via_asinh(rif):
    """CASE (f): Rifter scanResolution 660 against a 125 m target →
        40000 / 660 / asinh(125)² = 1.9880 s."""
    ship = rif["Rifter"]
    scan_res = _attr(ship, SCAN_RES)                     # 660, no skills → base
    tgt_sig = 125.0
    expected = min(40000.0 / scan_res / (math.asinh(tgt_sig) ** 2), 30 * 60.0)
    assert expected == pytest.approx(1.98795, rel=1e-4)
    op = OperatingProfile(propulsion_active=False,
                          target=TargetProfile(signature_radius=tgt_sig))
    res = evaluate_fit(ship, [], skills=NO_SKILLS, op=op)
    assert res.telemetry["targeting"]["lock_time_s"] == pytest.approx(expected, rel=2e-3)


def test_lock_time_absent_without_target(rif):
    """No target → no lock-time value (never a faked zero)."""
    res = evaluate_fit(rif["Rifter"], [], skills=NO_SKILLS)
    assert res.telemetry["targeting"]["lock_time_s"] is None


def test_warp_time_hand_computed_piecewise_10au(rif):
    """CASE (g): Rifter warp speed 5 AU/s (baseWarpSpeed 1 × warpSpeedMultiplier 5),
    subwarp 365 m/s, over the default 10 AU. Accel + cruise + decel per the CCP model:
        cruise 1.200 + accel 5.146 + decel 13.641 = 19.988 s."""
    ship = rif["Rifter"]
    warp_speed = _attr(ship, WARP_MULT) * (_attr(ship, BASE_WARP)
                                           if _has(ship, BASE_WARP) else 1.0)   # 5.0
    subwarp = _attr(ship, MAX_VEL)                       # 365, no skills → base
    expected = _warp_time(warp_speed, subwarp, 10.0)
    assert expected == pytest.approx(19.9875, rel=1e-4)
    res = evaluate_fit(ship, [], skills=NO_SKILLS)       # default warp_distance_au = 10
    mob = res.telemetry["mobility"]
    assert mob["warp_distance_au"] == pytest.approx(10.0)
    assert mob["warp_time_s"] == pytest.approx(expected, rel=2e-3)


def test_warp_time_scales_with_requested_distance(rif):
    """A longer warp costs more time; the requested distance flows through the profile."""
    ship = rif["Rifter"]
    warp_speed = _attr(ship, WARP_MULT)
    subwarp = _attr(ship, MAX_VEL)
    op = OperatingProfile(propulsion_active=False, warp_distance_au=30.0)
    res = evaluate_fit(ship, [], skills=NO_SKILLS, op=op)
    mob = res.telemetry["mobility"]
    assert mob["warp_distance_au"] == pytest.approx(30.0)
    assert mob["warp_time_s"] == pytest.approx(_warp_time(warp_speed, subwarp, 30.0),
                                               rel=2e-3)


# =====================================================================================
# Missiles — uniform applied_dps field + total, on the shared missile fixture
# =====================================================================================
def test_missile_applied_dps_field_and_total_consistency():
    """Missiles gain the uniform per-weapon `applied_dps` field; the per-weapon values sum
    to missile_dps_applied and feed total_applied_dps (applied ≤ raw, and complete since
    missiles need only sig/velocity)."""
    ids = load_graph_fixture("missiles_graph")
    ship = ids["Caracal"]
    launcher, missile = ids["Heavy Missile Launcher II"], ids["Scourge Heavy Missile"]
    op = OperatingProfile(propulsion_active=False,
                          target=TargetProfile(signature_radius=40.0, velocity=300.0))
    mods = [ModuleInput(launcher, SlotKind.HIGH, ModuleState.ACTIVE,
                        charge_type_id=missile) for _ in range(2)]
    res = evaluate_fit(ship, mods, op=op)
    off = res.telemetry["offence"]
    per_weapon = sum(w["applied_dps"] for w in off["weapons"] if w["kind"] == "missile")
    assert per_weapon == pytest.approx(off["missile_dps_applied"], abs=0.2)
    assert off["missile_dps_applied"] < off["missile_dps"]     # small fast target
    assert off["total_applied_dps"] == pytest.approx(off["missile_dps_applied"], abs=0.2)
    assert off["applied_complete"] is True
