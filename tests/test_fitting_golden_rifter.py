"""Golden fit: Rifter + autocannons (engine v2, real SDE slice, hand-derived numbers).

Pattern for the golden-fit matrix: the fixture is a real CCP data slice
(tests/fixtures/fitting/rifter_ac.json, extracted through the normal pipeline);
every expected value is DERIVED IN THE TEST from the slice's base attributes plus
documented EVE mechanics (stacking S(i)=exp(-(i/2.67)^2), skill/trait percentages
cited inline) — never read back from the engine.
"""
from __future__ import annotations

import math

import pytest

from apps.fitting.engine import attributes as A
from apps.fitting.engine.types import ModuleInput, ModuleState, SkillProfile, SlotKind

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db

S1 = math.exp(-((1 / 2.67) ** 2))          # 0.8691… second-module effectiveness
NAVIGATION_V = 1.25                        # Navigation: +5% max velocity / level
SHIELD_MGMT_V = 1.25                       # Shield Management: +5% shield capacity / level
CAP_MGMT_V = 1.25                          # Capacitor Management: +5% capacitor / level
RIFTER_ROF_PER_LEVEL = -0.075              # Rifter trait: -7.5% small projectile RoF / level
GUNNERY_V = 0.90                           # Gunnery: -2% turret RoF time / level
RAPID_FIRING_V = 0.80                      # Rapid Firing: -4% turret RoF time / level
SURGICAL_STRIKE_V = 1.15                   # Surgical Strike: +3% turret damage / level
SMALL_PROJECTILE_V = 1.25                  # Small Projectile Turret: +5% damage / level
SMALL_AC_SPEC_V = 1.10                     # Small Autocannon Spec: +2% damage / level (T2 gun)


@pytest.fixture()
def ids():
    return load_graph_fixture("rifter_ac")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def test_bare_hull_all_v(ids):
    rifter = ids["Rifter"]
    res = evaluate_fit(rifter, [])
    t = res.telemetry
    assert res.status.value in ("valid", "warnings")
    assert t["mobility"]["max_velocity"] == pytest.approx(
        _attr(rifter, A.MAX_VELOCITY) * NAVIGATION_V, rel=1e-3)
    assert t["defence"]["layers"]["shield"]["hp"] == pytest.approx(
        _attr(rifter, A.SHIELD_HP) * SHIELD_MGMT_V, rel=1e-3)
    assert t["capacitor"]["capacity"] == pytest.approx(
        _attr(rifter, A.CAP_CAPACITY) * CAP_MGMT_V, rel=1e-3)
    # Corrected recharge model: peak = 2.5 * C / tau.
    cap = t["capacitor"]
    assert cap["peak_recharge"] == pytest.approx(
        2.5 * cap["capacity"] / cap["recharge_s"], rel=1e-2)


def test_untrained_gun_dps_with_gyros_and_rig(ids):
    """3x 150mm AC II + RF EMP S + 2x Gyro II + Burst Aerator, NO skills: the whole
    chain is module-only and fully hand-computable from base attributes."""
    rifter, gun, ammo = ids["Rifter"], ids["150mm Light AutoCannon II"], \
        ids["Republic Fleet EMP S"]
    gyro, rig = ids["Gyrostabilizer II"], ids["Small Projectile Burst Aerator I"]
    mods = [ModuleInput(type_id=gun, slot=SlotKind.HIGH, charge_type_id=ammo)
            for _ in range(3)]
    mods += [ModuleInput(type_id=gyro, slot=SlotKind.LOW, state=ModuleState.ONLINE)
             for _ in range(2)]
    mods += [ModuleInput(type_id=rig, slot=SlotKind.RIG, state=ModuleState.ONLINE)]
    res = evaluate_fit(rifter, mods, skills=SkillProfile.from_dict({}))

    shot = sum(_attr(ammo, a) for a in
               (A.EM_DAMAGE, A.THERMAL_DAMAGE, A.KINETIC_DAMAGE, A.EXPLOSIVE_DAMAGE)
               if _has(ammo, a))
    base_mult = _attr(gun, A.DAMAGE_MULTIPLIER)
    g = _attr(gyro, A.DAMAGE_MULTIPLIER) - 1.0          # +10.5% (T2)
    dmg_mult = base_mult * (1 + g) * (1 + g * S1)       # 2 gyros, stacking-penalised
    rof_ms = _attr(gun, A.RATE_OF_FIRE)
    gy_rof = _attr(gyro, A.ROF_MULTIPLIER) - 1.0        # -10.5%
    rig_rof = _attr(rig, A.ROF_MULTIPLIER) - 1.0        # -10%
    # Gyro chain penalised together; the rig's speedMultiplier joins the same
    # non-stackable attribute chain (strongest first: gyro 10.5% > rig 10%).
    order = sorted([gy_rof, gy_rof, rig_rof], key=abs, reverse=True)
    rof = rof_ms
    for i, v in enumerate(order):
        rof *= 1 + v * (S1 ** (i * i))
    expected_dps = 3 * (shot * dmg_mult) / (rof / 1000.0)
    assert res.telemetry["offence"]["total_dps"] == pytest.approx(expected_dps, rel=2e-3)


def test_dcu_offline_vs_online(ids):
    """A Damage Control II contributes hull resists when online and nothing offline."""
    rifter, dcu = ids["Rifter"], ids["Damage Control II"]
    off = evaluate_fit(rifter, [ModuleInput(type_id=dcu, slot=SlotKind.LOW,
                                            state=ModuleState.OFFLINE)],
                       skills=SkillProfile.from_dict({}))
    on = evaluate_fit(rifter, [ModuleInput(type_id=dcu, slot=SlotKind.LOW,
                                           state=ModuleState.ONLINE)],
                      skills=SkillProfile.from_dict({}))
    hull_off = off.telemetry["defence"]["layers"]["hull"]["resists"]["em"]
    hull_on = on.telemetry["defence"]["layers"]["hull"]["resists"]["em"]
    base_res = _attr(rifter, A.HULL_RESONANCE["em"])    # modern hulls: 0.67 (33%)
    assert hull_off == pytest.approx((1 - base_res) * 100, abs=0.2)
    expected = (1.0 - base_res * _attr(dcu, A.HULL_RESONANCE_MODULE["em"])) * 100
    assert hull_on == pytest.approx(expected, abs=0.2)


def _has(type_id, attr_id) -> bool:
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.filter(type_id=type_id, attribute_id=attr_id).exists()
