"""Golden fits: WS-9 exotic weapons — smartbombs, vorton projectors, breacher pods.

Every expected value is DERIVED IN THE TEST from the fixture slice's base attributes plus
the documented mechanic — never read back from the engine. Attribute/effect ids are verified
against the SDE (scout-data §F + live type dumps) and asserted from the slice before they
drive an expectation.

Mechanics proven here
---------------------
* **Smartbomb** — an area pulse whose em/th/kin/exp damage lives on the MODULE (no charge):
  volley = Σ damage, dps = volley / duration, range = empFieldRange(99). No magazine, so
  sustained == burst; being area it auto-hits everything in range, so applied == raw ("aoe").
* **Vorton projector** — fires a Condenser Pack: volley = Σ charge damage × the projector's
  damageMultiplier(64), dps = volley / RoF, range = the charge-modified optimal. It carries
  the missile AoE attributes on the MODULE, so applied uses the missile size/velocity formula.
  The arc to secondary targets is reported (arc_range/arc_targets) but NOT counted (v1).
* **Breacher pod** — a non-stacking DoT ticking once per second for dotDuration:
  per-tick = min(dotMaxDamagePerTick, dotMaxHPPercentagePerTick% × target_total_HP). Without
  the target's HP only the flat arm is known (applied_reason "target_hp_unknown"). Multiple
  launchers do NOT stack — the fit's breacher contribution is the strongest single launcher.

Fixture: tests/fixtures/fitting/exotic.json.
"""
from __future__ import annotations

import pytest

from apps.fitting.engine import attributes as A
from apps.fitting.engine.types import (
    ModuleInput,
    OperatingProfile,
    SkillProfile,
    SlotKind,
    TargetProfile,
)

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db

NO_SKILLS = SkillProfile.from_dict({})

ATTR_DURATION = 73
ATTR_EMP_FIELD_RANGE = 99
ATTR_DAMAGE_MULTIPLIER = 64
ATTR_RATE_OF_FIRE = 51
ATTR_MAX_RANGE = 54
ATTR_CAPACITY = 38
ATTR_VOLUME = 161
ATTR_RELOAD = 1795
ATTR_WEAPON_RANGE_MULT = 120
ATTR_VORTON_ARC_RANGE = 3036
ATTR_VORTON_ARC_TARGETS = 3037
ATTR_DOT_DURATION = 5735
ATTR_DOT_MAX_DMG_TICK = 5736
ATTR_DOT_MAX_HP_PCT_TICK = 5737


@pytest.fixture()
def ids():
    return load_graph_fixture("exotic")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _has(type_id, attr_id) -> bool:
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.filter(type_id=type_id, attribute_id=attr_id).exists()


def approx(v):
    return pytest.approx(v, rel=2e-3, abs=0.1)


def _weapon(off, type_id):
    return next(w for w in off["weapons"] if w["type_id"] == type_id)


def _target(**kw):
    return OperatingProfile(propulsion_active=False, target=TargetProfile(**kw))


# --------------------------------------------------------------------------- #
# Smartbombs
# --------------------------------------------------------------------------- #
def test_smartbomb_dps_range_and_aoe_application(ids):
    """Large EMP Smartbomb I on a Rokh, NO skills. Damage is on the module (single EM type);
    dps = damage / duration; range = empFieldRange; no charge → sustained == dps; area →
    applied == dps with an ``aoe`` note. It contributes to total_dps/volley/sustained."""
    rokh, sb = ids["Rokh"], ids["Large EMP Smartbomb I"]
    em = _attr(sb, A.EM_DAMAGE)                       # 250
    cycle_s = _attr(sb, ATTR_DURATION) / 1000.0       # 10 s
    volley = em                                       # only EM populated
    dps = volley / cycle_s                            # 25
    rng = _attr(sb, ATTR_EMP_FIELD_RANGE)             # 5000
    assert em == 250.0 and cycle_s == 10.0

    off = evaluate_fit(rokh, [ModuleInput(type_id=sb, slot=SlotKind.HIGH)],
                       skills=NO_SKILLS).telemetry["offence"]
    w = _weapon(off, sb)
    assert w["kind"] == "smartbomb"
    assert w["volley"] == approx(volley)
    assert w["dps"] == approx(dps)
    assert w["range_m"] == approx(rng)
    assert w["sustained_dps"] == approx(dps)          # no magazine
    assert w["magazine_shots"] is None
    assert off["smartbomb_dps"] == approx(dps)
    assert off["total_dps"] == approx(dps)
    assert off["total_sustained_dps"] == approx(dps)

    # With a target: area weapon auto-hits, applied == raw, flagged aoe.
    off = evaluate_fit(rokh, [ModuleInput(type_id=sb, slot=SlotKind.HIGH)],
                       skills=NO_SKILLS, op=_target(signature_radius=125, velocity=0)
                       ).telemetry["offence"]
    w = _weapon(off, sb)
    assert w["applied_dps"] == approx(dps)
    assert w["applied_note"] == "aoe"
    assert off["smartbomb_dps_applied"] == approx(dps)
    assert off["total_applied_dps"] == approx(dps)


# --------------------------------------------------------------------------- #
# Vorton projectors
# --------------------------------------------------------------------------- #
def test_vorton_primary_dps_range_arc_and_application(ids):
    """Large Vorton Projector II + GalvaSurge Condenser Pack L, NO skills. Primary-target
    volley = Σ charge damage × the projector's damageMultiplier; dps = volley / RoF. The arc
    is reported but not counted. Applied uses the missile AoE formula: a target larger than
    the explosion applies in full."""
    rokh, proj, pack = (ids["Rokh"], ids["Large Vorton Projector II"],
                        ids["GalvaSurge Condenser Pack L"])
    shot = sum(_attr(pack, a) for a in
               (A.EM_DAMAGE, A.THERMAL_DAMAGE, A.KINETIC_DAMAGE, A.EXPLOSIVE_DAMAGE)
               if _has(pack, a))
    dmg_mult = _attr(proj, ATTR_DAMAGE_MULTIPLIER)         # 1.2
    rof_s = _attr(proj, ATTR_RATE_OF_FIRE) / 1000.0        # 15 s
    volley = shot * dmg_mult
    dps = volley / rof_s
    # The loaded charge's weaponRangeMultiplier scales the projector's optimal.
    expected_range = _attr(proj, ATTR_MAX_RANGE) * _attr(pack, ATTR_WEAPON_RANGE_MULT)

    off = evaluate_fit(
        rokh, [ModuleInput(type_id=proj, slot=SlotKind.HIGH, charge_type_id=pack)],
        skills=NO_SKILLS).telemetry["offence"]
    w = _weapon(off, proj)
    assert w["kind"] == "vorton"
    assert w["volley"] == approx(volley)
    assert w["dps"] == approx(dps)
    assert w["range_m"] == approx(expected_range)
    assert w["arc_range_m"] == approx(_attr(proj, ATTR_VORTON_ARC_RANGE))
    assert w["arc_targets"] == int(_attr(proj, ATTR_VORTON_ARC_TARGETS))
    assert off["vorton_dps"] == approx(dps)
    assert off["total_dps"] == approx(dps)

    # Magazine: floor(capacity/volume) shots, then reload — sustained strictly below burst.
    shots = int(round(_attr(proj, ATTR_CAPACITY) / _attr(pack, ATTR_VOLUME)))
    reload_s = _attr(proj, ATTR_RELOAD) / 1000.0
    sustained = shots * volley / (shots * rof_s + reload_s)
    assert w["magazine_shots"] == shots
    assert w["sustained_dps"] == approx(sustained)
    assert w["sustained_dps"] < w["dps"]

    # Applied: a huge, stationary target dwarfs the explosion, so the missile AoE formula
    # applies in full (min(1, sig/aoeCloudSize) = 1 when sig >> radius, velocity 0).
    off = evaluate_fit(
        rokh, [ModuleInput(type_id=proj, slot=SlotKind.HIGH, charge_type_id=pack)],
        skills=NO_SKILLS, op=_target(signature_radius=100000, velocity=0)).telemetry["offence"]
    w = _weapon(off, proj)
    assert w["applied_multiplier"] == approx(1.0)
    assert w["applied_dps"] == approx(dps)
    assert off["vorton_dps_applied"] == approx(dps)

    # A tiny stationary target applies only a fraction (the size term bites) — proving the
    # AoE application is actually wired, without pinning the evaluated explosion radius.
    off = evaluate_fit(
        rokh, [ModuleInput(type_id=proj, slot=SlotKind.HIGH, charge_type_id=pack)],
        skills=NO_SKILLS, op=_target(signature_radius=1, velocity=0)).telemetry["offence"]
    w = _weapon(off, proj)
    assert 0.0 < w["applied_multiplier"] < 1.0
    assert w["applied_dps"] == approx(dps * w["applied_multiplier"])


# --------------------------------------------------------------------------- #
# Breacher pods
# --------------------------------------------------------------------------- #
def test_breacher_flat_arm_without_target_hp(ids):
    """Small Breacher Pod Launcher + SCARAB Breacher Pod S, NO skills. Without a target HP the
    DoT reports its flat arm: dot_dps = dotMaxDamagePerTick per 1-second tick. Magazine/reload
    come off the launcher; sustained equals the (continuous) dot dps."""
    hull, launcher, pod = ids["Rokh"], ids["Small Breacher Pod Launcher"], ids["SCARAB Breacher Pod S"]
    flat = _attr(pod, ATTR_DOT_MAX_DMG_TICK)              # 200
    pct = _attr(pod, ATTR_DOT_MAX_HP_PCT_TICK)            # 0.6
    dur = _attr(pod, ATTR_DOT_DURATION) / 1000.0          # 40
    assert flat == 200.0 and pct == 0.6 and dur == 40.0

    off = evaluate_fit(
        hull, [ModuleInput(type_id=launcher, slot=SlotKind.HIGH, charge_type_id=pod)],
        skills=NO_SKILLS).telemetry["offence"]
    w = _weapon(off, launcher)
    assert w["kind"] == "breacher"
    assert w["flat_tick"] == approx(flat)
    assert w["pct_tick_of_max_hp"] == approx(pct)
    assert w["dot_duration_s"] == approx(dur)
    assert w["dot_dps"] == approx(flat)                  # flat arm (1 tick/second)
    assert w["dps"] == approx(flat)
    shots = int(round(_attr(launcher, ATTR_CAPACITY) / _attr(pod, ATTR_VOLUME)))
    assert w["magazine_shots"] == shots                 # 40
    assert w["sustained_dps"] == approx(flat)
    assert off["breacher_dps"] == approx(flat)
    assert off["total_dps"] == approx(flat)


def test_breacher_percent_hp_arm_with_target_hp(ids):
    """With the target's total HP known, the per-tick damage is min(flat, pct% × HP). A small
    target (HP where the % arm is the smaller) applies the % arm; a large target applies the
    flat cap. When a target is set without HP, the applied side falls back to the flat arm
    with an explicit reason (never silently pick one)."""
    hull, launcher, pod = ids["Rokh"], ids["Small Breacher Pod Launcher"], ids["SCARAB Breacher Pod S"]
    flat = _attr(pod, ATTR_DOT_MAX_DMG_TICK)             # 200
    pct = _attr(pod, ATTR_DOT_MAX_HP_PCT_TICK)           # 0.6

    def applied(hp=None):
        op = _target(signature_radius=40, velocity=0, target_hp=hp)
        off = evaluate_fit(
            hull, [ModuleInput(type_id=launcher, slot=SlotKind.HIGH, charge_type_id=pod)],
            skills=NO_SKILLS, op=op).telemetry["offence"]
        return off, _weapon(off, launcher)

    # Small target: 0.6% × 10000 = 60 < flat 200 → % arm wins.
    off, w = applied(hp=10000)
    expected_small = (pct / 100.0) * 10000               # 60
    assert w["applied_dps"] == approx(expected_small)
    assert w["dot_dps"] == approx(expected_small)
    assert off["breacher_dps_applied"] == approx(expected_small)

    # Large target: 0.6% × 50000 = 300 > flat 200 → flat cap wins.
    off, w = applied(hp=50000)
    assert w["applied_dps"] == approx(flat)

    # Target set, HP unknown: flat arm on the applied side, with an explicit reason.
    off, w = applied(hp=None)
    assert w["applied_dps"] == approx(flat)
    assert w["applied_reason"] == "target_hp_unknown"


def test_breacher_dot_does_not_stack_across_launchers(ids):
    """Two breacher launchers on one target do NOT stack — the fit's breacher contribution is
    the strongest single launcher, not the sum (the pod refreshes, it doesn't add). Both
    per-launcher rows are still reported."""
    hull, launcher, pod = ids["Rokh"], ids["Small Breacher Pod Launcher"], ids["SCARAB Breacher Pod S"]
    flat = _attr(pod, ATTR_DOT_MAX_DMG_TICK)
    mods = [ModuleInput(type_id=launcher, slot=SlotKind.HIGH, charge_type_id=pod)
            for _ in range(2)]
    off = evaluate_fit(hull, mods, skills=NO_SKILLS).telemetry["offence"]
    # Two rows, but the fit contribution is capped at one launcher's worth (non-stacking).
    assert len([w for w in off["weapons"] if w["kind"] == "breacher"]) == 2
    assert off["breacher_dps"] == approx(flat)           # not 2 × flat
    assert off["total_dps"] == approx(flat)
