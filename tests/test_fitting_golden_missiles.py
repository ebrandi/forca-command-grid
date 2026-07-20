"""Golden fits: missile boats (engine v2, real SDE slice, hand-derived numbers).

Fixture: tests/fixtures/fitting/missiles_graph.json — a real CCP data slice extracted
through the normal pipeline (Caracal / Kestrel / Condor hulls, Heavy / Rapid Light /
Rocket launchers, Scourge charges, Ballistic Control System II). Every expected value is
DERIVED IN THE TEST from the slice's base attributes plus documented EVE mechanics —
never read back from the engine.

Percentage sources (each verified against the live SDE dogma rows, cited per constant):

* Missile Launcher Operation (3319): rofBonus attr 293 = -2 per level, effect 1763
  (LocationRequiredSkillModifier, postPercent attr 51, requires 3319).
* Rapid Launch (21071): rofBonus attr 293 = -3 per level, same effect 1763.
* Heavy/Light/Rocket Specialization (20211/20210/20209): rofBonus 293 = -2 per level,
  effect 1851 (selfRof: applies to launchers requiring the spec skill itself).
* Heavy Missiles / Light Missiles / Rockets (3324/3321/3320): damageMultiplierBonus
  attr 292 = +5 per level, effects 660/661/662/668 (postPercent on the CHARGE's
  em/expl/therm/kin damage, requires the skill itself).
* Warhead Upgrades (20315): attr 292 = +2 per level, effects 1595-1597/1657
  (postPercent on charge damage, requires 3319 — every missile).
* Missile Projection (12442): speedFactor attr 20 = +10 per level, effect 1764
  (postPercent charge maxVelocity 37, requires 3319).
* Missile Bombardment (12441): maxFlightTimeBonus attr 557 = +10 per level, effect 784
  (postPercent charge explosionDelay 281, requires 3319).
* Caracal hull: shipBonusCC attr 487 = -5 (× Caldari Cruiser level, preMul eff 520);
  effect 5131 shipMissileRofCC = postPercent attr 51 on launcher GROUPS 510/511/771
  (Heavy + Rapid Light launchers both covered — verified in sde_sdemodifier).
  shipBonusCC2 attr 657 = +10 (× level): effect 1024 postPercent missile maxVelocity,
  requires Heavy Missiles 3324 (heavies only — NOT light missiles).
* Kestrel hull: shipBonusCF attr 463 = +10 (× Caldari Frigate level): effect 5080
  postPercent missile maxVelocity, requires 3319 (all missiles). shipBonusCF2 attr
  588 = +5 (× level): effects 5234/5237/5240/5243 postPercent on ALL FOUR damage
  attrs of missiles requiring Rockets 3320 or Light Missiles 3321 — the data says the
  Kestrel trait is an all-damage bonus, not kinetic-only (verified vs sde_sdemodifier).
* Ballistic Control System II: missileDamageMultiplierBonus attr 213 (=1.1) preMuls the
  CHARACTER's missileDamageMultiplier attr 212 (effect 763, online); attr 212 is
  stackable=false and a BCS is a module, so two BCS are stacking-penalised on the char
  attribute. The char's 212 then post-multiplies missile damage via the engine's
  documented builtin data patch (missiles requiring 3319). speedMultiplier attr 204
  (=0.895) post-multiplies launcher attr 51 (effect 889, requires 3319); attr 51 is
  stackable=false so BCS RoF chains are stacking-penalised too.
* Overload: overloadRofBonus attr 1205 (=-15) postPercent on the launcher's own attr 51
  (effect 3001, effectCategory 5 — applies only in the OVERHEATED state).

Missiles in the current SDE carry NO chargeSize attr (128) — verified: zero rows for
attr 128 across launcher groups 507/510/511 and charge groups 384/385/387/655 — so the
"wrong size" failure mode for missiles is chargeGroup incompatibility.
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
    TargetProfile,
)

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db

S1 = math.exp(-((1 / 2.67) ** 2))   # 0.8691… second-module stacking effectiveness

# Skill chain factors at level V (source percentages cited in the module docstring).
MLO_V = 1 - 0.02 * 5                # Missile Launcher Operation: -2% RoF/lvl (attr 293)
RAPID_LAUNCH_V = 1 - 0.03 * 5       # Rapid Launch: -3% RoF/lvl (attr 293)
SPEC_ROF_V = 1 - 0.02 * 5           # Heavy/Light/Rocket Spec: -2% RoF/lvl (attr 293)
MISSILE_DMG_SKILL_V = 1 + 0.05 * 5  # Heavy/Light Missiles, Rockets: +5% dmg/lvl (292)
WARHEAD_V = 1 + 0.02 * 5            # Warhead Upgrades: +2% dmg/lvl (attr 292)
MISSILE_PROJ_V = 1 + 0.10 * 5       # Missile Projection: +10% velocity/lvl (attr 20)
MISSILE_BOMB_V = 1 + 0.10 * 5       # Missile Bombardment: +10% flight time/lvl (557)
CARACAL_ROF_V = 1 - 0.05 * 5        # Caracal shipBonusCC 487=-5 × Caldari Cruiser V
CARACAL_HEAVY_VEL_V = 1 + 0.10 * 5  # Caracal shipBonusCC2 657=+10 × level (req. 3324)
KESTREL_VEL_V = 1 + 0.10 * 5        # Kestrel shipBonusCF 463=+10 × level (req. 3319)
KESTREL_DMG_V = 1 + 0.05 * 5        # Kestrel shipBonusCF2 588=+5 × level (rockets/lights)

ATTR_EXPLOSION_DELAY = 281          # flight time (ms) on the charge
ATTR_DRF = 1353                     # aoeDamageReductionFactor — the application EXPONENT

# Telemetry is rounded to 1 decimal; abs=0.06 covers that quantisation for values whose
# rel=2e-3 band is narrower than the rounding step.
def approx(v):
    return pytest.approx(v, rel=2e-3, abs=0.06)


@pytest.fixture()
def ids():
    return load_graph_fixture("missiles_graph")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _launchers(launcher, ammo, n, state=ModuleState.ACTIVE):
    return [ModuleInput(type_id=launcher, slot=SlotKind.HIGH, state=state,
                        charge_type_id=ammo) for _ in range(n)]


def _codes(res):
    return {d.code for d in res.diagnostics}


# --------------------------------------------------------------------------- #
# Isolation fits (no skills trained: every skill/trait chain is exactly zero)
# --------------------------------------------------------------------------- #
def test_untrained_hml_base_dps(ids):
    """Fit 1 — Caracal + 5x HML II + Scourge Heavy, NO skills: dps is purely
    charge damage / launcher RoF; velocity/flight/range telemetry at base values."""
    caracal, hml, ammo = ids["Caracal"], ids["Heavy Missile Launcher II"], \
        ids["Scourge Heavy Missile"]
    res = evaluate_fit(caracal, _launchers(hml, ammo, 5),
                       skills=SkillProfile.from_dict({}))

    shot = _attr(ammo, A.KINETIC_DAMAGE)                 # 149 kinetic, only component
    rof_s = _attr(hml, A.RATE_OF_FIRE) / 1000.0          # 12000 ms
    off = res.telemetry["offence"]
    assert off["missile_dps"] == approx(5 * shot / rof_s)
    assert off["turret_dps"] == 0.0
    assert off["volley"] == approx(5 * shot)
    entry = off["weapons"][0]
    assert entry["kind"] == "missile"
    vel = _attr(ammo, 37)                                # maxVelocity 4300
    flight_s = _attr(ammo, ATTR_EXPLOSION_DELAY) / 1000.0  # 6500 ms
    assert entry["missile_velocity"] == approx(vel)
    assert entry["flight_time_s"] == approx(flight_s)
    assert entry["range_m"] == approx(vel * flight_s)
    hp = res.telemetry["resources"]["hardpoints"]["launcher"]
    assert (hp["used"], hp["total"]) == (5, 5)
    # Untrained pilot cannot operate the fit — but it is structurally sound.
    assert res.status.value == "missing_skills"


def test_single_bcs_online_damage_and_rof(ids):
    """Fit 2 — one ONLINE BCS II: damage ×1.1 via the char's missileDamageMultiplier
    (attr 212) patch chain; RoF ×0.895 via speedMultiplier — both effects are
    effectCategory 4 (online), so ONLINE state must be enough."""
    caracal, hml, ammo, bcs = ids["Caracal"], ids["Heavy Missile Launcher II"], \
        ids["Scourge Heavy Missile"], ids["Ballistic Control System II"]
    mods = _launchers(hml, ammo, 4)
    mods.append(ModuleInput(type_id=bcs, slot=SlotKind.LOW, state=ModuleState.ONLINE))
    res = evaluate_fit(caracal, mods, skills=SkillProfile.from_dict({}))

    shot = _attr(ammo, A.KINETIC_DAMAGE)
    dmg_bonus = _attr(bcs, A.MISSILE_DAMAGE_MULT_BONUS)   # 1.1 → char 212 = 1.1
    rof_mult = _attr(bcs, A.ROF_MULTIPLIER)               # 0.895
    rof_s = _attr(hml, A.RATE_OF_FIRE) * rof_mult / 1000.0
    assert res.telemetry["offence"]["missile_dps"] == approx(
        4 * shot * dmg_bonus / rof_s)


def test_two_bcs_stacking_on_char_attr_212(ids):
    """Fit 3 — TWO BCS II: both chains stack-penalised. attr 212 (char) and attr 51
    (launcher) are stackable=false and a BCS is a module (category 7, not exempt), so
    the second BCS applies at S1 effectiveness on both damage and RoF."""
    caracal, hml, ammo, bcs = ids["Caracal"], ids["Heavy Missile Launcher II"], \
        ids["Scourge Heavy Missile"], ids["Ballistic Control System II"]
    mods = _launchers(hml, ammo, 4)
    mods += [ModuleInput(type_id=bcs, slot=SlotKind.LOW) for _ in range(2)]
    res = evaluate_fit(caracal, mods, skills=SkillProfile.from_dict({}))

    shot = _attr(ammo, A.KINETIC_DAMAGE)
    g = _attr(bcs, A.MISSILE_DAMAGE_MULT_BONUS) - 1.0     # +10% char 212, preMul
    char_mult = (1 + g) * (1 + g * S1)                    # penalised on char attr 212
    r = _attr(bcs, A.ROF_MULTIPLIER) - 1.0                # -10.5% RoF, postMul
    rof_s = _attr(hml, A.RATE_OF_FIRE) * (1 + r) * (1 + r * S1) / 1000.0
    assert res.telemetry["offence"]["missile_dps"] == approx(
        4 * shot * char_mult / rof_s)


def test_overheated_launcher_rof(ids):
    """Fit 1 (overheated state) — overloadRofBonus (-15%, attr 1205, effectCategory 5)
    applies postPercent to the launcher's own RoF only when OVERHEATED."""
    caracal, hml, ammo = ids["Caracal"], ids["Heavy Missile Launcher II"], \
        ids["Scourge Heavy Missile"]
    res_hot = evaluate_fit(caracal, _launchers(hml, ammo, 5, ModuleState.OVERHEATED),
                           skills=SkillProfile.from_dict({}))
    res_act = evaluate_fit(caracal, _launchers(hml, ammo, 5),
                           skills=SkillProfile.from_dict({}))

    shot = _attr(ammo, A.KINETIC_DAMAGE)
    base_rof_s = _attr(hml, A.RATE_OF_FIRE) / 1000.0
    ol = _attr(hml, A.OVERLOAD_ROF_BONUS)                 # -15 (%)
    hot_rof_s = base_rof_s * (1 + ol / 100.0)
    assert res_act.telemetry["offence"]["missile_dps"] == approx(5 * shot / base_rof_s)
    assert res_hot.telemetry["offence"]["missile_dps"] == approx(5 * shot / hot_rof_s)


# Regression test for the (fixed) DRF/DRS attribute mix-up: attr 655 is aoeFALLOFF,
# not the DRF; attr 1353 holds the post-Aegis application exponent DIRECTLY.
def test_missile_application_vs_target_profile(ids):
    """Fit 4 — application vs a small fast target, NO skills. Documented post-Aegis
    formula: applied = min(1, sig/er, ((sig/er)·(ev/vt))^drf), drf = attr 1353."""
    caracal, hml, ammo = ids["Caracal"], ids["Heavy Missile Launcher II"], \
        ids["Scourge Heavy Missile"]
    target = TargetProfile(signature_radius=35.0, velocity=600.0, label="interceptor")
    res = evaluate_fit(caracal, _launchers(hml, ammo, 3),
                       skills=SkillProfile.from_dict({}),
                       op=OperatingProfile(propulsion_active=False, target=target))

    er = _attr(ammo, A.AOE_CLOUD_SIZE)                    # 140 m
    ev = _attr(ammo, A.AOE_VELOCITY)                      # 85 m/s
    drf = _attr(ammo, ATTR_DRF)                           # 0.682 — the exponent itself
    size_term = target.signature_radius / er              # 0.25
    frac = min(1.0, size_term,
               (size_term * ev / target.velocity) ** drf)  # 0.1025
    off = res.telemetry["offence"]
    assert off["missile_application"] == pytest.approx(frac, rel=2e-3, abs=5e-4)
    shot = _attr(ammo, A.KINETIC_DAMAGE)
    rof_s = _attr(hml, A.RATE_OF_FIRE) / 1000.0
    assert off["missile_dps_applied"] == approx(3 * shot / rof_s * frac)


def test_launcher_hardpoint_overflow(ids):
    """Fit 5 — Condor: 4 high slots but only 3 launcher hardpoints (both values read
    from the slice), so a 4th launcher overflows hardpoints WITHOUT overflowing slots."""
    condor, rl = ids["Condor"], ids["Rocket Launcher II"]
    assert _attr(condor, A.HI_SLOTS) == 4
    assert _attr(condor, A.LAUNCHER_HARDPOINTS) == 3
    res = evaluate_fit(condor, _launchers(rl, None, 4),
                       skills=SkillProfile.from_dict({}))
    codes = _codes(res)
    assert "launcher_hardpoints" in codes
    assert "too_many_modules" not in codes
    hp = res.telemetry["resources"]["hardpoints"]["launcher"]
    assert (hp["used"], hp["total"]) == (4, 3)
    assert res.status.value == "impossible"


def test_empty_launcher_missing_ammo_warning(ids):
    """Fit 6 — two charge-less launchers: one missing_ammo warning each, zero missile
    dps, overall status 'warnings' (all skills trained, nothing structural)."""
    caracal, hml = ids["Caracal"], ids["Heavy Missile Launcher II"]
    res = evaluate_fit(caracal, _launchers(hml, None, 2))
    missing = [d for d in res.diagnostics if d.code == "missing_ammo"]
    assert len(missing) == 2
    assert res.telemetry["offence"]["missile_dps"] == 0.0
    assert res.status.value == "warnings"


def test_wrong_charge_group_diagnostic(ids):
    """Fit 7 — a heavy missile (group 385) in a Rocket Launcher II (accepts charge
    groups 387/648 only, read from the slice): incompatible_charge, impossible.
    Missiles carry no chargeSize attr in the SDE, so group compat IS the size gate."""
    from apps.sde.models import SdeType, SdeTypeAttribute
    kestrel, rl, heavy = ids["Kestrel"], ids["Rocket Launcher II"], \
        ids["Scourge Heavy Missile"]
    accepted = set(SdeTypeAttribute.objects.filter(
        type_id=rl, attribute_id__in=A.CHARGE_GROUP_ATTRS).values_list("value", flat=True))
    assert SdeType.objects.get(type_id=heavy).group_id not in accepted
    assert not SdeTypeAttribute.objects.filter(          # no chargeSize on either side
        type_id__in=(rl, heavy), attribute_id=A.CHARGE_SIZE).exists()

    res = evaluate_fit(kestrel, _launchers(rl, heavy, 1))
    assert "incompatible_charge" in _codes(res)
    assert res.status.value == "impossible"


# --------------------------------------------------------------------------- #
# Realistic all-V fits
# --------------------------------------------------------------------------- #
def test_caracal_hml_all_v_full_chain(ids):
    """Fit 8 — 5x HML II + Scourge Heavy + 2x BCS II, All V: the full hand chain.
    Damage: 149 × Heavy Missiles(+25%) × Warhead Upgrades(+10%) × char-212 BCS pair.
    RoF: 12000ms × MLO(-10%) × Rapid Launch(-15%) × HM Spec(-10%) × Caracal(-25%)
    × BCS pair (each ×0.895, stacking-penalised — skills/hull are exempt sources)."""
    caracal, hml, ammo, bcs = ids["Caracal"], ids["Heavy Missile Launcher II"], \
        ids["Scourge Heavy Missile"], ids["Ballistic Control System II"]
    mods = _launchers(hml, ammo, 5)
    mods += [ModuleInput(type_id=bcs, slot=SlotKind.LOW) for _ in range(2)]
    res = evaluate_fit(caracal, mods)

    shot = _attr(ammo, A.KINETIC_DAMAGE)
    g = _attr(bcs, A.MISSILE_DAMAGE_MULT_BONUS) - 1.0
    dmg = shot * MISSILE_DMG_SKILL_V * WARHEAD_V * (1 + g) * (1 + g * S1)
    r = _attr(bcs, A.ROF_MULTIPLIER) - 1.0
    rof_s = (_attr(hml, A.RATE_OF_FIRE) / 1000.0
             * MLO_V * RAPID_LAUNCH_V * SPEC_ROF_V * CARACAL_ROF_V
             * (1 + r) * (1 + r * S1))
    off = res.telemetry["offence"]
    assert off["missile_dps"] == approx(5 * dmg / rof_s)
    assert off["total_dps"] == approx(5 * dmg / rof_s)
    assert off["volley"] == approx(5 * dmg)
    assert off["damage_distribution"]["kinetic"] == approx(100.0)
    assert not res.missing_skills
    assert res.status.value in ("valid", "warnings")


def test_caracal_hml_all_v_velocity_flight_range(ids):
    """Fit 9 — 3x HML II + Scourge Heavy, All V, no BCS: dps from the skill/trait RoF
    chain, and missile velocity/flight/range hand-computed: velocity 4300 × Missile
    Projection(+50%) × Caracal heavy-velocity trait(+50%); flight 6.5s × Missile
    Bombardment(+50%); range = velocity × flight."""
    caracal, hml, ammo = ids["Caracal"], ids["Heavy Missile Launcher II"], \
        ids["Scourge Heavy Missile"]
    res = evaluate_fit(caracal, _launchers(hml, ammo, 3))

    dmg = _attr(ammo, A.KINETIC_DAMAGE) * MISSILE_DMG_SKILL_V * WARHEAD_V
    rof_s = (_attr(hml, A.RATE_OF_FIRE) / 1000.0
             * MLO_V * RAPID_LAUNCH_V * SPEC_ROF_V * CARACAL_ROF_V)
    off = res.telemetry["offence"]
    assert off["missile_dps"] == approx(3 * dmg / rof_s)
    vel = _attr(ammo, 37) * MISSILE_PROJ_V * CARACAL_HEAVY_VEL_V           # 9675
    flight_s = _attr(ammo, ATTR_EXPLOSION_DELAY) / 1000.0 * MISSILE_BOMB_V  # 9.75
    entry = off["weapons"][0]
    assert entry["missile_velocity"] == approx(vel)
    assert entry["flight_time_s"] == approx(flight_s)
    assert entry["range_m"] == pytest.approx(vel * flight_s, rel=2e-3)


def test_kestrel_rockets_all_v(ids):
    """Fit 10 — Kestrel + 4x Rocket Launcher II + Scourge Rocket, All V. The Kestrel
    hull trait (shipBonusCF2 attr 588 = +5%/lvl) applies to ALL FOUR damage attrs of
    rockets/lights — verified against the data, NOT a kinetic-only trait. No hull RoF
    trait (shipBonusCF 463 is the velocity bonus)."""
    kestrel, rl, ammo = ids["Kestrel"], ids["Rocket Launcher II"], ids["Scourge Rocket"]
    res = evaluate_fit(kestrel, _launchers(rl, ammo, 4))

    dmg = (_attr(ammo, A.KINETIC_DAMAGE)                  # 33, only nonzero component
           * MISSILE_DMG_SKILL_V * WARHEAD_V * KESTREL_DMG_V)
    rof_s = (_attr(rl, A.RATE_OF_FIRE) / 1000.0           # 4000 ms
             * MLO_V * RAPID_LAUNCH_V * SPEC_ROF_V)       # Rocket Spec selfRof
    off = res.telemetry["offence"]
    assert off["missile_dps"] == approx(4 * dmg / rof_s)
    vel = _attr(ammo, 37) * MISSILE_PROJ_V * KESTREL_VEL_V  # 2250 × 1.5 × 1.5
    flight_s = _attr(ammo, ATTR_EXPLOSION_DELAY) / 1000.0 * MISSILE_BOMB_V
    entry = off["weapons"][0]
    assert entry["missile_velocity"] == approx(vel)
    assert entry["range_m"] == pytest.approx(vel * flight_s, rel=2e-3)
    assert res.status.value in ("valid", "warnings")


def test_rapid_light_caracal_all_v(ids):
    """Fit 11 — Caracal + 2x Rapid Light Missile Launcher II + Scourge Light, All V.
    The Caracal RoF trait covers group 511 (rapid lights) per effect 5131's group
    list; Light Missile Spec supplies the selfRof; the Caracal heavy-VELOCITY trait
    (requires Heavy Missiles 3324) must NOT touch a light missile. RoF chain is the
    launcher attr-51 chain only (the 35s reload is outside attr 51 by contract)."""
    caracal, rlml, ammo = ids["Caracal"], ids["Rapid Light Missile Launcher II"], \
        ids["Scourge Light Missile"]
    res = evaluate_fit(caracal, _launchers(rlml, ammo, 2))

    dmg = _attr(ammo, A.KINETIC_DAMAGE) * MISSILE_DMG_SKILL_V * WARHEAD_V  # 83 kin
    rof_s = (_attr(rlml, A.RATE_OF_FIRE) / 1000.0         # 6240 ms
             * MLO_V * RAPID_LAUNCH_V * SPEC_ROF_V * CARACAL_ROF_V)
    off = res.telemetry["offence"]
    assert off["missile_dps"] == approx(2 * dmg / rof_s)
    # Light missiles get Missile Projection but NOT the heavies-only hull trait.
    assert off["weapons"][0]["missile_velocity"] == approx(
        _attr(ammo, 37) * MISSILE_PROJ_V)


def test_fury_heavy_all_v(ids):
    """Fit 12 — 2x HML II + Scourge FURY Heavy, All V: the T2 charge takes the same
    Heavy Missiles / Warhead Upgrades damage chain and the Caracal heavy-velocity
    trait (it requires 3324), with its own base damage / flight time / explosion."""
    caracal, hml, ammo = ids["Caracal"], ids["Heavy Missile Launcher II"], \
        ids["Scourge Fury Heavy Missile"]
    res = evaluate_fit(caracal, _launchers(hml, ammo, 2))

    dmg = _attr(ammo, A.KINETIC_DAMAGE) * MISSILE_DMG_SKILL_V * WARHEAD_V  # 201 kin
    rof_s = (_attr(hml, A.RATE_OF_FIRE) / 1000.0
             * MLO_V * RAPID_LAUNCH_V * SPEC_ROF_V * CARACAL_ROF_V)
    off = res.telemetry["offence"]
    assert off["missile_dps"] == approx(2 * dmg / rof_s)
    vel = _attr(ammo, 37) * MISSILE_PROJ_V * CARACAL_HEAVY_VEL_V
    flight_s = _attr(ammo, ATTR_EXPLOSION_DELAY) / 1000.0 * MISSILE_BOMB_V  # 4875 ms
    entry = off["weapons"][0]
    assert entry["flight_time_s"] == approx(flight_s)
    assert entry["range_m"] == pytest.approx(vel * flight_s, rel=2e-3)
    assert not res.missing_skills
