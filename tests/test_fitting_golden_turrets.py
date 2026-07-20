"""Golden fits: turrets (engine v2, real SDE slices, hand-derived numbers).

Domain: hybrid (Cormorant railguns + Magnetic Field Stabilizers), energy (Omen pulse
lasers + Heat Sinks + frequency-crystal optimal change), medium projectile (Rupture
425mm ACs + Gyrostabilizers), T2 spec-skill scaling, weapon-rig + damage-mod stacking
in one chain, range/tracking telemetry incl. ammo multipliers (Barrage), overload
bonuses, offline/online weapons, volley/dps consistency and the turret-hardpoint
overflow diagnostic.

Every expected value is DERIVED IN THE TEST from the fixture slice's base attributes
(read back from the SDE tables the slice loaded) plus documented EVE mechanics:

* stacking penalty S(i) = exp(-(i/2.67)^2) applied per (target attribute, operator)
  bucket, magnitudes sorted descending, positive/negative chains separate, only when
  the TARGET attribute is stackable=false and the SOURCE is a module (ship, charge,
  skill, implant and subsystem sources are exempt);
* skill/trait percentages are read from the bonus attribute on the skill/hull type
  itself (dgm data: postPercent, pre-scaled by skillLevel/280) and multiplied by the
  trained level;
* ammo multipliers: weaponRangeMultiplier(120) pre-multiplies the gun's maxRange(54),
  fallofMultiplier(517) post-multiplies falloff(158), trackingSpeedMultiplier(244)
  post-multiplies trackingSpeed(160) — charge sources are stacking-exempt;
* overload (module OVERHEATED): overloadRofBonus(1205) / overloadDamageModifier(1210)
  postPercent on the gun's own speed(51) / damageMultiplier(64).

Never read back from the engine.
"""
from __future__ import annotations

import math

import pytest

from apps.fitting.engine import attributes as A
from apps.fitting.engine.types import ModuleInput, ModuleState, SkillProfile, SlotKind

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db

S1 = math.exp(-((1 / 2.67) ** 2))   # 0.8691… stacking factor for the 2nd module

# --- dogma attribute ids used beyond attributes.py's named set -----------------------
WEAPON_RANGE_MULT = 120      # ammo weaponRangeMultiplier -> preMul gun maxRange (54)
TRACKING_MULT = 244          # ammo trackingSpeedMultiplier -> postMul tracking (160)
FALLOFF_MULT = 517           # ammo fallofMultiplier -> postMul falloff (158)
DMG_BONUS = 292              # skill damageMultiplierBonus (%/level, postPercent)
ROF_BONUS = 293              # Rapid Firing rofBonus (%/level)
RANGE_SKILL_BONUS = 294      # Sharpshooter rangeSkillBonus (%/level)
FALLOFF_BONUS = 349          # Trajectory Analysis falloffBonus (%/level)
TURRET_SPEED_BONUS = 441     # Gunnery turretSpeeBonus (%/level)
TRACKING_BONUS = 767         # Motion Prediction trackingSpeedBonus (%/level)
SHIP_ROLE_MAXRANGE = 351     # Cormorant maxRangeBonus role attr (NOT level-scaled)
SHIP_BONUS_CD1 = 734         # Cormorant optimal %/Caldari Destroyer level
SHIP_BONUS_CD2 = 735         # Cormorant tracking %/Caldari Destroyer level
SHIP_BONUS_MC = 489          # Rupture damage %/Minmatar Cruiser level
SHIP_BONUS_MC2 = 659         # Rupture tracking %/Minmatar Cruiser level

# --- skill type ids (category 16; percentages are read from the DB, not hardcoded) ---
GUNNERY = 3300               # -2% turret RoF time /lvl (attr 441, effect 414)
RAPID_FIRING = 3310          # -4% turret RoF time /lvl (attr 293, effect 582)
SURGICAL_STRIKE = 3315       # +3% turret damage /lvl (attr 292, effects 587/588/589)
SHARPSHOOTER = 3311          # +5% optimal /lvl (attr 294, effect 290)
MOTION_PREDICTION = 3312     # +5% tracking /lvl (attr 767, effect 2847)
TRAJECTORY_ANALYSIS = 3317   # +5% falloff /lvl (attr 349, effect 298)
SMALL_HYBRID_TURRET = 3301   # +5% damage /lvl (attr 292, effect 173)
MEDIUM_PROJECTILE_TURRET = 3305   # +5% damage /lvl (attr 292, effect 161)
SMALL_RAILGUN_SPEC = 11082   # +2% damage /lvl on T2 rails (attr 292, effect 1006)
MEDIUM_AC_SPEC = 12208       # +2% damage /lvl on T2 ACs (attr 292, effect 1013)
CALDARI_DESTROYER = 33092    # scales Cormorant trait attrs 734/735 per level
MINMATAR_CRUISER = 3333      # scales Rupture trait attrs 489/659 per level

NO_SKILLS = {}


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _has(type_id, attr_id) -> bool:
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.filter(type_id=type_id, attribute_id=attr_id).exists()


def _shot(ammo) -> float:
    return sum(_attr(ammo, a) for a in
               (A.EM_DAMAGE, A.THERMAL_DAMAGE, A.KINETIC_DAMAGE, A.EXPLOSIVE_DAMAGE)
               if _has(ammo, a))


def _chain(base: float, deltas: list[float]) -> float:
    """Apply one stacking-penalised multiplicative chain (single operator bucket,
    same sign): magnitudes sorted descending, i-th factor scaled by S1**(i*i) —
    identically exp(-(i/2.67)^2)."""
    v = base
    for i, d in enumerate(sorted(deltas, key=abs, reverse=True)):
        v *= 1.0 + d * (S1 ** (i * i))
    return v


def _guns(type_id, n, ammo, state=ModuleState.ACTIVE):
    return [ModuleInput(type_id=type_id, slot=SlotKind.HIGH, state=state,
                       charge_type_id=ammo) for _ in range(n)]


def _lows(type_id, n):
    return [ModuleInput(type_id=type_id, slot=SlotKind.LOW, state=ModuleState.ONLINE)
            for _ in range(n)]


# =====================================================================================
# Hybrid — Cormorant + railguns + Magnetic Field Stabilizers
# =====================================================================================
@pytest.fixture()
def corm():
    return load_graph_fixture("cormorant_rails")


def test_cormorant_rails_two_magstabs_dps_and_volley_consistency(corm):
    """FIT 1 (isolation): 7x 125mm Railgun I + Antimatter S + 2x Magnetic Field
    Stabilizer I, NO skills — the whole chain is module-only and hand-computable."""
    ship, gun, ammo = corm["Cormorant"], corm["125mm Railgun I"], \
        corm["Antimatter Charge S"]
    mfs = corm["Magnetic Field Stabilizer I"]
    res = evaluate_fit(ship, _guns(gun, 7, ammo) + _lows(mfs, 2),
                       skills=SkillProfile.from_dict(NO_SKILLS))
    off = res.telemetry["offence"]

    shot = _shot(ammo)                                     # 7 kin + 5 therm = 12
    d = _attr(mfs, A.DAMAGE_MULTIPLIER) - 1.0              # +7% (MFS I, attr 64)
    r = _attr(mfs, A.ROF_MULTIPLIER) - 1.0                 # -8% (MFS I, attr 204)
    # damageMultiplier(64) and speed(51) are stackable=false; both MFS postMul
    # contributions are stacking-penalised in their own per-attribute chains.
    dmg = _chain(_attr(gun, A.DAMAGE_MULTIPLIER), [d, d])
    rof_ms = _chain(_attr(gun, A.RATE_OF_FIRE), [r, r])
    volley_per = shot * dmg
    dps_per = volley_per / (rof_ms / 1000.0)

    assert off["total_dps"] == pytest.approx(7 * dps_per, rel=2e-3)
    assert off["turret_dps"] == pytest.approx(7 * dps_per, rel=2e-3)
    assert off["volley"] == pytest.approx(7 * volley_per, rel=2e-3)
    assert len(off["weapons"]) == 7
    for w in off["weapons"]:
        assert w["kind"] == "turret"
        assert w["volley"] == pytest.approx(volley_per, rel=2e-3, abs=0.06)
        # volley/dps consistency: dps must equal volley / evaluated cycle time
        assert w["dps"] == pytest.approx(w["volley"] / (rof_ms / 1000.0),
                                         rel=2e-3, abs=0.06)
    hp = res.telemetry["resources"]["hardpoints"]["turret"]
    assert hp == {"used": 7, "total": int(_attr(ship, A.TURRET_HARDPOINTS))}


def test_cormorant_turret_hardpoint_overflow_diagnostic(corm):
    """FIT 2: 8 railguns on 8 hi slots but only 7 turret hardpoints -> structural
    error diagnostic, status impossible; slot count itself is legal."""
    ship, gun, ammo = corm["Cormorant"], corm["125mm Railgun I"], \
        corm["Antimatter Charge S"]
    res = evaluate_fit(ship, _guns(gun, 8, ammo),
                       skills=SkillProfile.from_dict(NO_SKILLS))
    hard = int(_attr(ship, A.TURRET_HARDPOINTS))           # 7 (attr 102)
    assert int(_attr(ship, A.HI_SLOTS)) == 8               # 8 hi slots: not a slot error
    assert res.status.value == "impossible"
    diags = {d.code: d for d in res.diagnostics}
    assert "turret_hardpoints" in diags
    assert diags["turret_hardpoints"].params == {"have": 8, "cap": hard}
    assert "too_many_modules" not in diags
    assert res.telemetry["resources"]["hardpoints"]["turret"] == \
        {"used": 8, "total": hard}


def test_cormorant_rail_overload_rof_bonus(corm):
    """FIT 3: overheated railguns fire faster by exactly overloadRofBonus (attr 1205,
    effect 3001 postPercent on speed/51, only in the OVERHEATED state). The overload
    contribution is the only penalised entry in its bucket -> full effect."""
    ship, gun, ammo = corm["Cormorant"], corm["125mm Railgun I"], \
        corm["Antimatter Charge S"]
    active = evaluate_fit(ship, _guns(gun, 7, ammo, ModuleState.ACTIVE),
                          skills=SkillProfile.from_dict(NO_SKILLS))
    hot = evaluate_fit(ship, _guns(gun, 7, ammo, ModuleState.OVERHEATED),
                       skills=SkillProfile.from_dict(NO_SKILLS))

    shot = _shot(ammo)
    base_dps = 7 * shot * _attr(gun, A.DAMAGE_MULTIPLIER) \
        / (_attr(gun, A.RATE_OF_FIRE) / 1000.0)
    oh = _attr(gun, A.OVERLOAD_ROF_BONUS) / 100.0          # -15% cycle time
    assert active.telemetry["offence"]["total_dps"] == pytest.approx(base_dps, rel=2e-3)
    assert hot.telemetry["offence"]["total_dps"] == pytest.approx(
        base_dps / (1.0 + oh), rel=2e-3)
    # Volley is unchanged by a pure rate-of-fire overload.
    assert hot.telemetry["offence"]["volley"] == pytest.approx(
        active.telemetry["offence"]["volley"], rel=2e-3)


def test_cormorant_ranges_all_v_traits_and_gunnery_skills(corm):
    """FIT 4 (realistic, All V): optimal/falloff/tracking telemetry vs hand values.
    Optimal: gun maxRange x ammo weaponRangeMultiplier (preMul), then postPercent by
    the hull's +50% small-hybrid role bonus (attr 351, not level-scaled), the
    Caldari Destroyer trait (attr 734 %/lvl) and Sharpshooter (attr 294 %/lvl); all
    ship/skill sources -> stacking-exempt."""
    ship, gun, ammo = corm["Cormorant"], corm["125mm Railgun I"], \
        corm["Antimatter Charge S"]
    res = evaluate_fit(ship, _guns(gun, 1, ammo))          # omniscient() default
    w = res.telemetry["offence"]["weapons"][0]

    lvl = 5
    optimal = _attr(gun, A.OPTIMAL_RANGE) * _attr(ammo, WEAPON_RANGE_MULT) \
        * (1 + _attr(ship, SHIP_ROLE_MAXRANGE) / 100.0) \
        * (1 + _attr(ship, SHIP_BONUS_CD1) * lvl / 100.0) \
        * (1 + _attr(SHARPSHOOTER, RANGE_SKILL_BONUS) * lvl / 100.0)
    falloff = _attr(gun, A.FALLOFF) \
        * (1 + _attr(TRAJECTORY_ANALYSIS, FALLOFF_BONUS) * lvl / 100.0)
    tracking = _attr(gun, A.TRACKING_SPEED) \
        * (1 + _attr(ship, SHIP_BONUS_CD2) * lvl / 100.0) \
        * (1 + _attr(MOTION_PREDICTION, TRACKING_BONUS) * lvl / 100.0)
    assert w["optimal_m"] == pytest.approx(optimal, rel=2e-3)   # 9000*0.5*1.5*1.5*1.25
    assert w["falloff_m"] == pytest.approx(falloff, rel=2e-3)   # 5000*1.25
    assert w["tracking"] == pytest.approx(tracking, rel=2e-3)   # 89.25*1.5*1.25


def test_t2_rail_spec_skill_levels_0_3_5(corm):
    """FIT 5: 125mm Railgun II with Small Railgun Specialization at 0/3/5 — only the
    spec's +2%/lvl damage (attr 292 on skill 11082, effect 1006) may change; RoF and
    ranges stay fixed. Base skills held constant: Small Hybrid Turret 5 (+5%/lvl
    damage, attr 292 on 3301), Gunnery 1 (-2%/lvl RoF, attr 441 on 3300)."""
    ship, gun, ammo = corm["Cormorant"], corm["125mm Railgun II"], \
        corm["Antimatter Charge S"]
    shot = _shot(ammo)
    sht_pct = _attr(SMALL_HYBRID_TURRET, DMG_BONUS)        # 5 %/lvl
    spec_pct = _attr(SMALL_RAILGUN_SPEC, DMG_BONUS)        # 2 %/lvl
    gun_rof = _attr(gun, A.RATE_OF_FIRE) \
        * (1 + _attr(GUNNERY, TURRET_SPEED_BONUS) * 1 / 100.0)   # Gunnery I: -2%

    seen = {}
    for spec_lvl in (0, 3, 5):
        skills = SkillProfile.from_dict({SMALL_HYBRID_TURRET: 5, GUNNERY: 1,
                                         SMALL_RAILGUN_SPEC: spec_lvl})
        res = evaluate_fit(ship, _guns(gun, 1, ammo), skills=skills)
        w = res.telemetry["offence"]["weapons"][0]
        dmg = _attr(gun, A.DAMAGE_MULTIPLIER) \
            * (1 + sht_pct * 5 / 100.0) * (1 + spec_pct * spec_lvl / 100.0)
        expected = shot * dmg / (gun_rof / 1000.0)
        assert res.telemetry["offence"]["total_dps"] == pytest.approx(
            expected, rel=2e-3, abs=0.06)
        seen[spec_lvl] = w
    # Only the damage-side attrs may move with the spec level.
    for lvl in (3, 5):
        assert seen[lvl]["optimal_m"] == seen[0]["optimal_m"]
        assert seen[lvl]["falloff_m"] == seen[0]["falloff_m"]
        assert seen[lvl]["tracking"] == seen[0]["tracking"]


def test_offline_and_online_guns_contribute_zero_dps(corm):
    """FIT 6: one ACTIVE gun fires; a merely-ONLINE gun and an OFFLINE gun add zero
    DPS. Hardpoint accounting still sees all three turrets; CPU ignores the offline
    one (Weapon Upgrades untrained -> CPU needs are base values)."""
    ship, gun, ammo = corm["Cormorant"], corm["125mm Railgun I"], \
        corm["Antimatter Charge S"]
    mods = [ModuleInput(type_id=gun, slot=SlotKind.HIGH, state=ModuleState.ACTIVE,
                        charge_type_id=ammo),
            ModuleInput(type_id=gun, slot=SlotKind.HIGH, state=ModuleState.ONLINE,
                        charge_type_id=ammo),
            ModuleInput(type_id=gun, slot=SlotKind.HIGH, state=ModuleState.OFFLINE,
                        charge_type_id=ammo)]
    res = evaluate_fit(ship, mods, skills=SkillProfile.from_dict(NO_SKILLS))
    off = res.telemetry["offence"]

    expected = _shot(ammo) * _attr(gun, A.DAMAGE_MULTIPLIER) \
        / (_attr(gun, A.RATE_OF_FIRE) / 1000.0)            # 12*2.2/3.25s, one gun
    assert off["total_dps"] == pytest.approx(expected, rel=2e-3, abs=0.06)
    assert len(off["weapons"]) == 1                        # only the ACTIVE gun fires
    assert res.telemetry["resources"]["hardpoints"]["turret"]["used"] == 3
    assert res.telemetry["resources"]["cpu"]["used"] == pytest.approx(
        2 * _attr(gun, A.CPU_USAGE), rel=1e-6)             # offline gun costs no CPU


# =====================================================================================
# Hybrid medium — Moa: weapon rig + damage mods sharing ONE stacking chain
# =====================================================================================
def test_moa_rig_and_magstabs_share_one_rof_chain():
    """FIT 7: 4x 250mm Railgun II + Antimatter M + 3x Magnetic Field Stabilizer II +
    Medium Hybrid Burst Aerator I, NO skills. All four RoF contributions (3 MFS
    speedMultiplier 0.895, rig speedMultiplier 0.9) post-multiply the gun's
    speed(51) — one negative penalised chain of four; damage chain is the 3 MFS."""
    ids = load_graph_fixture("moa_rails")
    ship, gun, ammo = ids["Moa"], ids["250mm Railgun II"], ids["Antimatter Charge M"]
    mfs, rig = ids["Magnetic Field Stabilizer II"], ids["Medium Hybrid Burst Aerator I"]
    mods = _guns(gun, 4, ammo) + _lows(mfs, 3) + \
        [ModuleInput(type_id=rig, slot=SlotKind.RIG, state=ModuleState.ONLINE)]
    res = evaluate_fit(ship, mods, skills=SkillProfile.from_dict(NO_SKILLS))

    shot = _shot(ammo)                                     # 14 kin + 10 therm = 24
    d = _attr(mfs, A.DAMAGE_MULTIPLIER) - 1.0              # +10% (MFS II)
    r = _attr(mfs, A.ROF_MULTIPLIER) - 1.0                 # -10.5% (MFS II)
    rr = _attr(rig, A.ROF_MULTIPLIER) - 1.0                # -10% (Burst Aerator I)
    dmg = _chain(_attr(gun, A.DAMAGE_MULTIPLIER), [d, d, d])
    # One chain, sorted by magnitude: 10.5%, 10.5%, 10.5%, then the rig's 10%.
    rof_ms = _chain(_attr(gun, A.RATE_OF_FIRE), [r, r, r, rr])
    expected = 4 * shot * dmg / (rof_ms / 1000.0)
    assert res.telemetry["offence"]["total_dps"] == pytest.approx(expected, rel=2e-3)
    # The Burst Aerator's drawback (attr 1138 = +10%, effect 2707 postPercent) raises
    # every hybrid gun's power need: 4 x 208 x 1.10 + 3 x 1 = 918.2 MW — over the
    # Moa's base 850 MW without fitting skills, so the fit is over-resources (the
    # telemetry above is still computed).
    drawback = _attr(rig, 1138) / 100.0
    pg_used = 4 * _attr(gun, A.POWER_USAGE) * (1 + drawback) \
        + 3 * _attr(mfs, A.POWER_USAGE)
    assert res.telemetry["resources"]["powergrid"]["used"] == pytest.approx(
        pg_used, rel=1e-3)
    assert res.status.value == "over_resources"
    assert any(d.code == "powergrid_exceeded" for d in res.diagnostics)


# =====================================================================================
# Energy — Omen + pulse lasers + Heat Sinks + crystal swap
# =====================================================================================
@pytest.fixture()
def omen():
    return load_graph_fixture("omen_lasers")


# Regression test for the (fixed) laser-detection defect: energy turrets carry
# effect 10 (targetAttack); detection now uses A.TURRET_EFFECTS = {10, 34}.
def test_omen_pulse_lasers_heat_sink_stacking(omen):
    """FIT 8: 5x Focused Medium Pulse Laser I + Multifrequency M + 2x Heat Sink II,
    NO skills. Same maths as the hybrid/projectile damage mods: Heat Sink II
    damageMultiplier 1.1 (penalised chain of two on attr 64), speedMultiplier 0.895
    (penalised chain of two on attr 51)."""
    ship, gun = omen["Omen"], omen["Focused Medium Pulse Laser I"]
    ammo, hs = omen["Multifrequency M"], omen["Heat Sink II"]
    res = evaluate_fit(ship, _guns(gun, 5, ammo) + _lows(hs, 2),
                       skills=SkillProfile.from_dict(NO_SKILLS))
    off = res.telemetry["offence"]

    shot = _shot(ammo)                                     # 14 em + 10 therm = 24
    d = _attr(hs, A.DAMAGE_MULTIPLIER) - 1.0               # +10%
    r = _attr(hs, A.ROF_MULTIPLIER) - 1.0                  # -10.5%
    dmg = _chain(_attr(gun, A.DAMAGE_MULTIPLIER), [d, d])
    rof_ms = _chain(_attr(gun, A.RATE_OF_FIRE), [r, r])
    expected = 5 * shot * dmg / (rof_ms / 1000.0)
    assert off["total_dps"] == pytest.approx(expected, rel=2e-3)
    assert len(off["weapons"]) == 5


# Regression test for the (fixed) laser-detection defect: energy turrets carry
# effect 10 (targetAttack); detection now uses A.TURRET_EFFECTS = {10, 34}.
def test_omen_frequency_crystal_changes_optimal(omen):
    """FIT 9: swapping Multifrequency M (weaponRangeMultiplier 0.5) for Radio M (1.6)
    rescales the laser's optimal through the ammoInfluenceRange preMul; the gun's own
    maxRange is unchanged (no skills, so no other optimal source)."""
    ship, gun = omen["Omen"], omen["Focused Medium Pulse Laser I"]
    for ammo_name in ("Multifrequency M", "Radio M"):
        ammo = omen[ammo_name]
        res = evaluate_fit(ship, _guns(gun, 1, ammo),
                           skills=SkillProfile.from_dict(NO_SKILLS))
        w = res.telemetry["offence"]["weapons"][0]
        assert w["optimal_m"] == pytest.approx(
            _attr(gun, A.OPTIMAL_RANGE) * _attr(ammo, WEAPON_RANGE_MULT), rel=2e-3)
        assert w["falloff_m"] == pytest.approx(_attr(gun, A.FALLOFF), rel=2e-3)


# =====================================================================================
# Projectile medium — Rupture + 425mm autocannons + Gyrostabilizers
# =====================================================================================
@pytest.fixture()
def rupture():
    return load_graph_fixture("rupture_ac")


def test_rupture_ac_gyros_dps_and_range_telemetry(rupture):
    """FIT 10 (isolation): 4x 425mm AutoCannon I + EMP M + 2x Gyrostabilizer II, NO
    skills. Also checks the range/tracking telemetry against EMP M's multipliers
    (weaponRange 0.5, tracking 1.0, no falloff attr) and the damage distribution
    straight from the charge's per-type damage split."""
    ship, gun, ammo = rupture["Rupture"], rupture["425mm AutoCannon I"], \
        rupture["EMP M"]
    gyro = rupture["Gyrostabilizer II"]
    res = evaluate_fit(ship, _guns(gun, 4, ammo) + _lows(gyro, 2),
                       skills=SkillProfile.from_dict(NO_SKILLS))
    off = res.telemetry["offence"]

    shot = _shot(ammo)                                     # 18 em + 4 expl + 2 kin = 24
    d = _attr(gyro, A.DAMAGE_MULTIPLIER) - 1.0             # +10% (Gyro II)
    r = _attr(gyro, A.ROF_MULTIPLIER) - 1.0                # -10.5%
    dmg = _chain(_attr(gun, A.DAMAGE_MULTIPLIER), [d, d])
    rof_ms = _chain(_attr(gun, A.RATE_OF_FIRE), [r, r])
    assert off["total_dps"] == pytest.approx(
        4 * shot * dmg / (rof_ms / 1000.0), rel=2e-3)

    w = off["weapons"][0]
    assert w["optimal_m"] == pytest.approx(
        _attr(gun, A.OPTIMAL_RANGE) * _attr(ammo, WEAPON_RANGE_MULT), rel=2e-3)
    assert w["falloff_m"] == pytest.approx(_attr(gun, A.FALLOFF), rel=2e-3)
    assert w["tracking"] == pytest.approx(
        _attr(gun, A.TRACKING_SPEED) * _attr(ammo, TRACKING_MULT), rel=2e-3)
    # Damage split is the charge's own em/expl/kin proportions.
    dist = off["damage_distribution"]
    assert dist["em"] == pytest.approx(100 * _attr(ammo, A.EM_DAMAGE) / shot, abs=0.1)
    assert dist["explosive"] == pytest.approx(
        100 * _attr(ammo, A.EXPLOSIVE_DAMAGE) / shot, abs=0.1)
    assert dist["thermal"] == 0.0


def test_rupture_barrage_falloff_and_tracking_multipliers(rupture):
    """FIT 11: 425mm AutoCannon II + Barrage M, NO skills. Barrage carries
    fallofMultiplier 1.5 (attr 517, effect 599 postMul falloff), tracking 0.75
    (attr 244, effect 600) and weaponRangeMultiplier 1.0 — charge source, exempt."""
    ship, gun, ammo = rupture["Rupture"], rupture["425mm AutoCannon II"], \
        rupture["Barrage M"]
    res = evaluate_fit(ship, _guns(gun, 1, ammo),
                       skills=SkillProfile.from_dict(NO_SKILLS))
    w = res.telemetry["offence"]["weapons"][0]

    assert w["falloff_m"] == pytest.approx(
        _attr(gun, A.FALLOFF) * _attr(ammo, FALLOFF_MULT), rel=2e-3)       # 10836*1.5
    assert w["tracking"] == pytest.approx(
        _attr(gun, A.TRACKING_SPEED) * _attr(ammo, TRACKING_MULT), rel=2e-3)
    assert w["optimal_m"] == pytest.approx(
        _attr(gun, A.OPTIMAL_RANGE) * _attr(ammo, WEAPON_RANGE_MULT), rel=2e-3)
    assert w["volley"] == pytest.approx(
        _shot(ammo) * _attr(gun, A.DAMAGE_MULTIPLIER), rel=2e-3, abs=0.06)


def test_rupture_ac_overload_damage_bonus(rupture):
    """FIT 12: autocannons overload for damage, not RoF: overloadDamageModifier
    (attr 1210, effect 3025 postPercent on damageMultiplier/64, OVERHEATED only).
    Sole entry of its penalised bucket -> full +15%."""
    ship, gun, ammo = rupture["Rupture"], rupture["425mm AutoCannon I"], \
        rupture["EMP M"]
    active = evaluate_fit(ship, _guns(gun, 4, ammo, ModuleState.ACTIVE),
                          skills=SkillProfile.from_dict(NO_SKILLS))
    hot = evaluate_fit(ship, _guns(gun, 4, ammo, ModuleState.OVERHEATED),
                       skills=SkillProfile.from_dict(NO_SKILLS))
    oh = _attr(gun, 1210) / 100.0                          # overloadDamageModifier +15%
    a_off, h_off = active.telemetry["offence"], hot.telemetry["offence"]
    assert h_off["total_dps"] == pytest.approx(a_off["total_dps"] * (1 + oh), rel=3e-3)
    assert h_off["volley"] == pytest.approx(a_off["volley"] * (1 + oh), rel=3e-3)


def test_rupture_all_v_realistic_t2_fit(rupture):
    """FIT 13 (realistic, All V): 4x 425mm AutoCannon II + EMP M + 2x Gyrostabilizer
    II + Medium Projectile Burst Aerator I. Exempt postPercent sources: Medium
    Projectile Turret +5%/lvl, Medium AC Spec +2%/lvl, Surgical Strike +3%/lvl and
    the Rupture trait +10%/lvl damage (ship attr 489, scaled by Minmatar Cruiser);
    Gunnery -2%/lvl and Rapid Firing -4%/lvl on RoF. Penalised module chains: gyro
    damage [+10%, +10%]; RoF [gyro -10.5%, -10.5%, rig -10%] in ONE chain."""
    ship, gun, ammo = rupture["Rupture"], rupture["425mm AutoCannon II"], \
        rupture["EMP M"]
    gyro, rig = rupture["Gyrostabilizer II"], \
        rupture["Medium Projectile Burst Aerator I"]
    mods = _guns(gun, 4, ammo) + _lows(gyro, 2) + \
        [ModuleInput(type_id=rig, slot=SlotKind.RIG, state=ModuleState.ONLINE)]
    res = evaluate_fit(ship, mods)                         # omniscient() default
    off = res.telemetry["offence"]

    lvl = 5
    shot = _shot(ammo)
    skill_dmg = (1 + _attr(MEDIUM_PROJECTILE_TURRET, DMG_BONUS) * lvl / 100.0) \
        * (1 + _attr(MEDIUM_AC_SPEC, DMG_BONUS) * lvl / 100.0) \
        * (1 + _attr(SURGICAL_STRIKE, DMG_BONUS) * lvl / 100.0) \
        * (1 + _attr(ship, SHIP_BONUS_MC) * lvl / 100.0)
    g = _attr(gyro, A.DAMAGE_MULTIPLIER) - 1.0
    dmg = _chain(_attr(gun, A.DAMAGE_MULTIPLIER) * skill_dmg, [g, g])
    skill_rof = (1 + _attr(GUNNERY, TURRET_SPEED_BONUS) * lvl / 100.0) \
        * (1 + _attr(RAPID_FIRING, ROF_BONUS) * lvl / 100.0)
    gr = _attr(gyro, A.ROF_MULTIPLIER) - 1.0
    rr = _attr(rig, A.ROF_MULTIPLIER) - 1.0
    rof_ms = _chain(_attr(gun, A.RATE_OF_FIRE) * skill_rof, [gr, gr, rr])
    assert off["total_dps"] == pytest.approx(4 * shot * dmg / (rof_ms / 1000.0),
                                             rel=2e-3)

    w = off["weapons"][0]
    tracking = _attr(gun, A.TRACKING_SPEED) * _attr(ammo, TRACKING_MULT) \
        * (1 + _attr(ship, SHIP_BONUS_MC2) * lvl / 100.0) \
        * (1 + _attr(MOTION_PREDICTION, TRACKING_BONUS) * lvl / 100.0)
    optimal = _attr(gun, A.OPTIMAL_RANGE) * _attr(ammo, WEAPON_RANGE_MULT) \
        * (1 + _attr(SHARPSHOOTER, RANGE_SKILL_BONUS) * lvl / 100.0)
    falloff = _attr(gun, A.FALLOFF) \
        * (1 + _attr(TRAJECTORY_ANALYSIS, FALLOFF_BONUS) * lvl / 100.0)
    assert w["tracking"] == pytest.approx(tracking, rel=2e-3)
    assert w["optimal_m"] == pytest.approx(optimal, rel=2e-3)
    assert w["falloff_m"] == pytest.approx(falloff, rel=2e-3)
    assert not res.missing_skills                          # All V: nothing missing
