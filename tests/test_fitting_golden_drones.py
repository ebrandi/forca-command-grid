"""Golden fits: drones (engine v2, real SDE slice, hand-derived numbers).

Fixture: tests/fixtures/fitting/vexor_drones.json — a real CCP data slice extracted
through the normal pipeline (Vexor, T2 drones, Drone Damage Amplifier II,
Gyrostabilizer II + every transitively required skill). Every expected value is
DERIVED IN THE TEST from the slice's base attributes plus documented EVE mechanics —
never read back from the engine.

Drone damage chain (all verified against the live dev DB's dogma rows):
* Drone DPS = sum(damage attrs 114/116/117/118) x damageMultiplier(64) / (speed(51)/1000).
* Vexor hull trait: effect 2188 (shipBonusDroneDamageMultiplierGC2) postPercent(op 6)
  on attr 64 of everything requiring Drones(3436), magnitude = ship attr 658
  (shipBonusGC2 = 10) preMultiplied by Gallente Cruiser(3332) level (effect 928).
* Drone Interfacing(3442): effect 6663 postPercent on attr 64 of Drones(3436)-requirers,
  magnitude = its attr 292 (10) x level (effect 146 preMul by skillLevel 280).
* Size/spec skills (Medium/Light/Heavy Drone Operation, Sentry Drone Interfacing,
  racial Drone Specializations): effect 1730 (droneDmgBonus, the documented
  client-internal patch) postPercent on attr 64 of entities requiring THAT skill,
  magnitude = own attr 292 x level.
* Drone Damage Amplifier II: effect 6556 (category 4 = online) postPercent on attr 64
  of Drones(3436)-requirers, magnitude attr 1255 (20.5). Module source (category 7),
  attr 64 stackable=false -> multiple DDAs are stacking-penalised together; ship and
  skill sources are exempt.
"""
from __future__ import annotations

import math

import pytest

from apps.fitting.engine.types import ModuleInput, ModuleState, SkillProfile, SlotKind

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db

S1 = math.exp(-((1 / 2.67) ** 2))    # second stacking-penalised module effectiveness

# Dogma attribute ids (CCP SDE) used by the derivations below.
DMG_MULT = 64            # damageMultiplier (stackable=false, verified in the slice)
ROF = 51                 # speed: ms between drone attack cycles
DAMAGE_ATTRS = (114, 116, 117, 118)   # em / explosive / kinetic / thermal
SKILL_DMG_BONUS = 292    # damageMultiplierBonus (%/level on the drone skills)
SHIP_GC2 = 658           # shipBonusGC2 on the Vexor (%/Gallente Cruiser level)
DDA_BONUS = 1255         # droneDamageBonus on Drone Damage Amplifiers (%)
BW_USED = 1272           # droneBandwidthUsed (Mbit/s per drone)
VOLUME = 161             # drone volume (m3)


@pytest.fixture()
def ids():
    return load_graph_fixture("vexor_drones")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _shot(type_id) -> float:
    return sum(_attr(type_id, a) for a in DAMAGE_ATTRS)


def _base_dps(type_id, qty=1) -> float:
    """Unmodified drone DPS from base attributes alone."""
    return qty * _shot(type_id) * _attr(type_id, DMG_MULT) / (_attr(type_id, ROF) / 1000.0)


def _drones(type_id, n, state=ModuleState.ACTIVE):
    return [ModuleInput(type_id=type_id, slot=SlotKind.DRONE, state=state)
            for _ in range(n)]


def _all_v_factor(ids, size_skill_name, spec_skill_name) -> float:
    """The all-V damage multiplier chain for a T2 drone on a Vexor: hull trait x
    Drone Interfacing x size-operation skill x racial specialization. All four are
    postPercent from ship/skill sources (stacking-exempt categories 6/16), each
    percentage = the bonus attribute in the DB x level 5."""
    hull = _attr(ids["Vexor"], SHIP_GC2) * 5 / 100.0            # 10%/lvl (attr 658)
    di = _attr(ids["Drone Interfacing"], SKILL_DMG_BONUS) * 5 / 100.0   # 10%/lvl
    size = _attr(ids[size_skill_name], SKILL_DMG_BONUS) * 5 / 100.0     # 5%/lvl
    spec = _attr(ids[spec_skill_name], SKILL_DMG_BONUS) * 5 / 100.0     # 2%/lvl
    return (1 + hull) * (1 + di) * (1 + size) * (1 + spec)


# --------------------------------------------------------------------------- #
# Isolation: base drone maths, no skills
# --------------------------------------------------------------------------- #
def test_hammerheads_untrained_base_dps(ids):
    """5x Hammerhead II with NO skills: every skill entity sits at level 0, so its
    bonus attribute (292 x level, 658 x level) evaluates to 0 and the drones fire at
    raw base attributes: 32 thermal x 1.92 / 4.0 s each."""
    vexor, hh = ids["Vexor"], ids["Hammerhead II"]
    res = evaluate_fit(vexor, _drones(hh, 5), skills=SkillProfile.from_dict({}))
    off = res.telemetry["offence"]
    assert off["turret_dps"] == 0.0
    assert off["missile_dps"] == 0.0
    assert off["drone_dps"] == pytest.approx(_base_dps(hh, 5), rel=2e-3)   # 76.8
    assert off["total_dps"] == off["drone_dps"]
    assert res.status.value == "missing_skills"    # T2 drones need trained skills


def test_hammerheads_all_v(ids):
    """5x Hammerhead II, all skills V. Chain (each % read from the DB):
    hull 10%/lvl (Vexor attr 658, effect 2188) + Drone Interfacing 10%/lvl (attr 292,
    effect 6663) + Medium Drone Operation 5%/lvl (attr 292, effect 1730) + Gallente
    Drone Specialization 2%/lvl (attr 292, effect 1730):
    1.92 x 1.5 x 1.5 x 1.25 x 1.10 = 5.94 -> 5 x 32 x 5.94/4 = 237.6 dps."""
    vexor, hh = ids["Vexor"], ids["Hammerhead II"]
    res = evaluate_fit(vexor, _drones(hh, 5))      # omniscient() default
    factor = _all_v_factor(ids, "Medium Drone Operation", "Gallente Drone Specialization")
    expected = _base_dps(hh, 5) * factor
    assert res.telemetry["offence"]["drone_dps"] == pytest.approx(expected, rel=2e-3)
    r = res.telemetry["resources"]
    assert r["drone_bandwidth"] == pytest.approx(_attr(vexor, 1271))       # 75 Mbit
    assert r["drone_bandwidth_used"] == pytest.approx(5 * _attr(hh, BW_USED))  # 50
    assert r["drone_bay_used"] == pytest.approx(5 * _attr(hh, VOLUME))     # 50 m3
    assert res.status.value in ("valid", "warnings")


def test_drone_interfacing_per_level(ids):
    """Drone Interfacing alone (levels 3 and 5): x(1 + 10 x level / 100) over the
    untrained baseline. The 10%/level is the skill's own attr 292 in the DB,
    per-level scaling via effect 146 (preMul of 292 by skillLevel 280)."""
    vexor, hh, di = ids["Vexor"], ids["Hammerhead II"], ids["Drone Interfacing"]
    pct = _attr(di, SKILL_DMG_BONUS)               # 10 (% per level)
    for level in (3, 5):
        res = evaluate_fit(vexor, _drones(hh, 5),
                           skills=SkillProfile.from_dict({di: level}))
        expected = _base_dps(hh, 5) * (1 + pct * level / 100.0)
        assert res.telemetry["offence"]["drone_dps"] == pytest.approx(expected, rel=2e-3)


def test_hull_trait_per_level(ids):
    """Gallente Cruiser 4 alone: the Vexor's drone-damage trait is ship attr 658
    (shipBonusGC2 = 10) preMultiplied by the racial skill's level (effect 928), then
    applied postPercent to every Drones-requiring entity (effect 2188) -> x1.4."""
    vexor, hh, gc = ids["Vexor"], ids["Hammerhead II"], ids["Gallente Cruiser"]
    res = evaluate_fit(vexor, _drones(hh, 5), skills=SkillProfile.from_dict({gc: 4}))
    expected = _base_dps(hh, 5) * (1 + _attr(vexor, SHIP_GC2) * 4 / 100.0)
    assert res.telemetry["offence"]["drone_dps"] == pytest.approx(expected, rel=2e-3)


# --------------------------------------------------------------------------- #
# Isolation: Drone Damage Amplifiers via their real graph modifiers
# --------------------------------------------------------------------------- #
def test_dda_online_applies_offline_does_not(ids):
    """One DDA II: effect 6556 is category 4 (online), so an ONLINE module already
    grants its +20.5% (attr 1255); OFFLINE it grants nothing."""
    vexor, hh, dda = ids["Vexor"], ids["Hammerhead II"], ids["Drone Damage Amplifier II"]
    base = _base_dps(hh, 5)
    bonus = _attr(dda, DDA_BONUS) / 100.0          # 0.205
    on = evaluate_fit(vexor, _drones(hh, 5) + [
        ModuleInput(type_id=dda, slot=SlotKind.LOW, state=ModuleState.ONLINE)],
        skills=SkillProfile.from_dict({}))
    off = evaluate_fit(vexor, _drones(hh, 5) + [
        ModuleInput(type_id=dda, slot=SlotKind.LOW, state=ModuleState.OFFLINE)],
        skills=SkillProfile.from_dict({}))
    assert on.telemetry["offence"]["drone_dps"] == pytest.approx(
        base * (1 + bonus), rel=2e-3)              # 76.8 x 1.205 = 92.544
    assert off.telemetry["offence"]["drone_dps"] == pytest.approx(base, rel=2e-3)


def test_two_ddas_stacking_penalised(ids):
    """Two DDA IIs: both modify the drones' damageMultiplier (attr 64,
    stackable=false) from a module source (category 7, not exempt), so the second
    is penalised: x(1 + 0.205) x (1 + 0.205 x S1)."""
    vexor, hh, dda = ids["Vexor"], ids["Hammerhead II"], ids["Drone Damage Amplifier II"]
    b = _attr(dda, DDA_BONUS) / 100.0
    mods = _drones(hh, 5) + [
        ModuleInput(type_id=dda, slot=SlotKind.LOW, state=ModuleState.ONLINE)
        for _ in range(2)]
    res = evaluate_fit(vexor, mods, skills=SkillProfile.from_dict({}))
    expected = _base_dps(hh, 5) * (1 + b) * (1 + b * S1)      # ~109.0 dps
    assert res.telemetry["offence"]["drone_dps"] == pytest.approx(expected, rel=2e-3)


# --------------------------------------------------------------------------- #
# Bandwidth / bay gating
# --------------------------------------------------------------------------- #
def test_bandwidth_gating_five_heavies(ids):
    """5 Ogre II (25 Mbit each) on the Vexor's 75 Mbit: only 3 launch and count for
    DPS; the fit reports both the over-bandwidth error and the truncation warning.
    Bay is exactly full (5 x 25 m3 = 125) so no bay diagnostic."""
    vexor, ogre = ids["Vexor"], ids["Ogre II"]
    res = evaluate_fit(
        vexor, [ModuleInput(type_id=ogre, slot=SlotKind.DRONE, quantity=5)],
        skills=SkillProfile.from_dict({}))
    r = res.telemetry["resources"]
    assert r["drone_bandwidth_used"] == pytest.approx(5 * _attr(ogre, BW_USED))  # 125
    assert r["drone_bay_used"] == pytest.approx(5 * _attr(ogre, VOLUME))         # 125
    codes = {d.code for d in res.diagnostics}
    assert "drone_bandwidth_exceeded" in codes
    assert "drone_bay_exceeded" not in codes
    over = next(d for d in res.diagnostics if d.code == "drones_over_bandwidth")
    assert over.params["counted"] == 3             # int(75 // 25)
    assert over.params["requested"] == 5
    assert res.status.value == "over_resources"
    # Only the 3 launchable heavies add DPS: 3 x 64 x 1.92 / 4.0 = 92.16.
    assert res.telemetry["offence"]["drone_dps"] == pytest.approx(
        _base_dps(ogre, 3), rel=2e-3)


def test_drone_bay_volume_overflow(ids):
    """6 Ogre II = 150 m3 in a 125 m3 bay: structural error, fit impossible."""
    vexor, ogre = ids["Vexor"], ids["Ogre II"]
    res = evaluate_fit(
        vexor, [ModuleInput(type_id=ogre, slot=SlotKind.DRONE, quantity=6)],
        skills=SkillProfile.from_dict({}))
    assert res.telemetry["resources"]["drone_bay_used"] == pytest.approx(
        6 * _attr(ogre, VOLUME))                   # 150 > 125
    codes = {d.code for d in res.diagnostics}
    assert "drone_bay_exceeded" in codes
    assert "drone_bandwidth_exceeded" in codes     # 150 Mbit > 75 as well
    assert res.status.value == "impossible"


# --------------------------------------------------------------------------- #
# Realistic fits
# --------------------------------------------------------------------------- #
def test_mixed_flight_all_v(ids):
    """2x Hammerhead II + 2x Warrior II + 1x Ogre II, all V (55 of 75 Mbit).
    Per-size chains: mediums scale by Medium Drone Operation + Gallente spec,
    lights (Warrior) by Light Drone Operation + Minmatar spec (12485), heavies by
    Heavy Drone Operation + Gallente spec — hull trait and Drone Interfacing apply
    to all. Warrior II deals explosive; the rest thermal."""
    vexor = ids["Vexor"]
    hh, war, ogre = ids["Hammerhead II"], ids["Warrior II"], ids["Ogre II"]
    mods = _drones(hh, 2) + _drones(war, 2) + _drones(ogre, 1)
    res = evaluate_fit(vexor, mods)
    dps_hh = _base_dps(hh, 2) * _all_v_factor(
        ids, "Medium Drone Operation", "Gallente Drone Specialization")
    dps_war = _base_dps(war, 2) * _all_v_factor(
        ids, "Light Drone Operation", "Minmatar Drone Specialization")
    dps_ogre = _base_dps(ogre, 1) * _all_v_factor(
        ids, "Heavy Drone Operation", "Gallente Drone Specialization")
    total = dps_hh + dps_war + dps_ogre            # ~238.3
    off = res.telemetry["offence"]
    assert off["drone_dps"] == pytest.approx(total, rel=2e-3)
    assert off["total_dps"] == pytest.approx(total, rel=2e-3)
    # Damage split: Hammerhead/Ogre are pure thermal (attr 118), Warrior pure
    # explosive (attr 116) — distribution follows the per-type DPS shares.
    dist = off["damage_distribution"]
    assert dist["thermal"] == pytest.approx((dps_hh + dps_ogre) / total * 100, abs=0.1)
    assert dist["explosive"] == pytest.approx(dps_war / total * 100, abs=0.1)
    assert dist["em"] == 0.0 and dist["kinetic"] == 0.0
    assert res.telemetry["resources"]["drone_bandwidth_used"] == pytest.approx(
        2 * _attr(hh, BW_USED) + 2 * _attr(war, BW_USED) + _attr(ogre, BW_USED))
    assert res.status.value in ("valid", "warnings")


def test_sentry_garde_untrained_and_all_v(ids):
    """One Garde II sentry. Untrained: 64 x 1.65 / 4.0 = 26.4 dps. All V the chain
    is hull 10%/lvl + Drone Interfacing 10%/lvl + Sentry Drone Interfacing 5%/lvl
    (attr 292, effect 1730) + Gallente spec 2%/lvl -> 64 x 1.65 x 3.09375 / 4."""
    vexor, garde = ids["Vexor"], ids["Garde II"]
    res0 = evaluate_fit(vexor, _drones(garde, 1), skills=SkillProfile.from_dict({}))
    assert res0.telemetry["offence"]["drone_dps"] == pytest.approx(
        _base_dps(garde, 1), rel=2e-3)
    res5 = evaluate_fit(vexor, _drones(garde, 1))
    expected = _base_dps(garde, 1) * _all_v_factor(
        ids, "Sentry Drone Interfacing", "Gallente Drone Specialization")
    assert res5.telemetry["offence"]["drone_dps"] == pytest.approx(expected, rel=2e-3)


def test_drones_in_bay_contribute_nothing(ids):
    """5 Hammerhead II carried in the bay (state=offline): inert — no DPS, no
    bandwidth, no drone slot usage, and the fit reports no weapons."""
    vexor, hh = ids["Vexor"], ids["Hammerhead II"]
    res = evaluate_fit(vexor, _drones(hh, 5, state=ModuleState.OFFLINE))
    off = res.telemetry["offence"]
    assert off["drone_dps"] == 0.0
    assert off["total_dps"] == 0.0
    r = res.telemetry["resources"]
    assert r["drone_bandwidth_used"] == 0.0
    assert r["slots"]["used"]["drone"] == 0
    assert "no_weapons_detected" in res.unsupported
    assert res.status.value == "valid"


def test_turret_damage_mods_do_not_affect_drones(ids):
    """Gyrostabilizer II modifies damageMultiplier/speed ONLY via
    LocationGroupModifier group 55 (Projectile Weapon — effects 89/92 in the slice),
    and the Vexor's turret trait (effect 562) filters on Medium Hybrid Turret skill
    3304 — drones (group 100, requiring drone skills) match neither, so two fitted
    gyros change drone DPS by exactly nothing."""
    vexor, hh, gyro = ids["Vexor"], ids["Hammerhead II"], ids["Gyrostabilizer II"]
    plain = evaluate_fit(vexor, _drones(hh, 5), skills=SkillProfile.from_dict({}))
    gyroed = evaluate_fit(
        vexor,
        _drones(hh, 5) + [ModuleInput(type_id=gyro, slot=SlotKind.LOW,
                                      state=ModuleState.ONLINE) for _ in range(2)],
        skills=SkillProfile.from_dict({}))
    assert gyroed.telemetry["offence"]["drone_dps"] == \
        plain.telemetry["offence"]["drone_dps"]
    assert gyroed.telemetry["offence"]["drone_dps"] == pytest.approx(
        _base_dps(hh, 5), rel=2e-3)
    assert gyroed.telemetry["offence"]["turret_dps"] == 0.0
