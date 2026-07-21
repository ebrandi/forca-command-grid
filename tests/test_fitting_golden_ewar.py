"""Golden fits: WS-10 EWAR application (engine v2, real SDE slices).

Every expectation is hand-derived from the fixture's own base attributes and the documented
EVE mechanics — never read back from the engine. Attribute ids and base values are asserted
from the slice before they drive an expectation, so a fixture drift fails loudly.

Mechanics proven here
---------------------
* **Consolidated ewar section**: one entry per OUR active offensive-ewar module, classified by
  its DEFAULT (identifying) effect id — jammers (remoteECMFalloff 6470), burst jammers
  (ECMBurstJammer 6714), dampeners (6422), painters (6425), webs (6426), warp scram/disrupt
  (5934/39) and tracking/guidance disruptors (6424/6423). Each carries its evaluated strength
  attribute(s), range, cycle and cap/cycle.
* **ECM jam chance**: per jammer, per sensor type, ``min(1, strength(238-241) / target sensor
  strength)``; combined across jammers as independent per-cycle rolls ``1 − Π(1 − p_i)``
  (pyfa eos/saveddata/fit.py:439-444 jamChance; per-jammer strength = scanXStrengthBonus for
  the target's sensor type, pyfa eos/effects.py:30992). Absent target sensor strength → null
  with a reason, never a fabricated number.
* **Adjusted target**: our painters enlarge the target's signature and our webs slow it, using
  the SAME stacking-penalised postPercent maths a fitted/projected modifier uses
  (graph._calculate). Applied DPS is measured against that adjusted profile; the raw-profile
  totals are kept as ``total_applied_dps_unassisted`` so the UI can show both.

Regressions locked in (bugs found by WS-6, verified against live data here)
---------------------------------------------------------------------------
* (f) Target Painter now appears in the ewar section — the old readout keyed painters on group
  209 (a scan-strength attr id), but the real Target Painter group is 379, so the branch was
  dead code and no painter ever showed.
* (g) Sensor dampener scan-res readout reads scanResolutionBonus (566); the old constant 565
  (scanResolutionMultiplier) is absent on player dampeners and read a silent zero.
* (h) Local hull active tank reads structureDamageAmount (83); the old constant 87
  (shieldTransferRange) is absent on hull repairers and read a silent zero.

Fixture: tests/fixtures/fitting/ewar_local.json.
"""
from __future__ import annotations

import math

import pytest

from apps.fitting.engine.types import (
    FitInput,
    ModuleInput,
    ModuleState,
    OperatingProfile,
    SkillProfile,
    SlotKind,
    TargetProfile,
)

from ._fitting_graph_utils import load_graph_fixture

pytestmark = pytest.mark.django_db

# The engine's stacking-penalty factor, reproduced so the two-painter expectation runs the
# same maths as graph._calculate / evaluator._stacked_postpercent (exp(-(1/2.67)^2); the
# i-th element of a penalised chain is multiplied by _PENALTY**(i*i)).
_PENALTY = math.exp(-((1.0 / 2.67) ** 2))


@pytest.fixture()
def ids():
    return load_graph_fixture("ewar_local")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    row = SdeTypeAttribute.objects.filter(type_id=type_id, attribute_id=attr_id).first()
    return row.value if row else None


def _mod(tid, slot=SlotKind.MED, state=ModuleState.ACTIVE, charge=None):
    return ModuleInput(type_id=tid, slot=slot, state=state, charge_type_id=charge)


def _telemetry(ship, modules=(), op=None):
    """Full pass-4 telemetry through the production adapter path (empty skills, so only the
    fitted modules' base attributes move a number)."""
    from apps.fitting.engine.adapter import FittingEngine

    engine = FittingEngine()
    fit = FitInput(ship_type_id=ship, modules=tuple(modules))
    return engine.evaluate(fit, SkillProfile.from_dict({}),
                           op or OperatingProfile(propulsion_active=False))


def _ewar_entry(res, kind):
    for e in res.telemetry["ewar"]["modules"]:
        if e["kind"] == kind:
            return e
    return None


def _missile_application(sig, vel, er, ev_, drf):
    """Reproduces evaluator._missile_application (post-2015 missile formula)."""
    if er <= 0:
        return 1.0
    size_term = sig / er
    if vel <= 0 or ev_ <= 0 or drf <= 0:
        return min(1.0, size_term)
    return min(1.0, size_term, (size_term * (ev_ / vel)) ** drf)


def _stacked(base, bonuses):
    """Reproduces evaluator._stacked_postpercent."""
    val = base
    for i, b in enumerate(sorted(bonuses, key=abs, reverse=True)):
        val *= 1.0 + (b / 100.0) * (_PENALTY ** (i * i))
    return val


# --------------------------------------------------------------------------- #
# (a) ECM jam chance: 2.6 / tgt_ss per jammer, combined 1 − (1 − p)² for two
# --------------------------------------------------------------------------- #
def test_ecm_jam_chance_and_combined(ids):
    rifter, ecm = ids["Rifter"], ids["Multispectral ECM II"]
    assert _attr(ecm, 241) == 2.6                      # scanRadarStrengthBonus (241)
    tgt_ss = 20.0
    op = OperatingProfile(propulsion_active=False, target=TargetProfile(
        signature_radius=100.0, target_sensor_strength=tgt_ss, target_sensor_type="radar"))
    res = _telemetry(rifter, [_mod(ecm), _mod(ecm)], op=op)

    p = min(1.0, 2.6 / tgt_ss)                          # 0.13 per jammer
    for e in res.telemetry["ewar"]["modules"]:
        assert e["kind"] == "ecm"
        assert e["jam_sensor"] == "radar"
        assert e["jam_chance"] == round(p, 4)
    jam = res.telemetry["ewar"]["jam"]
    assert jam["jammer_count"] == 2
    assert jam["target_sensor_strength"] == tgt_ss
    combined = 1.0 - (1.0 - p) ** 2                     # independent rolls
    assert jam["combined_chance"] == round(combined, 4)
    assert jam["reason"] is None


# --------------------------------------------------------------------------- #
# (b) per-type chances when no sensor type is named
# --------------------------------------------------------------------------- #
def test_ecm_per_type_chances_without_sensor_type(ids):
    rifter, ecm = ids["Rifter"], ids["Multispectral ECM II"]
    tgt_ss = 26.0
    op = OperatingProfile(propulsion_active=False, target=TargetProfile(
        signature_radius=100.0, target_sensor_strength=tgt_ss))   # no sensor type
    res = _telemetry(rifter, [_mod(ecm)], op=op)

    e = _ewar_entry(res, "ecm")
    expected = round(min(1.0, 2.6 / tgt_ss), 4)        # 0.1
    assert e["jam_chances"] == {k: expected for k in
                                ("radar", "ladar", "magnetometric", "gravimetric")}
    assert "jam_chance" not in e
    jam = res.telemetry["ewar"]["jam"]
    assert jam["combined_chance"] is None
    assert jam["reason"] == "no_target_sensor_type"


# --------------------------------------------------------------------------- #
# (c) jam chance is null (with a reason) when target sensor strength is unknown
# --------------------------------------------------------------------------- #
def test_ecm_jam_null_without_sensor_strength(ids):
    rifter, ecm = ids["Rifter"], ids["Multispectral ECM II"]
    op = OperatingProfile(propulsion_active=False,
                          target=TargetProfile(signature_radius=100.0))   # no sensor strength
    res = _telemetry(rifter, [_mod(ecm)], op=op)

    e = _ewar_entry(res, "ecm")
    assert e["jam_chance"] is None
    assert e["jam_reason"] == "target_sensor_strength_unknown"
    jam = res.telemetry["ewar"]["jam"]
    assert jam["combined_chance"] is None
    assert jam["reason"] == "target_sensor_strength_unknown"
    # Strengths are still reported honestly (they don't depend on the target).
    assert e["strengths"]["radar"] == 2.6


# --------------------------------------------------------------------------- #
# (d) target painter: adjusted sig = sig × 1.30 (one), stacking-penalised (two)
# --------------------------------------------------------------------------- #
def test_painter_adjusts_target_signature(ids):
    rifter, painter = ids["Rifter"], ids["Target Painter II"]
    assert _attr(painter, 554) == 30.0                 # signatureRadiusBonus (554)
    base_sig = 120.0
    op = OperatingProfile(propulsion_active=False,
                          target=TargetProfile(signature_radius=base_sig, velocity=0.0))

    one = _telemetry(rifter, [_mod(painter)], op=op)
    ot = one.telemetry["ewar"]["ewar_on_target"]
    assert ot["adjusted"]["signature"] == round(base_sig * 1.30, 1)      # 156.0
    assert ot["base"]["signature"] == round(base_sig, 1)

    two = _telemetry(rifter, [_mod(painter), _mod(painter)], op=op)
    ot2 = two.telemetry["ewar"]["ewar_on_target"]
    expected = base_sig * (1.0 + 0.30) * (1.0 + 0.30 * _PENALTY)         # second penalised
    assert ot2["adjusted"]["signature"] == round(expected, 1)
    # Strictly larger than one painter, strictly smaller than a naive un-penalised double.
    naive = base_sig * (1.30 ** 2)
    assert round(base_sig * 1.30, 1) < ot2["adjusted"]["signature"] < round(naive, 1)


# --------------------------------------------------------------------------- #
# (e) web-adjusted velocity feeds applied DPS — assert BOTH totals
# --------------------------------------------------------------------------- #
def test_web_adjusted_velocity_feeds_applied_dps(ids):
    caracal, web, launcher, missile = (ids["Caracal"], ids["Stasis Webifier II"],
                                       ids["Light Missile Launcher II"],
                                       ids["Scourge Light Missile"])
    assert _attr(web, 20) == -60.0                     # speedFactor (20)
    kin = _attr(missile, 117)                          # kineticDamage
    rof_s = _attr(launcher, 51) / 1000.0
    er, ev_, drf = _attr(missile, 654), _attr(missile, 653), _attr(missile, 1353)
    dps = kin / rof_s

    base_sig, base_vel = 40.0, 400.0
    op = OperatingProfile(propulsion_active=False, target=TargetProfile(
        signature_radius=base_sig, velocity=base_vel))
    res = _telemetry(caracal,
                     [_mod(launcher, slot=SlotKind.HIGH, charge=missile), _mod(web)], op=op)

    adj_vel = base_vel * (1.0 + (-60.0) / 100.0)        # 160.0 (single web, no penalty)
    ot = res.telemetry["ewar"]["ewar_on_target"]
    assert ot["adjusted"]["velocity"] == round(adj_vel, 1)
    assert ot["web_velocity_pct"] == -60.0

    factor_adj = _missile_application(base_sig, adj_vel, er, ev_, drf)
    factor_raw = _missile_application(base_sig, base_vel, er, ev_, drf)
    off = res.telemetry["offence"]
    assert off["total_applied_dps"] == round(dps * factor_adj, 1)
    assert off["total_applied_dps_unassisted"] == round(dps * factor_raw, 1)
    # The web strictly helped: assisted application beats unassisted.
    assert off["total_applied_dps"] > off["total_applied_dps_unassisted"]


# --------------------------------------------------------------------------- #
# (f) REGRESSION: a target painter appears in the ewar section (group 379, not 209)
# --------------------------------------------------------------------------- #
def test_regression_painter_in_ewar_section(ids):
    rifter, painter = ids["Rifter"], ids["Target Painter II"]
    res = _telemetry(rifter, [_mod(painter)])
    e = _ewar_entry(res, "target_painter")
    assert e is not None                               # was dead code under the group-209 bug
    assert e["type_id"] == painter
    assert e["strengths"]["signature_bonus"] == 30.0
    assert e["optimal_m"] == round(_attr(painter, 54), 0)


# --------------------------------------------------------------------------- #
# (g) REGRESSION: sensor dampener scan-res readout uses scanResolutionBonus (566)
# --------------------------------------------------------------------------- #
def test_regression_damp_uses_566(ids):
    rifter, damp = ids["Rifter"], ids["Remote Sensor Dampener II"]
    assert _attr(damp, 566) == -15.3                   # scanResolutionBonus (566)
    assert _attr(damp, 565) is None                    # scanResolutionMultiplier absent (the bug)
    res = _telemetry(rifter, [_mod(damp)])
    e = _ewar_entry(res, "sensor_dampener")
    assert e["strengths"]["scan_res_bonus"] == -15.3   # NOT a silent zero
    assert e["strengths"]["lock_range_bonus"] == -15.3


# --------------------------------------------------------------------------- #
# (h) REGRESSION: hull active tank = structureDamageAmount(83) / cycle (not old-87 zero)
# --------------------------------------------------------------------------- #
def test_regression_hull_repairer_active_tank(ids):
    rifter, rep = ids["Rifter"], ids["Small Hull Repairer II"]
    amount = _attr(rep, 83)                             # structureDamageAmount
    assert amount == 30.0
    assert _attr(rep, 87) is None                       # shieldTransferRange absent (the bug)
    cycle_s = _attr(rep, 73) / 1000.0
    res = _telemetry(rifter, [_mod(rep, slot=SlotKind.LOW)])
    hull_hps = res.telemetry["defence"]["active_tank"]["hull_hps"]
    assert hull_hps == round(amount / cycle_s, 1)       # 30 / 24 = 1.2
    assert hull_hps != 0.0                              # the old 87 behaviour is gone


# --------------------------------------------------------------------------- #
# Tracking / guidance disruptor strength readouts (old group 213 → 291 dead-code fix)
# --------------------------------------------------------------------------- #
def test_tracking_disruptor_strengths(ids):
    rifter, td = ids["Rifter"], ids["Tracking Disruptor II"]
    res = _telemetry(rifter, [_mod(td)])
    e = _ewar_entry(res, "tracking_disruptor")
    assert e is not None
    assert e["strengths"] == {"tracking_bonus": _attr(td, 767),
                              "optimal_bonus": _attr(td, 351),
                              "falloff_bonus": _attr(td, 349)}
    assert e["strengths"]["tracking_bonus"] == -17.19


def test_guidance_disruptor_strengths(ids):
    rifter, gd = ids["Rifter"], ids["Guidance Disruptor II"]
    res = _telemetry(rifter, [_mod(gd)])
    e = _ewar_entry(res, "guidance_disruptor")
    assert e is not None
    assert e["strengths"]["missile_velocity_bonus"] == _attr(gd, 547) == -9.0
    assert e["strengths"]["explosion_radius_bonus"] == _attr(gd, 848) == 12.0


# --------------------------------------------------------------------------- #
# Burst jammer: AoE (no optimal/falloff), carries a burst radius, jams by strength
# --------------------------------------------------------------------------- #
def test_burst_jammer_readout(ids):
    rifter, burst = ids["Rifter"], ids["Burst Jammer I"]
    op = OperatingProfile(propulsion_active=False, target=TargetProfile(
        signature_radius=100.0, target_sensor_strength=20.0, target_sensor_type="ladar"))
    res = _telemetry(rifter, [_mod(burst)], op=op)
    e = _ewar_entry(res, "ecm_burst")
    assert e is not None
    assert e["strengths"]["ladar"] == 6.0              # scanLadarStrengthBonus (239)
    assert e["optimal_m"] is None and e["falloff_m"] is None
    assert e["burst_range_m"] == round(_attr(burst, 142), 0)   # ecmBurstRange 10000
    assert e["jam_chance"] == round(min(1.0, 6.0 / 20.0), 4)   # 0.3


# --------------------------------------------------------------------------- #
# Sensor dampener adjusted-target deltas (stacking-penalised, reported not applied)
# --------------------------------------------------------------------------- #
def test_damp_on_target_deltas(ids):
    rifter, damp = ids["Rifter"], ids["Remote Sensor Dampener II"]
    b = _attr(damp, 309)                               # -15.3 lock-range + scan-res bonus
    op = OperatingProfile(propulsion_active=False,
                          target=TargetProfile(signature_radius=100.0, velocity=0.0))
    res = _telemetry(rifter, [_mod(damp), _mod(damp)], op=op)
    dd = res.telemetry["ewar"]["ewar_on_target"]["damp"]
    expected_pct = (_stacked(1.0, [b, b]) - 1.0) * 100.0
    assert dd["lock_range_pct"] == round(expected_pct, 1)
    assert dd["scan_res_pct"] == round(expected_pct, 1)
    # A single web/painter is absent here, so sig/velocity are unchanged.
    assert res.telemetry["ewar"]["ewar_on_target"]["adjusted"]["signature"] == 100.0
