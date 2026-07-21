"""Golden fits: WS-6 projected effects (engine v2, real SDE slices).

Every expectation is hand-derived from the fixture's own base attributes and the effect's
actual ``SdeModifier`` rows / documented per-cycle attributes, using CCP's operator
semantics — never read back from the engine. Attribute ids and base values are asserted
from the slice before they drive an expectation, so a fixture drift fails loudly.

Mechanics proven here
---------------------
* **Web / painter / dampener** default effects (remoteWebifierFalloff 6426 /
  remoteTargetPaintFalloff 6425 / remoteSensorDampFalloff 6422) ship EMPTY modifierInfo in
  CCP's SDE; the importer synthesises the documented ``targetID`` postPercent modifiers
  (import_ship_bonuses._CLIENT_INTERNAL_EFFECTS, mirroring pyfa's boostItemAttr handlers).
  The graph's projected pass (graph._collect_projected) applies them onto the hull with the
  same stacking machinery a fitted module uses — so two webs are stacking-penalised.
* **Resistance**: the incoming value is scaled by the hull's evaluated resistance attribute
  for the family (web 2115 / painter 2114 / damp 2112 / energy-warfare 2045), default 1.0.
  A fitted capacitor battery lowers energyWarfareResistance (2045) via effect 6487, cutting
  incoming neut pressure.
* **Neut / nos** carry no dogma modifier — the evaluator reads energyNeutralizerAmount (97)
  / powerTransferAmount (90) per cycle straight off the module and adds it to the capacitor
  drain (NOS treated as pure drain in v1).
* **Remote reps** add incoming HP/s per layer (shieldBonus 68 / armorDamageAmount 84 /
  structureDamageAmount 83 per cycle), reported separately from our own active tank.
* **Warp scrambler** has a real modifier graph (5934): its targetID modAdd writes
  warpScrambleStatus (104) on the hull.
* **Range/falloff** are ignored — v1 applies projected effects at full strength (at optimal).
  The attacker is evaluated at BASE attributes (unbonused). Both are documented in the matrix.

Fixture: tests/fixtures/fitting/projected_ewar.json (the projected modules + Rifter/Caracal
victim hulls).
"""
from __future__ import annotations

import math

import pytest

from apps.fitting.engine.types import (
    FitInput,
    ModuleInput,
    ModuleState,
    OperatingProfile,
    ProjectedInput,
    SkillProfile,
    SlotKind,
    TargetProfile,
)

from ._fitting_graph_utils import load_graph_fixture

pytestmark = pytest.mark.django_db

# The engine's stacking-penalty factor, reproduced so the two-web expectation runs the same
# maths as graph._calculate (exp(-(1/2.67)^2); the second element of a penalised chain is
# multiplied by this once).
_PENALTY = math.exp(-((1.0 / 2.67) ** 2))


@pytest.fixture()
def ids():
    return load_graph_fixture("projected_ewar")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _attr_default(attr_id):
    from apps.sde.models import SdeDogmaAttribute
    return SdeDogmaAttribute.objects.get(attribute_id=attr_id).default_value


def _proj(tid, state=ModuleState.ACTIVE, qty=1):
    return ProjectedInput(type_id=tid, state=state, quantity=qty)


def _mod(tid, slot, state=ModuleState.ACTIVE):
    return ModuleInput(type_id=tid, slot=slot, state=state)


def _graph(ship, modules=(), projected=(), skills=None):
    """Passes 1-3 only, for asserting raw evaluated hull attributes (via ``ship_value``).
    Empty skills so nothing but the fitted modules + projected sources move an attribute;
    the full skill catalogue is still materialised at level 0."""
    from apps.fitting.engine.adapter import ORMDataProvider
    from apps.fitting.engine.graph import evaluate_attributes

    prov = ORMDataProvider()
    fit = FitInput(ship_type_id=ship, modules=tuple(modules), projected=tuple(projected))
    return evaluate_attributes(fit, skills or SkillProfile.from_dict({}), prov,
                               skill_ids=prov.trained_skill_ids())


def _telemetry(ship, modules=(), projected=(), skills=None, op=None):
    """Full pass-4 telemetry through the production adapter path."""
    from apps.fitting.engine.adapter import FittingEngine

    engine = FittingEngine()
    fit = FitInput(ship_type_id=ship, modules=tuple(modules), projected=tuple(projected))
    return engine.evaluate(fit, skills or SkillProfile.from_dict({}),
                           op or OperatingProfile(propulsion_active=False))


def _codes(res):
    return {d.code for d in res.diagnostics}


def _lock_time_s(scan_res, target_sig):
    """Reproduces evaluator._lock_time_s (CCP/EVE-Uni targeting formula)."""
    return min(40000.0 / scan_res / (math.asinh(target_sig) ** 2), 30 * 60.0)


def _cap_runtime(capacity, tau, net_drain):
    """Reproduces evaluator._cap_runtime (1 s-step integration of the recharge ODE)."""
    c = capacity
    for t in range(0, 7200):
        x = c / capacity
        c += (10.0 * capacity / tau) * (math.sqrt(x) - x) - net_drain
        if c <= 0:
            return float(t + 1)
    return None


# --------------------------------------------------------------------------- #
# (a) one web: max velocity × (1 - 0.60)
# --------------------------------------------------------------------------- #
def test_one_web_velocity(ids):
    rifter, web = ids["Rifter"], ids["Stasis Webifier II"]
    base_v = _attr(rifter, 37)
    speed_factor = _attr(web, 20)
    assert speed_factor == -60.0                      # speedFactor (20)
    assert _attr_default(2115) == 1.0                 # stasisWebifierResistance default → no-op

    ev = _graph(rifter, projected=[_proj(web)])
    expected = base_v * (1.0 + speed_factor / 100.0)  # 365 × 0.4 = 146.0
    assert ev.ship_value(37) == pytest.approx(expected)

    tel = _telemetry(rifter, projected=[_proj(web)])
    assert tel.telemetry["mobility"]["max_velocity"] == round(expected, 1)
    # The projected module is listed in the telemetry with its effect summary.
    proj = tel.telemetry["projected"]
    assert proj["count"] == 1
    assert proj["modules"][0]["type_id"] == web and proj["modules"][0]["quantity"] == 1
    assert "mode_invalid_for_ship" not in _codes(tel)


# --------------------------------------------------------------------------- #
# (b) two webs: second is stacking-penalised
# --------------------------------------------------------------------------- #
def test_two_webs_stacking_penalty(ids):
    rifter, web = ids["Rifter"], ids["Stasis Webifier II"]
    base_v = _attr(rifter, 37)
    v = _attr(web, 20) / 100.0                         # -0.6
    # Penalised chain (both equal magnitude): first at full, second × _PENALTY.
    expected = base_v * (1.0 + v) * (1.0 + v * _PENALTY)

    ev = _graph(rifter, projected=[_proj(web), _proj(web)])
    assert ev.ship_value(37) == pytest.approx(expected)
    # Strictly slower than one web, strictly faster than a naive (unpenalised) double.
    one = base_v * (1.0 + v)
    naive = base_v * (1.0 + v) * (1.0 + v)
    assert naive < expected < one


# --------------------------------------------------------------------------- #
# (c) target painter: signature radius × 1.30
# --------------------------------------------------------------------------- #
def test_painter_signature(ids):
    rifter, painter = ids["Rifter"], ids["Target Painter II"]
    base_sig = _attr(rifter, 552)
    sig_bonus = _attr(painter, 554)
    assert sig_bonus == 30.0                           # signatureRadiusBonus (554)
    assert _attr_default(2114) == 1.0

    ev = _graph(rifter, projected=[_proj(painter)])
    expected = base_sig * (1.0 + sig_bonus / 100.0)    # 35 × 1.30 = 45.5
    assert ev.ship_value(552) == pytest.approx(expected)

    tel = _telemetry(rifter, projected=[_proj(painter)])
    assert tel.telemetry["mobility"]["signature_radius"] == round(expected, 1)


# --------------------------------------------------------------------------- #
# (d) sensor dampener: lock range + scan resolution reduced; lock time rises
# --------------------------------------------------------------------------- #
def test_dampener_range_scanres_and_lock_time(ids):
    rifter, damp = ids["Rifter"], ids["Remote Sensor Dampener II"]
    base_range, base_scan = _attr(rifter, 76), _attr(rifter, 564)
    range_bonus, scan_bonus = _attr(damp, 309), _attr(damp, 566)
    assert (range_bonus, scan_bonus) == (-15.3, -15.3)
    assert _attr_default(2112) == 1.0

    ev = _graph(rifter, projected=[_proj(damp)])
    exp_range = base_range * (1.0 + range_bonus / 100.0)   # 22500 × 0.847
    exp_scan = base_scan * (1.0 + scan_bonus / 100.0)      # 660 × 0.847
    assert ev.ship_value(76) == pytest.approx(exp_range)
    assert ev.ship_value(564) == pytest.approx(exp_scan)

    # Lock time uses OUR (dampened) scan resolution against a target's signature. A damp
    # drops scan res, so lock time rises exactly in proportion (660 / 559.02).
    target_sig = 125.0
    op = OperatingProfile(propulsion_active=False,
                          target=TargetProfile(signature_radius=target_sig))
    bare = _telemetry(rifter, op=op)
    damped = _telemetry(rifter, projected=[_proj(damp)], op=op)
    assert bare.telemetry["targeting"]["lock_time_s"] == round(
        _lock_time_s(base_scan, target_sig), 2)
    assert damped.telemetry["targeting"]["lock_time_s"] == round(
        _lock_time_s(exp_scan, target_sig), 2)
    assert damped.telemetry["targeting"]["lock_time_s"] > \
        bare.telemetry["targeting"]["lock_time_s"]
    assert damped.telemetry["targeting"]["max_target_range"] == round(exp_range, 0)


# --------------------------------------------------------------------------- #
# (e) energy neutralizer vs capacitor stability
# --------------------------------------------------------------------------- #
def test_neut_capacitor_stability(ids):
    rifter, neut = ids["Rifter"], ids["Medium Energy Neutralizer II"]
    amount, cycle = _attr(neut, 97), _attr(neut, 73)
    assert (amount, cycle) == (180.0, 12000.0)         # 180 GJ / 12 s = 15 GJ/s
    gj_s = amount / (cycle / 1000.0)

    cap, tau = _attr(rifter, 482), _attr(rifter, 55) / 1000.0   # 250 GJ, 125 s
    peak = 2.5 * cap / tau                              # 5.0 GJ/s

    bare = _telemetry(rifter).telemetry["capacitor"]
    assert bare["incoming_pressure"] == 0.0
    assert bare["stable"] is True and bare["stable_pct"] == 100.0

    hit = _telemetry(rifter, projected=[_proj(neut)]).telemetry["capacitor"]
    assert hit["incoming_pressure"] == round(gj_s, 2)   # 15.0, resistance 1.0
    # Incoming drain 15 GJ/s exceeds peak recharge 5 GJ/s → not stable; finite runtime.
    assert gj_s > peak
    assert hit["stable"] is False and hit["stable_pct"] is None
    assert hit["runtime_s"] == _cap_runtime(cap, tau, gj_s)


# --------------------------------------------------------------------------- #
# (f) neut + capacitor battery: pressure cut by evaluated energyWarfareResistance
# --------------------------------------------------------------------------- #
def test_neut_with_cap_battery_resistance(ids):
    rifter, neut, batt = (ids["Rifter"], ids["Medium Energy Neutralizer II"],
                          ids["Large Cap Battery II"])
    ew_bonus = _attr(batt, 2267)
    assert ew_bonus == -25.0                            # energyWarfareResistanceBonus (2267)
    gj_s = _attr(neut, 97) / (_attr(neut, 73) / 1000.0)   # 15 GJ/s raw

    # Effect 6487 (online, shipID postPercent 2045←2267) lowers our energyWarfareResistance.
    ev = _graph(rifter, modules=[_mod(batt, SlotKind.MED)])
    exp_resist = 1.0 * (1.0 + ew_bonus / 100.0)         # 0.75
    assert ev.ship_value(2045) == pytest.approx(exp_resist)

    hit = _telemetry(rifter, modules=[_mod(batt, SlotKind.MED)],
                     projected=[_proj(neut)]).telemetry["capacitor"]
    assert hit["incoming_pressure"] == round(gj_s * exp_resist, 2)   # 15 × 0.75 = 11.25


# --------------------------------------------------------------------------- #
# (g) remote shield booster projected onto us: incoming rep HP/s
# --------------------------------------------------------------------------- #
def test_remote_shield_booster_incoming_rep(ids):
    caracal, rsb = ids["Caracal"], ids["Large Remote Shield Booster II"]
    boost, cycle = _attr(rsb, 68), _attr(rsb, 73)
    assert (boost, cycle) == (680.0, 8000.0)           # shieldBonus / duration
    assert _attr_default(2116) == 1.0                  # remoteRepairImpedance default → no-op

    tel = _telemetry(caracal, projected=[_proj(rsb)])
    incoming = tel.telemetry["defence"]["incoming_rep"]
    assert incoming["shield_hps"] == round(boost / (cycle / 1000.0), 1)   # 85.0
    assert incoming["armor_hps"] == 0.0 and incoming["hull_hps"] == 0.0
    # Reported SEPARATELY from our own active tank (which is zero here — no local booster).
    assert tel.telemetry["defence"]["active_tank"]["shield_hps"] == 0.0


# --------------------------------------------------------------------------- #
# (h) warp scrambler: warpScrambleStatus goes positive via its real graph
# --------------------------------------------------------------------------- #
def test_warp_scrambler_status(ids):
    rifter, scram = ids["Rifter"], ids["Warp Scrambler II"]
    strength = _attr(scram, 105)
    assert strength == 2.0                              # warpScrambleStrength (105)
    assert _attr_default(104) == 0.0                    # warpScrambleStatus starts at 0

    ev = _graph(rifter, projected=[_proj(scram)])
    # Effect 5934 modAdd: warpScrambleStatus (104) += warpScrambleStrength (105).
    assert ev.ship_value(104) == pytest.approx(strength)   # 0 + 2 = 2 (positive)
    assert ev.ship_value(104) > 0


# --------------------------------------------------------------------------- #
# (i) quantity=2 web == two independent single webs (identical numbers)
# --------------------------------------------------------------------------- #
def test_web_quantity_equals_two_singles(ids):
    rifter, web = ids["Rifter"], ids["Stasis Webifier II"]
    qty = _graph(rifter, projected=[_proj(web, qty=2)]).ship_value(37)
    two = _graph(rifter, projected=[_proj(web), _proj(web)]).ship_value(37)
    assert qty == pytest.approx(two)
    # And the telemetry collapses the pair back to one row with quantity 2.
    tel = _telemetry(rifter, projected=[_proj(web, qty=2)])
    assert tel.telemetry["projected"]["modules"][0]["quantity"] == 2


# --------------------------------------------------------------------------- #
# (j) a projected module with no target effect → projected_module_inert (advisory)
# --------------------------------------------------------------------------- #
def test_projected_inert_module(ids):
    rifter, gyro = ids["Rifter"], ids["Gyrostabilizer II"]

    # Omniscient skills so the hull's own skill requirements are met — isolating the
    # projected-inert warning from an unrelated missing-skills status.
    allv = SkillProfile.omniscient()
    tel = _telemetry(rifter, projected=[_proj(gyro)], skills=allv)
    d = next(x for x in tel.diagnostics if x.code == "projected_module_inert")
    assert d.severity.value == "warning"
    assert d.params["type_id"] == gyro
    # An inert projected module is advisory, never structural — the fit is not IMPOSSIBLE.
    assert tel.status.value in ("valid", "warnings")
    # And it changes nothing on the hull: identical velocity to the same fit without it.
    bare = _telemetry(rifter, skills=allv)
    assert tel.telemetry["mobility"]["max_velocity"] == \
        bare.telemetry["mobility"]["max_velocity"]


# --------------------------------------------------------------------------- #
# (k) projected entries are excluded from EFT export / price / stock / doctrine
# --------------------------------------------------------------------------- #
def test_projected_excluded_from_export_and_overlays(ids):
    from apps.fitting import services

    rifter, web = ids["Rifter"], ids["Stasis Webifier II"]
    items = [
        {"type_id": web, "slot": "projected", "state": "active",
         "charge_type_id": None, "quantity": 1},
    ]
    # Persistence path: a slot="projected" entry becomes FitInput.projected, not a module.
    loaded = services.fit_input_from_items(rifter, items)
    assert [p.type_id for p in loaded.projected] == [web]
    assert loaded.modules == ()

    # EFT export omits the projected marker entirely (EFT has no such concept).
    eft = services.export_eft(rifter, items, "Victim")
    assert "Stasis Webifier" not in eft
    # Pricing / stock never count a projected module (it is not part of the fit's cost).
    assert web not in {li["type_id"] for li in services.price_fit(rifter, items)["lines"]}
    assert web not in {r["type_id"] for r in services.stock_coverage(rifter, items)["rows"]}
