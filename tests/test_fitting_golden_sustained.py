"""Golden fits: reload-aware sustained DPS (engine v2, real SDE slices, hand-derived).

Every expected value is DERIVED IN THE TEST from the fixture slice's base attributes
plus the documented reload mechanic — never read back from the engine.

Sustained-DPS mechanic (WS-1). A weapon with a finite magazine fires N shots, then
pays its reload before firing again, so its long-run rate is below the burst rate:

    magazine_shots  = floor( floor(capacity(38) / charge volume(161)) / chargeRate(56) )
    time_to_empty_s = magazine_shots × evaluated cycle (the same cycle burst DPS uses)
    reload_s        = evaluated reloadTime(1795) / 1000
    sustained_dps   = (magazine_shots × volley) / (time_to_empty_s + reload_s)

so  sustained_dps / burst_dps == time_to_empty_s / (time_to_empty_s + reload_s).

Frequency crystals depend on the CHARGE's crystalsGetDamaged(786): when 0 (T1 lenses
and every faction/T2 lens the SDE flags permanent) the crystal never depletes, so
sustained == burst and the magazine fields are null; when 1 the lens wears out after
floor(rounds × hp(9) / (crystalVolatilityDamage(784) × crystalVolatilityChance(783)))
shots. Drones carry no magazine, so they sustain by definition and contribute their
full DPS to total_sustained_dps.

Attribute ids verified against the SDE (scout-data §B). Reload/magazine semantics were
studied for behaviour only in pyfa's GPL eos (saveddata/module.py:196-302); no code was
reused.
"""
from __future__ import annotations

import math

import pytest

from apps.fitting.engine import attributes as A
from apps.fitting.engine.types import ModuleInput, ModuleState, SkillProfile, SlotKind

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db

NO_SKILLS = SkillProfile.from_dict({})

ATTR_CAPACITY = 38
ATTR_VOLUME = 161
ATTR_CHARGE_RATE = 56
ATTR_RELOAD_TIME = 1795
ATTR_CRYSTALS_GET_DAMAGED = 786
ATTR_CRYSTAL_VOL_CHANCE = 783
ATTR_CRYSTAL_VOL_DAMAGE = 784
ATTR_CHARGE_HP = 9


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


def _magazine_shots(weapon, ammo) -> int:
    """floor(floor(capacity/volume) / chargeRate) — matches the engine's ammo path."""
    rounds = int(round(_attr(weapon, ATTR_CAPACITY) / _attr(ammo, ATTR_VOLUME), 6))
    return math.floor(rounds / _attr(weapon, ATTR_CHARGE_RATE))


def approx(v):
    return pytest.approx(v, rel=2e-3, abs=0.06)


# --------------------------------------------------------------------------- #
# (a) Autocannon — finite magazine, sustained below burst by the reload ratio
# --------------------------------------------------------------------------- #
@pytest.fixture()
def rifter_ids():
    return load_graph_fixture("rifter_ac")


def test_autocannon_sustained_below_burst_by_reload_ratio(rifter_ids):
    """3x 150mm AutoCannon II + Republic Fleet EMP S, NO skills. The magazine holds
    floor(capacity/volume) rounds; after it empties the gun pays reloadTime before
    firing again, so sustained = magazine_dmg / (time_to_empty + reload) < burst."""
    rifter, gun, ammo = rifter_ids["Rifter"], rifter_ids["150mm Light AutoCannon II"], \
        rifter_ids["Republic Fleet EMP S"]
    mods = [ModuleInput(type_id=gun, slot=SlotKind.HIGH, charge_type_id=ammo)
            for _ in range(3)]
    res = evaluate_fit(rifter, mods, skills=NO_SKILLS)
    off = res.telemetry["offence"]

    shot = _shot(ammo)
    volley = shot * _attr(gun, A.DAMAGE_MULTIPLIER)         # turret damageMultiplier(64)
    cycle_s = _attr(gun, A.RATE_OF_FIRE) / 1000.0           # base RoF, no skills
    burst = volley / cycle_s
    shots = _magazine_shots(gun, ammo)
    reload_s = _attr(gun, ATTR_RELOAD_TIME) / 1000.0        # 10000 ms -> 10 s
    time_to_empty = shots * cycle_s
    sustained = shots * volley / (time_to_empty + reload_s)

    w = off["weapons"][0]
    assert w["magazine_shots"] == shots
    assert w["reload_s"] == approx(reload_s)
    assert w["time_to_empty_s"] == approx(time_to_empty)
    assert w["sustained_dps"] == approx(sustained)
    assert w["dps"] == approx(burst)
    # The exact reload ratio, and sustained is strictly below burst.
    assert sustained < burst
    assert w["sustained_dps"] / w["dps"] == pytest.approx(
        time_to_empty / (time_to_empty + reload_s), rel=3e-3)

    # Fit total: three identical guns; burst total_dps is untouched by the feature.
    assert off["total_dps"] == approx(3 * burst)
    assert off["total_sustained_dps"] == approx(3 * sustained)
    assert off["total_sustained_dps"] < off["total_dps"]


def test_empty_gun_has_no_sustained_fields(rifter_ids):
    """A charge-less gun does not fire: it produces no weapon entry (hence no sustained
    fields) and contributes nothing to total_sustained_dps."""
    rifter, gun = rifter_ids["Rifter"], rifter_ids["150mm Light AutoCannon II"]
    res = evaluate_fit(rifter, [ModuleInput(type_id=gun, slot=SlotKind.HIGH)],
                       skills=NO_SKILLS)
    off = res.telemetry["offence"]
    assert off["weapons"] == []
    assert off["total_sustained_dps"] == 0.0
    assert any(d.code == "missing_ammo" for d in res.diagnostics)


# --------------------------------------------------------------------------- #
# (b) Missile launchers — the long reload dominates on rapid-launch weapons
# --------------------------------------------------------------------------- #
@pytest.fixture()
def missile_ids():
    return load_graph_fixture("missiles_graph")


def test_heavy_missile_launcher_sustained(missile_ids):
    """5x Heavy Missile Launcher II + Scourge Heavy, NO skills. Launchers apply no
    damageMultiplier (volley == charge damage); reload 10 s over a 40-round magazine."""
    caracal, hml, ammo = missile_ids["Caracal"], \
        missile_ids["Heavy Missile Launcher II"], missile_ids["Scourge Heavy Missile"]
    mods = [ModuleInput(type_id=hml, slot=SlotKind.HIGH, charge_type_id=ammo)
            for _ in range(5)]
    res = evaluate_fit(caracal, mods, skills=NO_SKILLS)
    off = res.telemetry["offence"]

    volley = _attr(ammo, A.KINETIC_DAMAGE)                  # kinetic-only charge
    cycle_s = _attr(hml, A.RATE_OF_FIRE) / 1000.0
    burst = volley / cycle_s
    shots = _magazine_shots(hml, ammo)                      # floor(1.2/0.03) = 40
    reload_s = _attr(hml, ATTR_RELOAD_TIME) / 1000.0        # 10 s
    time_to_empty = shots * cycle_s
    sustained = shots * volley / (time_to_empty + reload_s)

    w = off["weapons"][0]
    assert w["magazine_shots"] == shots
    assert w["sustained_dps"] == approx(sustained)
    assert off["total_sustained_dps"] == approx(5 * sustained)
    assert off["total_dps"] == approx(5 * burst)


def test_rapid_light_launcher_long_reload_dominates(missile_ids):
    """2x Rapid Light Missile Launcher II + Scourge Light, NO skills. RLMLs carry a
    35 s reload against a 20-round magazine, so sustained falls well below burst — the
    signature case where reload-aware DPS matters most."""
    caracal, rlml, ammo = missile_ids["Caracal"], \
        missile_ids["Rapid Light Missile Launcher II"], \
        missile_ids["Scourge Light Missile"]
    mods = [ModuleInput(type_id=rlml, slot=SlotKind.HIGH, charge_type_id=ammo)
            for _ in range(2)]
    res = evaluate_fit(caracal, mods, skills=NO_SKILLS)
    off = res.telemetry["offence"]

    volley = _attr(ammo, A.KINETIC_DAMAGE)
    cycle_s = _attr(rlml, A.RATE_OF_FIRE) / 1000.0
    burst = volley / cycle_s
    shots = _magazine_shots(rlml, ammo)                     # floor(0.3/0.015) = 20
    reload_s = _attr(rlml, ATTR_RELOAD_TIME) / 1000.0       # 35 s
    time_to_empty = shots * cycle_s
    sustained = shots * volley / (time_to_empty + reload_s)

    w = off["weapons"][0]
    assert w["magazine_shots"] == shots
    assert w["reload_s"] == approx(reload_s)
    assert w["sustained_dps"] == approx(sustained)
    # The reload is a large fraction of the fire cycle: sustained is well under burst.
    assert w["sustained_dps"] / w["dps"] < 0.85
    assert off["total_sustained_dps"] == approx(2 * sustained)


# --------------------------------------------------------------------------- #
# (c)/(d) Frequency crystals — permanent vs depleting
# --------------------------------------------------------------------------- #
@pytest.fixture()
def crystal_ids():
    return load_graph_fixture("laser_crystals")


def test_permanent_crystal_sustained_equals_burst(crystal_ids):
    """Focused Medium Pulse Laser I + Multifrequency M, NO skills. The lens is flagged
    crystalsGetDamaged=0 -> permanent -> the gun never reloads, so sustained == burst
    and the magazine/time/reload fields are null (there is no magazine to empty)."""
    omen, gun, crystal = crystal_ids["Omen"], \
        crystal_ids["Focused Medium Pulse Laser I"], crystal_ids["Multifrequency M"]
    assert _attr(crystal, ATTR_CRYSTALS_GET_DAMAGED) == 0.0
    assert not _has(gun, ATTR_CHARGE_RATE)                  # no chargeRate -> crystal path

    res = evaluate_fit(omen, [ModuleInput(type_id=gun, slot=SlotKind.HIGH,
                                          charge_type_id=crystal)], skills=NO_SKILLS)
    off = res.telemetry["offence"]

    volley = _shot(crystal) * _attr(gun, A.DAMAGE_MULTIPLIER)
    burst = volley / (_attr(gun, A.RATE_OF_FIRE) / 1000.0)

    w = off["weapons"][0]
    assert w["magazine_shots"] is None
    assert w["time_to_empty_s"] is None
    assert w["reload_s"] is None
    assert w["sustained_dps"] == approx(burst)
    assert w["sustained_dps"] == w["dps"]
    assert off["total_sustained_dps"] == approx(burst)
    assert off["total_sustained_dps"] == off["total_dps"]


def test_depleting_crystal_reports_expected_shot_life(crystal_ids):
    """Focused Medium Pulse Laser I + Scorch M, NO skills. Scorch is crystalsGetDamaged=1
    -> it wears out after floor(rounds × hp / (volDmg × volChance)) shots. The lens swap
    is near-instant (reloadTime ~0.01 ms), so sustained stays essentially at burst, but
    the finite magazine_shots proves the depleting-crystal branch (not the permanent
    branch, which reports magazine_shots=None) executed."""
    omen, gun, crystal = crystal_ids["Omen"], \
        crystal_ids["Focused Medium Pulse Laser I"], crystal_ids["Scorch M"]
    assert _attr(crystal, ATTR_CRYSTALS_GET_DAMAGED) == 1.0

    res = evaluate_fit(omen, [ModuleInput(type_id=gun, slot=SlotKind.HIGH,
                                          charge_type_id=crystal)], skills=NO_SKILLS)
    off = res.telemetry["offence"]

    rounds = int(round(_attr(gun, ATTR_CAPACITY) / _attr(crystal, ATTR_VOLUME), 6))
    denom = _attr(crystal, ATTR_CRYSTAL_VOL_DAMAGE) * _attr(crystal, ATTR_CRYSTAL_VOL_CHANCE)
    expected_shots = math.floor(rounds * _attr(crystal, ATTR_CHARGE_HP) / denom)
    volley = _shot(crystal) * _attr(gun, A.DAMAGE_MULTIPLIER)
    burst = volley / (_attr(gun, A.RATE_OF_FIRE) / 1000.0)

    w = off["weapons"][0]
    assert w["magazine_shots"] == expected_shots           # 1000 expected shots
    assert w["magazine_shots"] is not None
    # Near-instant reload -> sustained rounds to burst, and never exceeds it.
    assert w["sustained_dps"] == approx(burst)
    assert w["sustained_dps"] <= w["dps"] + 0.06


# --------------------------------------------------------------------------- #
# Drones sustain by definition (no magazine) — they enter total_sustained_dps whole
# --------------------------------------------------------------------------- #
def test_drones_contribute_full_dps_to_sustained_total():
    """Vexor + 5x Hammerhead II, All V. Drones do not reload, so a drone-only fit's
    sustained total equals its burst total (both are the full drone DPS)."""
    ids = load_graph_fixture("vexor_drones")
    vexor, drone = ids["Vexor"], ids["Hammerhead II"]
    mods = [ModuleInput(type_id=drone, slot=SlotKind.DRONE, quantity=5)]
    res = evaluate_fit(vexor, mods)
    off = res.telemetry["offence"]
    assert off["drone_dps"] > 0
    assert off["total_sustained_dps"] == approx(off["total_dps"])
    assert off["total_sustained_dps"] == approx(off["drone_dps"])
