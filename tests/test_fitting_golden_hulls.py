"""Single-hull END-TO-END golden fits (engine v2, real SDE slices, hand-derived numbers).

These two whole-ship fits restore the belt-and-braces coverage the deleted v1 goldens
(test_fitting_loki_golden / test_fitting_vagabond_golden) used to give, now on the v2
graph engine with real fixtures. Unlike the per-mechanic goldens, each fit here drives
EVERY telemetry family at once (offence + defence + capacitor + mobility) so a
regression that only shows up when the whole chain runs together is caught.

Fixtures (extracted through scripts/tochas_lab_extract_fixture.py from the live SDE):
  * tests/fixtures/fitting/vagabond_hull.json — Minmatar HAC + 220mm ACs + Barrage +
    gyros + 10MN AB + Large Shield Extender + nanofiber/istab + a shield rig.
  * tests/fixtures/fitting/loki_hull.json — Minmatar T3C + its 4 subsystems + Heavy
    Assault Missile Launchers + Scourge Rage + shield hardener/extender + BCS + a burst.

Every expected value is DERIVED IN THE TEST from the slice's base attributes plus
documented EVE mechanics — NEVER read back from the engine:

* stacking S(i) = exp(-(i/2.67)^2) per (target attribute, operator) bucket, magnitudes
  sorted descending, positive/negative chains separate, ONLY when the target attribute
  is stackable=false and the source is a module/rig (ship, charge, skill, SUBSYSTEM
  sources are stacking-exempt → plain multipliers);
* skill/ship-trait/subsystem per-level bonuses are the bonus attribute's own value
  (read from the slice) times the trained level — CCP encodes the per-level scaling as a
  preMul of the bonus attribute by attr 280 (verified in both slices: 489/692/693 on the
  Vagabond and 1449/1522/1534 on the Loki offensive subsystem carry the 280 preMul), so
  at All V a bonus attribute of B contributes B*5;
* stacking penalty peak = 2.5*C/tau; cap equilibrium s(1-s) = drain*tau/(10*C);
  align = -ln(0.25)*mass*agility/1e6; prop thrust
  v = v_base*(1 + (speedFactor/100)*(speedBoostFactor/mass));
* post-Aegis missile application min(1, sig/Er, ((sig/Er)*(Ev/Vt))^drf), drf = attr 1353.

Tolerances mirror the existing goldens (rel=2e-3; abs=0.06 where 1-dp rounding dominates).
"""
from __future__ import annotations

import math

import pytest

from apps.fitting.engine import attributes as A
from apps.fitting.engine.types import (
    BoostInput,
    DamageProfileInput,
    ModuleInput,
    ModuleState,
    OperatingProfile,
    SkillProfile,
    SlotKind,
    TargetProfile,
)

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db

S1 = math.exp(-((1 / 2.67) ** 2))       # 0.8691… second stacked module effectiveness
LN4 = -math.log(0.25)                   # align-time constant
NO_SKILLS = SkillProfile.from_dict({})

# --- Dogma attribute ids used beyond attributes.py's named set -----------------------
DMG_BONUS = 292              # damageMultiplierBonus (%/lvl, gunnery + missile dmg skills)
ROF_BONUS = 293              # rofBonus (%/lvl, Rapid Firing / Rapid Launch / missile skills)
TURRET_SPEED_BONUS = 441     # Gunnery turretSpeeBonus (%/lvl, RoF)
WEAPON_RANGE_MULT = 120      # ammo weaponRangeMultiplier -> preMul gun maxRange (54)
TRACKING_MULT = 244          # ammo trackingSpeedMultiplier -> postMul trackingSpeed (160)
FALLOFF_MULT = 517           # ammo fallofMultiplier -> postMul falloff (158)
ATTR_DURATION = 73
ATTR_CAP_NEED = 6
ATTR_RELOAD = 1795
ATTR_CAPACITY = 38
ATTR_VOLUME = 161
ATTR_DRF = 1353              # aoeDamageReductionFactor (the application exponent itself)
ATTR_SHIELD_CAP_ADD = 72     # Large Shield Extender capacityBonus (flat, modAdd)
ATTR_SHIELD_RIG_HP_PCT = 337 # Core Defense Field Extender shieldCapacityBonus (%) / Shield Mgmt
ATTR_SIG_ADD = 983           # Large Shield Extender signatureRadiusAdd (flat, modAdd)
ATTR_RIG_DRAWBACK = 1138     # navigation/shield rig sig drawback (%)
ATTR_RIGGING_SKILL_PCT = 1139  # Shield Rigging: reduces group-774 rig drawback (%/lvl)
ATTR_SIG_BONUS = 554         # istab signatureRadiusBonus (%)
ATTR_VELOCITY_BONUS = 1076   # nanofiber implantBonusVelocity (%)
ATTR_AGILITY_MULT = 169      # nanofiber/istab agilityMultiplier (%)
ATTR_SPEED_FACTOR = 20       # AB speedFactor (% velocity bonus)
ATTR_SPEED_BOOST = 567       # AB speedBoostFactor (thrust)
ATTR_MASS_ADD = 796          # AB massAddition
ATTR_STRUCT_HP_ADD = 2688    # subsystem structureHPBonusAdd (flat, modAdd to hp 9)
ATTR_STRUCT_HP_PCT = 327     # Mechanics structureHpBonus (%/lvl)
ATTR_SHIELD_CAP_SUB_ADD = 263  # defensive subsystem shieldCapacity passive add
ATTR_BCS_DMG_BONUS = 213     # BCS missileDamageMultiplierBonus (preMul on char 212)
ATTR_SUB_OFF_ROF = 1522      # offensive subsystem missile-RoF bonus (%/subsystem lvl)

# --- Skill type ids (category 16; the per-level % is read from the skill's own dogma) ----
MEDIUM_PROJECTILE_TURRET = 3305   # +5%/lvl medium projectile damage (attr 292)
MEDIUM_AC_SPEC = 12208            # +2%/lvl T2 AC damage (attr 292)
SURGICAL_STRIKE = 3315            # +3%/lvl turret damage (attr 292)
GUNNERY = 3300                    # -2%/lvl turret RoF time (attr 441)
RAPID_FIRING = 3310               # -4%/lvl turret RoF time (attr 293)
NAVIGATION = 3449                 # +5%/lvl max velocity
ACCELERATION_CONTROL = 3452       # +5%/lvl prop speedFactor
AFTERBURNER = 3450                # -5%/lvl AB duration, -10%/lvl AB cap need
FUEL_CONSERVATION = 3451          # -10%/lvl AB cap need
EVASIVE_MANEUVERING = 3453        # -5%/lvl agility
SPACESHIP_COMMAND = 3327          # -2%/lvl agility
CAP_MANAGEMENT = 3418             # +5%/lvl capacitor capacity
CAP_SYSTEMS_OPERATION = 3417      # -5%/lvl capacitor recharge time
SHIELD_MANAGEMENT = 3419          # +5%/lvl shield capacity (attr 337)
SHIELD_RIGGING = 26261            # -10%/lvl group-774 rig drawback (attr 1139)
MECHANICS = 3392                  # +5%/lvl structure HP (attr 327)
# Missiles.
HEAVY_ASSAULT_MISSILES = 25719    # +5%/lvl HAM damage (attr 292)
HAM_SPECIALIZATION = 25718        # -2%/lvl HAM RoF (attr 293, selfRof)
MISSILE_LAUNCHER_OPERATION = 3319 # -2%/lvl missile RoF (attr 293)
RAPID_LAUNCH = 21071              # -3%/lvl missile RoF (attr 293)
WARHEAD_UPGRADES = 20315          # +2%/lvl missile damage (attr 292)

# Ship-trait / subsystem-bonus attributes (per-level values on the hull/subsystem itself).
VAGA_TRAIT_ROF_MC = 489           # shipBonusMC = -5 (×Minmatar Cruiser lvl): medium proj RoF
VAGA_TRAIT_DMG_HAC = 693          # eliteBonusHeavyGunship2 = +5 (×HAC lvl): medium proj damage
VAGA_TRAIT_FALLOFF_HAC = 692      # eliteBonusHeavyGunship1 = +12.5 (×HAC lvl): medium proj falloff


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _has(type_id, attr_id) -> bool:
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.filter(type_id=type_id, attribute_id=attr_id).exists()


def _chain(base: float, deltas: list[float]) -> float:
    """One stacking-penalised multiplicative chain (single operator bucket, same sign):
    magnitudes sorted descending, the i-th factor scaled by S1**(i*i)."""
    v = base
    for i, dlt in enumerate(sorted(deltas, key=abs, reverse=True)):
        v *= 1.0 + dlt * (S1 ** (i * i))
    return v


def _shot(charge) -> float:
    return sum(_attr(charge, a) for a in
               (A.EM_DAMAGE, A.THERMAL_DAMAGE, A.KINETIC_DAMAGE, A.EXPLOSIVE_DAMAGE)
               if _has(charge, a))


def _evaluate_with_boosts(ship_type_id, modules, boosts, skills=None, op=None):
    """The shared evaluate_fit helper does not carry fleet boosts; build the FitInput
    directly (omniscient by default, prop off) so the boost case exercises the same
    production path (ORM provider + engine v2)."""
    from apps.fitting.engine.adapter import FittingEngine
    from apps.fitting.engine.types import FitInput

    engine = FittingEngine()
    fit = FitInput(ship_type_id=ship_type_id, modules=tuple(modules), boosts=tuple(boosts))
    return engine.evaluate(fit, skills or SkillProfile.omniscient(),
                           op or OperatingProfile(propulsion_active=False))


# =====================================================================================
# FIT A — Vagabond (Minmatar HAC): 5x 220mm AutoCannon II + Barrage M, 2x Gyrostabilizer
# II, 10MN Afterburner II, Large Shield Extender II, Nanofiber + Inertial Stabilizer,
# Medium Core Defense Field Extender rig.  All-V, prop running (except the isolation fit).
# =====================================================================================
@pytest.fixture()
def vaga():
    return load_graph_fixture("vagabond_hull")


def _vaga_guns(ids, n, state=ModuleState.ACTIVE):
    gun, ammo = ids["220mm Vulcan AutoCannon II"], ids["Barrage M"]
    return [ModuleInput(type_id=gun, slot=SlotKind.HIGH, state=state, charge_type_id=ammo)
            for _ in range(n)]


def _vaga_fit(ids, prop=True):
    """The realistic whole fit used by the All-V assertions."""
    gyro = ids["Gyrostabilizer II"]
    ab, lse = ids["10MN Afterburner II"], ids["Large Shield Extender II"]
    nano, istab = ids["Nanofiber Internal Structure II"], ids["Inertial Stabilizers II"]
    rig = ids["Medium Core Defense Field Extender I"]
    mods = _vaga_guns(ids, 5)
    mods += [ModuleInput(type_id=gyro, slot=SlotKind.LOW, state=ModuleState.ONLINE)
             for _ in range(2)]
    mods += [ModuleInput(type_id=ab, slot=SlotKind.MED,
                         state=ModuleState.ACTIVE if prop else ModuleState.ONLINE),
             ModuleInput(type_id=lse, slot=SlotKind.MED, state=ModuleState.ONLINE),
             ModuleInput(type_id=nano, slot=SlotKind.LOW, state=ModuleState.ONLINE),
             ModuleInput(type_id=istab, slot=SlotKind.LOW, state=ModuleState.ONLINE),
             ModuleInput(type_id=rig, slot=SlotKind.RIG, state=ModuleState.ONLINE)]
    return mods


def test_vagabond_isolation_dps_no_skills(vaga):
    """Module-only anchor: 5x 220mm AutoCannon II + Barrage + 2x Gyrostabilizer II, NO
    skills — every ship trait and skill is level 0, so only the two gyros move the numbers.
    Damage chain [+10%, +10%] on damageMultiplier(64); RoF chain [-10.5%, -10.5%] on
    speed(51); both are stackable=false, module sources -> penalised together. Also pins
    the Barrage range/tracking multipliers with no skill layer to muddy them."""
    gun, ammo = vaga["220mm Vulcan AutoCannon II"], vaga["Barrage M"]
    gyro = vaga["Gyrostabilizer II"]
    mods = _vaga_guns(vaga, 5) + [
        ModuleInput(type_id=gyro, slot=SlotKind.LOW, state=ModuleState.ONLINE)
        for _ in range(2)]
    res = evaluate_fit(vaga["Vagabond"], mods, skills=NO_SKILLS)
    off = res.telemetry["offence"]

    shot = _shot(ammo)                                     # 12 expl + 10 kin = 22
    g = _attr(gyro, A.DAMAGE_MULTIPLIER) - 1.0             # +10% (Gyro II, attr 64)
    r = _attr(gyro, A.ROF_MULTIPLIER) - 1.0                # -10.5% (attr 204)
    dmg = _chain(_attr(gun, A.DAMAGE_MULTIPLIER), [g, g])
    rof_ms = _chain(_attr(gun, A.RATE_OF_FIRE), [r, r])
    volley_per = shot * dmg
    dps_per = volley_per / (rof_ms / 1000.0)
    assert off["total_dps"] == pytest.approx(5 * dps_per, rel=2e-3)
    assert off["turret_dps"] == pytest.approx(5 * dps_per, rel=2e-3)
    assert off["volley"] == pytest.approx(5 * volley_per, rel=2e-3)
    assert len(off["weapons"]) == 5

    # Barrage's own multipliers (charge source, stacking-exempt): optimal x1.0,
    # falloff x1.5, tracking x0.75; no skills so nothing else touches range/tracking.
    w = off["weapons"][0]
    assert w["optimal_m"] == pytest.approx(
        _attr(gun, A.OPTIMAL_RANGE) * _attr(ammo, WEAPON_RANGE_MULT), rel=2e-3)
    assert w["falloff_m"] == pytest.approx(
        _attr(gun, A.FALLOFF) * _attr(ammo, FALLOFF_MULT), rel=2e-3)
    assert w["tracking"] == pytest.approx(
        _attr(gun, A.TRACKING_SPEED) * _attr(ammo, TRACKING_MULT), rel=2e-3)
    # Turret hardpoints: 5 guns on the hull's 5 turret slots (attr 102).
    hp = res.telemetry["resources"]["hardpoints"]["turret"]
    assert (hp["used"], hp["total"]) == (5, int(_attr(vaga["Vagabond"], A.TURRET_HARDPOINTS)))


def test_vagabond_all_v_dps_volley_sustained(vaga):
    """Full-chain (a) total_dps, (b) total_sustained_dps and (c) volley, All V.

    Damage: base damageMultiplier x Medium Projectile Turret(+5%/lvl) x Medium AC
    Spec(+2%/lvl) x Surgical Strike(+3%/lvl) x the Vagabond HAC damage trait
    (eliteBonusHeavyGunship2 = +5 x HAC V), then the two gyros penalised [+10%,+10%].
    RoF: base speed x Gunnery(-2%/lvl) x Rapid Firing(-4%/lvl) x the Minmatar Cruiser RoF
    trait (shipBonusMC = -5 x MC V), then the two gyros penalised [-10.5%,-10.5%].
    All skill/ship-trait sources are stacking-exempt -> plain multipliers."""
    gun, ammo, gyro = (vaga["220mm Vulcan AutoCannon II"], vaga["Barrage M"],
                       vaga["Gyrostabilizer II"])
    res = evaluate_fit(vaga["Vagabond"], _vaga_fit(vaga),
                       op=OperatingProfile(propulsion_active=True))  # omniscient default
    off = res.telemetry["offence"]
    assert res.status.value in ("valid", "warnings")

    shot = _shot(ammo)                                     # 22
    lvl = 5
    skill_dmg = ((1 + _attr(MEDIUM_PROJECTILE_TURRET, DMG_BONUS) * lvl / 100.0)
                 * (1 + _attr(MEDIUM_AC_SPEC, DMG_BONUS) * lvl / 100.0)
                 * (1 + _attr(SURGICAL_STRIKE, DMG_BONUS) * lvl / 100.0)
                 * (1 + _attr(vaga["Vagabond"], VAGA_TRAIT_DMG_HAC) * lvl / 100.0))
    g = _attr(gyro, A.DAMAGE_MULTIPLIER) - 1.0             # +10%
    dmg = _chain(_attr(gun, A.DAMAGE_MULTIPLIER) * skill_dmg, [g, g])
    skill_rof = ((1 + _attr(GUNNERY, TURRET_SPEED_BONUS) * lvl / 100.0)
                 * (1 + _attr(RAPID_FIRING, ROF_BONUS) * lvl / 100.0)
                 * (1 + _attr(vaga["Vagabond"], VAGA_TRAIT_ROF_MC) * lvl / 100.0))
    r = _attr(gyro, A.ROF_MULTIPLIER) - 1.0                # -10.5%
    rof_ms = _chain(_attr(gun, A.RATE_OF_FIRE) * skill_rof, [r, r])
    volley_per = shot * dmg
    dps_per = volley_per / (rof_ms / 1000.0)
    assert off["total_dps"] == pytest.approx(5 * dps_per, rel=2e-3)
    assert off["volley"] == pytest.approx(5 * volley_per, rel=2e-3)

    # Sustained (b): magazine = floor(capacity / charge volume), chargeRate 1 -> that many
    # shots, then reloadTime(1795) before firing again. sustained = mag*volley / (mag*cycle
    # + reload), strictly below burst.
    mag = math.floor(_attr(gun, ATTR_CAPACITY) / _attr(ammo, ATTR_VOLUME))   # 2/0.0125 = 160
    assert mag == 160
    reload_s = _attr(gun, ATTR_RELOAD) / 1000.0
    time_to_empty = mag * (rof_ms / 1000.0)
    sustained_per = mag * volley_per / (time_to_empty + reload_s)
    assert off["total_sustained_dps"] == pytest.approx(5 * sustained_per, rel=2e-3)
    assert off["total_sustained_dps"] < off["total_dps"]
    # Per-weapon reload-aware fields agree with the fit total.
    assert off["weapons"][0]["magazine_shots"] == mag
    assert off["weapons"][0]["reload_s"] == pytest.approx(reload_s, rel=2e-3)


def test_vagabond_shield_layer_hp_ehp_and_resist(vaga):
    """(d) Shield layer HP + EHP + one resist channel, All V.

    Shield HP: (hull shieldCapacity + Large Shield Extender flat add) then the shield rig
    +15% and Shield Management +5%/lvl. shieldCapacity(263) is stackable=true -> both
    percentages are plain (no penalty). Nothing in the fit touches shield resonances, so
    each resist is the hull base (EM 1-0.25 = 75%). EHP uses the uniform 25/25/25/25
    profile: hp / (0.25 * sum(resonances))."""
    ship = vaga["Vagabond"]
    lse, rig = vaga["Large Shield Extender II"], vaga["Medium Core Defense Field Extender I"]
    res = evaluate_fit(ship, _vaga_fit(vaga),
                       op=OperatingProfile(propulsion_active=True))
    shield = res.telemetry["defence"]["layers"]["shield"]

    sm_pct = _attr(SHIELD_MANAGEMENT, ATTR_SHIELD_RIG_HP_PCT) * 5      # +25
    shield_hp = ((_attr(ship, A.SHIELD_HP) + _attr(lse, ATTR_SHIELD_CAP_ADD))
                 * (1 + _attr(rig, ATTR_SHIELD_RIG_HP_PCT) / 100.0)    # rig +15%
                 * (1 + sm_pct / 100.0))                              # (1800+2600)*1.15*1.25
    assert shield_hp == pytest.approx(6325.0, rel=1e-4)               # sanity on the arithmetic
    assert shield["hp"] == pytest.approx(shield_hp, rel=2e-3)

    # One resist channel: EM shield resonance is untouched (no hardener) -> hull base.
    base_em = _attr(ship, A.SHIELD_RESONANCE["em"])                   # 0.25
    assert shield["resists"]["em"] == pytest.approx((1 - base_em) * 100, abs=0.2)

    # EHP (uniform profile) = hp / (0.25 * sum of the four shield resonances).
    res_sum = sum(_attr(ship, A.SHIELD_RESONANCE[d]) for d in A.DAMAGE_TYPES)
    assert shield["ehp"] == pytest.approx(shield_hp / (0.25 * res_sum), rel=2e-3)


def test_vagabond_prop_velocity_signature_and_align(vaga):
    """(e) max velocity with the AB running, (f) signature (AB adds no bloom), (g) align
    time — All V, prop active.

    Base velocity: hull maxVelocity x Navigation(+5%/lvl) x nanofiber velocityBonus
    (single penalised entry -> full). Prop: v = base * (1 + (speedFactor*Acceleration
    Control/100) * (speedBoostFactor / (mass + AB massAddition))).
    Signature: (hull sig + LSE flat add) then the istab +11% and the shield-rig drawback
    (+10%, halved to +5% by Shield Rigging V) share ONE penalised chain on the
    non-stackable signatureRadius(552); the AB has NO signatureRadiusBonus so it is
    identical with prop on and off. Align uses the prop-inclusive mass."""
    ship = vaga["Vagabond"]
    ab, lse = vaga["10MN Afterburner II"], vaga["Large Shield Extender II"]
    nano, istab = vaga["Nanofiber Internal Structure II"], vaga["Inertial Stabilizers II"]
    rig = vaga["Medium Core Defense Field Extender I"]
    res = evaluate_fit(ship, _vaga_fit(vaga),
                       op=OperatingProfile(propulsion_active=True))
    off_prop = evaluate_fit(ship, _vaga_fit(vaga, prop=False),
                            op=OperatingProfile(propulsion_active=False))
    mob = res.telemetry["mobility"]

    # Base (prop-off) velocity: Navigation is a skill (plain); the nanofiber is the only
    # module on maxVelocity(37) -> single penalised entry at full strength.
    nav = 1 + _attr(NAVIGATION, 315) * 5 / 100.0            # Navigation velocityBonus +5%/lvl
    base_v = (_attr(ship, A.MAX_VELOCITY) * nav
              * (1 + _attr(nano, ATTR_VELOCITY_BONUS) / 100.0))
    assert mob["max_velocity"] == pytest.approx(base_v, rel=2e-3)

    # Prop velocity: the thrust formula with Acceleration Control-boosted speedFactor and
    # the AB massAddition folded into ship mass while it runs.
    mass = _attr(ship, A.MASS) + _attr(ab, ATTR_MASS_ADD)
    sf = _attr(ab, ATTR_SPEED_FACTOR) * (
        1 + _attr(ACCELERATION_CONTROL, 318) * 5 / 100.0)  # Acceleration Control +5%/lvl
    thrust = _attr(ab, ATTR_SPEED_BOOST)
    prop_v = base_v * (1.0 + (sf / 100.0) * (thrust / mass))
    assert mob["propulsion_velocity"] == pytest.approx(prop_v, rel=2e-3)
    assert mob["mass"] == pytest.approx(mass, rel=1e-6)
    assert mob["propulsion_velocity"] > mob["max_velocity"]

    # Signature: modAdd first, then the two penalised postPercent drawbacks. Shield Rigging
    # V halves the group-774 rig drawback (10 -> 5); istab's +11 is the stronger entry.
    rigging = _attr(SHIELD_RIGGING, ATTR_RIGGING_SKILL_PCT) * 5        # -50
    base_sig = _attr(ship, A.SIGNATURE_RADIUS) + _attr(lse, ATTR_SIG_ADD)
    rig_draw = _attr(rig, ATTR_RIG_DRAWBACK) * (1 + rigging / 100.0) / 100.0  # +0.05
    istab_sig = _attr(istab, ATTR_SIG_BONUS) / 100.0                          # +0.11
    sig = base_sig * _chain(1.0, [rig_draw, istab_sig])
    assert mob["signature_radius"] == pytest.approx(sig, rel=2e-3)
    # The AB has no signatureRadiusBonus: signature is unchanged with prop off.
    assert off_prop.telemetry["mobility"]["signature_radius"] == pytest.approx(sig, rel=2e-3)

    # Align time uses the prop-inclusive mass; agility takes both agility skills (plain) and
    # the istab(-20%) + nanofiber(-15.75%) penalised chain (istab is the stronger entry).
    agility = (_attr(ship, A.AGILITY) * 0.75 * 0.90          # Evasive Manoeuvring + SC V
               * _chain(1.0, [_attr(istab, ATTR_AGILITY_MULT) / 100.0,
                              _attr(nano, ATTR_AGILITY_MULT) / 100.0]))
    assert mob["align_time_s"] == pytest.approx(LN4 * mass * agility / 1e6, rel=2e-3)


def test_vagabond_capacitor_stability(vaga):
    """(h) Cap stability, All V, prop running. Autocannons draw no capacitor, so the AB is
    the only load. capacity = hull cap x Cap Management V; tau = hull recharge x Cap Systems
    Operation V. Drain = AB capNeed (Afterburner + Fuel Conservation -10%/lvl each) over the
    AB cycle (Afterburner -5%/lvl duration). Stable fraction from the recharge quadratic."""
    ship, ab = vaga["Vagabond"], vaga["10MN Afterburner II"]
    res = evaluate_fit(ship, _vaga_fit(vaga),
                       op=OperatingProfile(propulsion_active=True))
    cap = res.telemetry["capacitor"]

    capacity = _attr(ship, A.CAP_CAPACITY) * 1.25          # Cap Management V (+5%/lvl)
    tau = _attr(ship, A.CAP_RECHARGE_RATE) / 1000.0 * 0.75  # Cap Systems Operation V (-5%/lvl)
    peak = 2.5 * capacity / tau
    cycle_s = _attr(ab, ATTR_DURATION) / 1000.0 * 0.75      # Afterburner V duration -5%/lvl
    need = _attr(ab, ATTR_CAP_NEED) * 0.5 * 0.5             # Afterburner + Fuel Conservation V
    drain = need / cycle_s
    assert cap["capacity"] == pytest.approx(capacity, rel=2e-3)
    assert cap["recharge_s"] == pytest.approx(tau, rel=2e-3)
    assert cap["peak_recharge"] == pytest.approx(peak, rel=2e-3)
    assert cap["usage"] == pytest.approx(drain, rel=2e-3)   # AC guns cost 0 cap
    assert drain < peak and cap["stable"] is True
    k = drain * tau / (10.0 * capacity)
    s = (1.0 + math.sqrt(1.0 - 4.0 * k)) / 2.0
    assert cap["stable_pct"] == pytest.approx(s * s * 100.0, abs=0.06)


# =====================================================================================
# FIT B — Loki (Minmatar T3 Strategic Cruiser): 4 subsystems + 4x Heavy Assault Missile
# Launcher II + Scourge Rage + Large Shield Extender II + Multispectrum Shield Hardener II
# + 2x Ballistic Control System II, + a Shield Harmonizing Charge burst for the boost case.
# =====================================================================================
@pytest.fixture()
def loki():
    return load_graph_fixture("loki_hull")


def _loki_subs(ids):
    return [ModuleInput(type_id=ids[name], slot=SlotKind.SUBSYSTEM,
                        state=ModuleState.ONLINE) for name in (
        "Loki Offensive - Launcher Efficiency Configuration",
        "Loki Defensive - Adaptive Defense Node",
        "Loki Propulsion - Wake Limiter",
        "Loki Core - Augmented Nuclear Reactor")]


def _loki_hams(ids, n, state=ModuleState.ACTIVE):
    ham, rage = ids["Heavy Assault Missile Launcher II"], \
        ids["Scourge Rage Heavy Assault Missile"]
    return [ModuleInput(type_id=ham, slot=SlotKind.HIGH, state=state, charge_type_id=rage)
            for _ in range(n)]


def _loki_full(ids):
    """Whole HAM shield Loki used by the DPS / defence / status assertions."""
    lse, hard = ids["Large Shield Extender II"], ids["Multispectrum Shield Hardener II"]
    bcs = ids["Ballistic Control System II"]
    mods = _loki_subs(ids) + _loki_hams(ids, 4)
    mods += [ModuleInput(type_id=lse, slot=SlotKind.MED, state=ModuleState.ONLINE),
             ModuleInput(type_id=hard, slot=SlotKind.MED, state=ModuleState.ACTIVE),
             ModuleInput(type_id=bcs, slot=SlotKind.LOW, state=ModuleState.ONLINE),
             ModuleInput(type_id=bcs, slot=SlotKind.LOW, state=ModuleState.ONLINE)]
    return mods


def test_loki_subsystem_slot_hardpoint_structure_folding(loki):
    """(a) Subsystem slot / hardpoint / structure-HP folding, All V. The bare Loki hull
    carries no hi/med/low slot attrs — every slot and hardpoint comes from a subsystem's
    slotModifier / hardPointModifier attribute (a flat modAdd, skill-independent), and the
    Adaptive Defense Node adds +100 flat structure HP (attr 2688) and +300 flat shield
    capacity (attr 263). Structure HP then takes Mechanics +5%/lvl."""
    ship = loki["Loki"]
    off = loki["Loki Offensive - Launcher Efficiency Configuration"]
    dfn = loki["Loki Defensive - Adaptive Defense Node"]
    prop = loki["Loki Propulsion - Wake Limiter"]
    core = loki["Loki Core - Augmented Nuclear Reactor"]
    res = evaluate_fit(ship, _loki_full(loki),
                       op=OperatingProfile(propulsion_active=False))
    r = res.telemetry["resources"]

    # Hi/med/low = sum of each subsystem's slot modifiers (hull base is 0).
    hi = int(_attr(off, A.SUB_HI_SLOT_MOD))                            # 7
    med = int(_attr(dfn, A.SUB_MED_SLOT_MOD) + _attr(prop, A.SUB_MED_SLOT_MOD)
              + _attr(core, A.SUB_MED_SLOT_MOD))                       # 2+2+1 = 5
    low = int(_attr(dfn, A.SUB_LOW_SLOT_MOD) + _attr(core, A.SUB_LOW_SLOT_MOD))  # 2+3 = 5
    assert r["slots"]["hull"] == {"high": hi, "med": med, "low": low,
                                  "rig": int(_attr(ship, A.RIG_SLOTS))}
    assert (hi, med, low) == (7, 5, 5)

    # Turret / launcher hardpoints come from the offensive subsystem only.
    assert r["hardpoints"]["turret"]["total"] == int(_attr(off, A.SUB_TURRET_HP_MOD))     # 2
    assert r["hardpoints"]["launcher"]["total"] == int(_attr(off, A.SUB_LAUNCHER_HP_MOD))  # 5
    assert r["hardpoints"]["launcher"]["used"] == 4                    # the 4 HAMs

    # Structure HP: (hull hp + defensive structureHPBonusAdd) x Mechanics V.
    struct = ((_attr(ship, A.HULL_HP) + _attr(dfn, ATTR_STRUCT_HP_ADD))
              * (1 + _attr(MECHANICS, ATTR_STRUCT_HP_PCT) * 5 / 100.0))   # (1000+100)*1.25
    assert res.telemetry["defence"]["layers"]["hull"]["hp"] == pytest.approx(struct, rel=2e-3)
    assert struct == pytest.approx(1375.0, rel=1e-4)


def test_loki_all_v_dps_volley_sustained_and_status_valid(loki):
    """(b) total_dps / volley / sustained with the full subsystem + skill missile chain,
    and (f) fit status valid (all four subsystems present, one per slot). All V.

    Damage: Scourge Rage kinetic x Heavy Assault Missiles(+5%/lvl) x Warhead
    Upgrades(+2%/lvl) x the two BCS on the char's missileDamageMultiplier(212, preMul,
    penalised). RoF: launcher speed x Missile Launcher Operation(-2%/lvl) x Rapid
    Launch(-3%/lvl) x HAM Specialization(-2%/lvl) x the offensive subsystem RoF bonus
    (subsystemBonusMinmatarOffensive2 = -10 x Minmatar Offensive Systems V = -50%,
    subsystem source -> exempt/plain) x the two BCS speedMultiplier(0.895, penalised)."""
    ham, rage, bcs = (loki["Heavy Assault Missile Launcher II"],
                      loki["Scourge Rage Heavy Assault Missile"],
                      loki["Ballistic Control System II"])
    off_sub = loki["Loki Offensive - Launcher Efficiency Configuration"]
    res = evaluate_fit(loki["Loki"], _loki_full(loki),
                       op=OperatingProfile(propulsion_active=False))  # omniscient default
    off = res.telemetry["offence"]
    assert res.status.value == "valid"                                 # (f) subsystems valid

    shot = _attr(rage, A.KINETIC_DAMAGE)                               # 155.3 (kinetic only)
    lvl = 5
    skill_dmg = ((1 + _attr(HEAVY_ASSAULT_MISSILES, DMG_BONUS) * lvl / 100.0)
                 * (1 + _attr(WARHEAD_UPGRADES, DMG_BONUS) * lvl / 100.0))
    gd = _attr(bcs, ATTR_BCS_DMG_BONUS) - 1.0             # +10% char-212 preMul per BCS
    char_mult = (1 + gd) * (1 + gd * S1)                  # two BCS, penalised on attr 212
    dmg = shot * skill_dmg * char_mult

    sub_rof = 1 + _attr(off_sub, ATTR_SUB_OFF_ROF) * lvl / 100.0       # 1 + (-10*5)/100 = 0.5
    skill_rof = ((1 + _attr(MISSILE_LAUNCHER_OPERATION, ROF_BONUS) * lvl / 100.0)
                 * (1 + _attr(RAPID_LAUNCH, ROF_BONUS) * lvl / 100.0)
                 * (1 + _attr(HAM_SPECIALIZATION, ROF_BONUS) * lvl / 100.0)
                 * sub_rof)
    rb = _attr(bcs, A.ROF_MULTIPLIER) - 1.0               # -10.5% speedMultiplier per BCS
    rof_ms = _chain(_attr(ham, A.RATE_OF_FIRE) * skill_rof, [rb, rb])
    volley_per = dmg
    dps_per = volley_per / (rof_ms / 1000.0)
    assert off["missile_dps"] == pytest.approx(4 * dps_per, rel=2e-3)
    assert off["total_dps"] == pytest.approx(4 * dps_per, rel=2e-3)
    assert off["volley"] == pytest.approx(4 * volley_per, rel=2e-3)
    assert off["damage_distribution"]["kinetic"] == pytest.approx(100.0, abs=0.1)

    # Sustained: HAM magazine = floor(0.99 / 0.015) = 66 rounds, then reloadTime.
    mag = math.floor(_attr(ham, ATTR_CAPACITY) / _attr(rage, ATTR_VOLUME))
    assert mag == 66
    reload_s = _attr(ham, ATTR_RELOAD) / 1000.0
    time_to_empty = mag * (rof_ms / 1000.0)
    sustained_per = mag * volley_per / (time_to_empty + reload_s)
    assert off["total_sustained_dps"] == pytest.approx(4 * sustained_per, rel=2e-3)
    assert off["total_sustained_dps"] < off["total_dps"]


def test_loki_applied_dps_vs_target_no_skills(loki):
    """(c) Applied DPS vs a target profile — post-Aegis missile formula, NO skills so the
    Scourge Rage explosion radius/velocity stay at base (no subsystem/skill modification)
    and the maths is a clean hand calc. Against a sig-150 / 200 m/s cruiser:
    factor = min(1, sig/Er, ((sig/Er)*(Ev/Vt))^drf), applied = missile_dps * factor."""
    ham, rage = (loki["Heavy Assault Missile Launcher II"],
                 loki["Scourge Rage Heavy Assault Missile"])
    target = TargetProfile(signature_radius=150.0, velocity=200.0, label="cruiser")
    res = evaluate_fit(loki["Loki"], _loki_subs(loki) + _loki_hams(loki, 4),
                       skills=NO_SKILLS,
                       op=OperatingProfile(propulsion_active=False, target=target))
    off = res.telemetry["offence"]

    er = _attr(rage, A.AOE_CLOUD_SIZE)                    # 215 m explosion radius
    ev = _attr(rage, A.AOE_VELOCITY)                      # 87 m/s explosion velocity
    drf = _attr(rage, ATTR_DRF)                           # 0.92 (the exponent itself)
    size_term = target.signature_radius / er
    factor = min(1.0, size_term,
                 (size_term * (ev / target.velocity)) ** drf)
    assert off["missile_application"] == pytest.approx(factor, rel=2e-3, abs=5e-4)

    shot = _attr(rage, A.KINETIC_DAMAGE)                  # no skills -> no dmg/RoF chain
    rof_s = _attr(ham, A.RATE_OF_FIRE) / 1000.0
    missile_dps = 4 * shot / rof_s
    assert off["missile_dps"] == pytest.approx(missile_dps, rel=2e-3, abs=0.06)
    assert off["missile_dps_applied"] == pytest.approx(missile_dps * factor,
                                                       rel=2e-3, abs=0.06)


def test_loki_shield_resist_hardener_and_ehp(loki):
    """(d) Shield resist with the Multispectrum hardener + (e) shield EHP, All V. The active
    hardener applies its resistance bonus (attr 984-987 = -32.5% each) postPercent to the
    hull's shield resonances; a single hardener is the only entry in each resonance's
    penalised chain -> full strength. shieldCapacity(263) folds hull + defensive subsystem
    +300 + Large Shield Extender +2600, x Shield Management V. EHP from the uniform profile."""
    ship = loki["Loki"]
    hard, lse = loki["Multispectrum Shield Hardener II"], loki["Large Shield Extender II"]
    dfn = loki["Loki Defensive - Adaptive Defense Node"]
    res = evaluate_fit(ship, _loki_full(loki),
                       op=OperatingProfile(propulsion_active=False))
    shield = res.telemetry["defence"]["layers"]["shield"]

    for d in A.DAMAGE_TYPES:
        base = _attr(ship, A.SHIELD_RESONANCE[d])
        bonus = _attr(hard, A.SHIELD_RESIST_BONUS[d]) / 100.0         # -0.325
        expected = base * (1 + bonus)                                # single entry -> full
        assert shield["resists"][d] == pytest.approx((1 - expected) * 100, abs=0.2)

    sm_pct = _attr(SHIELD_MANAGEMENT, ATTR_SHIELD_RIG_HP_PCT) * 5     # +25
    shield_hp = ((_attr(ship, A.SHIELD_HP) + _attr(dfn, ATTR_SHIELD_CAP_SUB_ADD)
                  + _attr(lse, ATTR_SHIELD_CAP_ADD)) * (1 + sm_pct / 100.0))
    assert shield_hp == pytest.approx(6750.0, rel=1e-4)              # (2500+300+2600)*1.25
    assert shield["hp"] == pytest.approx(shield_hp, rel=2e-3)

    # EHP (uniform) = hp / (0.25 * sum of hardened resonances).
    res_sum = sum(_attr(ship, A.SHIELD_RESONANCE[d])
                  * (1 + _attr(hard, A.SHIELD_RESIST_BONUS[d]) / 100.0)
                  for d in A.DAMAGE_TYPES)
    assert shield["ehp"] == pytest.approx(shield_hp / (0.25 * res_sum), rel=2e-3)


def test_loki_boost_interplay_shield_harmonizing(loki):
    """(g) Fleet-boost interplay: a Shield Harmonizing Charge (-8% shield resonance, buff
    id 10) applied ON TOP of the active Multispectrum hardener. Both are postPercent on the
    non-stackable shield resonances, so they share ONE penalised chain (verified against the
    boosts golden): the hardener (-32.5%, larger magnitude) takes full strength, the boost
    (-8%) is penalised by S1. All V."""
    ship = loki["Loki"]
    hard, charge = (loki["Multispectrum Shield Hardener II"],
                    loki["Shield Harmonizing Charge"])
    mods = _loki_subs(loki) + [
        ModuleInput(type_id=hard, slot=SlotKind.MED, state=ModuleState.ACTIVE)]
    res = _evaluate_with_boosts(ship, mods, [BoostInput(charge_type_id=charge)])
    shield = res.telemetry["defence"]["layers"]["shield"]

    # The burst's warfareBuff1Multiplier is the effective strength of an unbonused T1 burst.
    boost = _attr(charge, 2596) / 100.0                              # -0.08
    assert boost == pytest.approx(-0.08)
    for d in A.DAMAGE_TYPES:
        base = _attr(ship, A.SHIELD_RESONANCE[d])
        h = _attr(hard, A.SHIELD_RESIST_BONUS[d]) / 100.0            # -0.325 (stronger)
        expected = base * _chain(1.0, [h, boost])                   # shared penalty chain
        assert shield["resists"][d] == pytest.approx((1 - expected) * 100, abs=0.2)

    # Sanity brackets: with the boost the EM resist is strictly better than hardener-alone,
    # but weaker than a naive un-penalised stack.
    base_em = _attr(ship, A.SHIELD_RESONANCE["em"])
    h_em = _attr(hard, A.SHIELD_RESIST_BONUS["em"]) / 100.0
    hardener_only = (1 - base_em * (1 + h_em)) * 100
    naive = (1 - base_em * (1 + h_em) * (1 + boost)) * 100
    assert hardener_only < shield["resists"]["em"] < naive

    # The boost is recorded and applied (buff id 10 resolved from the imported dbuff table).
    b = res.telemetry["boosts"]
    assert b["count"] == 1
    assert b["boosts"][0]["buffs"][0]["buff_id"] == 10
    assert b["boosts"][0]["buffs"][0]["applied"] is True
