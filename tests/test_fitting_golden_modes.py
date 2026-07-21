"""Golden fits: WS-5 tactical modes + siege-class verification (engine v2, real SDE slices).

Every expectation is hand-derived from the fixture's own base attributes and the effect's
actual ``SdeModifier`` rows using CCP's documented operator semantics — never read back
from the engine. Each attribute id / modifier value is asserted from the slice before it
is used, so a fixture drift fails loudly.

Mechanics proven here
---------------------
* **T3D tactical modes** are materialised as an always-on entity whose ``ItemModifier``
  (domain ``shipID``) modifiers write onto the hull. All CCP mode modifiers use operator 5
  (``postDiv``): ``attr := attr / modeAttr``.  Confessor **Defense Mode** (34319) divides
  ``signatureRadius`` (552) and the four armour resonances (267-270) by 1.5; **Sharpshooter
  Mode** (34321) divides ``maxTargetRange`` (76) and the scan strengths (208-211) by 0.5
  (i.e. doubles them).
* **Stacking**: a mode type is category 7 (Module), so it is NOT in the engine's
  stacking-exempt set {6,8,16,20,32} — its multiplicative modifiers are penalisable exactly
  like a module's (matching pyfa, which applies a mode with a plain ``("module",)`` context
  and no exemption — eos/saveddata/mode.py:55). The engine buckets the stacking penalty per
  operator; every mode modifier is ``postDiv`` while resist/utility modules use
  ``preMul``/``postPercent``, and no fittable module writes the mode-affected non-stackable
  attributes via ``postDiv`` (verified: zero published category-7 postDiv carriers on 552 /
  267). So in live data a mode's bonus and a module's bonus land in different operator
  buckets and compound at full strength (each alone in its chain → penalty factor 1.0). Case
  ``defense_mode_with_damage_control`` hand-derives that compounding.
* **Mode ↔ hull link**: CCP ships no data link (the mode carries no
  fitsToShipType / canFitShipType* / canFitShipGroup* / requiredSkill, and the hull
  enumerates no mode ids). The only tie — pyfa's (eos/saveddata/ship.py:123-139, "race is
  not reliable") — is group "Ship Modifiers" + the mode name beginning with the hull name.
* **Siege / Bastion** are ordinary active modules whose single default effect carries a
  large real modifier set (no engine change): Siege Module II (4292) effect 6582 = 35
  modifiers, Bastion Module I (33400) effect 6658 = 49. Verified by hand-deriving key
  outcomes from those rows.

Fixtures: tests/fixtures/fitting/modes_tactical.json (Confessor + its 3 modes + Damage
Control II + Rifter + Svipul + a Svipul mode), tests/fixtures/fitting/bastion_golem.json
(Golem + Bastion Module I + Large Shield Booster II + Cruise Missile Launcher II), and the
existing fitval_restrictions.json (Revelation + Siege Module II).
"""
from __future__ import annotations

import pytest

from apps.fitting.engine.types import (
    FitInput,
    ModuleInput,
    ModuleState,
    OperatingProfile,
    SkillProfile,
    SlotKind,
)

from ._fitting_graph_utils import load_graph_fixture

pytestmark = pytest.mark.django_db

# T3D mode modifier operator is CCP op 5 (postDiv): a lone postDiv application (the mode is
# alone in its operator bucket, penalty factor 1.0 for the first element) evaluates to
# ``base / divisor``. postPercent (op 6) is ``base * (1 + pct/100)``; preMul (op 0) is
# ``base * factor``. These reproduce apps/fitting/engine/graph.py:_calculate for a single
# penalisable application, so the expectations follow the same maths the engine runs.
def _post_div(base: float, divisor: float) -> float:
    return base / divisor


def _post_percent(base: float, pct: float) -> float:
    return base * (1.0 + pct / 100.0)


def _pre_mul(base: float, factor: float) -> float:
    return base * factor


def _post_mul(base: float, factor: float) -> float:
    # postMul (op 4): a lone penalisable application → base * factor (mass 1.29e9 * 10).
    return base * factor


@pytest.fixture()
def mode_ids():
    return load_graph_fixture("modes_tactical")


@pytest.fixture()
def bastion_ids():
    return load_graph_fixture("bastion_golem")


@pytest.fixture()
def siege_ids():
    return load_graph_fixture("fitval_restrictions")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _stackable(attr_id):
    from apps.sde.models import SdeDogmaAttribute
    return SdeDogmaAttribute.objects.get(attribute_id=attr_id).stackable


def _mod(tid, slot, state=ModuleState.ACTIVE, charge=None, qty=1):
    return ModuleInput(type_id=tid, slot=slot, state=state, charge_type_id=charge, quantity=qty)


def _graph(ship_type_id, modules=(), mode_type_id=None, skills=None):
    """Passes 1-3 only, for asserting raw evaluated entity attributes (via ``ship_value`` /
    ``value``). Empty skills by default so nothing but the fitted modules + mode moves an
    attribute; the full skill catalogue is still materialised (at level 0) so hull traits
    correctly zero out."""
    from apps.fitting.engine.adapter import ORMDataProvider
    from apps.fitting.engine.graph import evaluate_attributes

    prov = ORMDataProvider()
    fit = FitInput(ship_type_id=ship_type_id, modules=tuple(modules), mode_type_id=mode_type_id)
    return evaluate_attributes(fit, skills or SkillProfile.from_dict({}), prov,
                               skill_ids=prov.trained_skill_ids())


def _telemetry(ship_type_id, modules=(), mode_type_id=None, skills=None, op=None):
    """Full pass-4 telemetry through the production adapter path."""
    from apps.fitting.engine.adapter import FittingEngine

    engine = FittingEngine()
    fit = FitInput(ship_type_id=ship_type_id, modules=tuple(modules), mode_type_id=mode_type_id)
    return engine.evaluate(fit, skills or SkillProfile.from_dict({}),
                           op or OperatingProfile(propulsion_active=False))


def _codes(res):
    return {d.code for d in res.diagnostics}


def _module(ev, type_id):
    return next(m for m in ev.modules if m.type_id == type_id)


# --------------------------------------------------------------------------- #
# (a) Defense Mode: signature radius + armour resonances divided by 1.5
# --------------------------------------------------------------------------- #
def test_defense_mode_sig_and_armor_resonance(mode_ids):
    conf, mode = mode_ids["Confessor"], mode_ids["Confessor Defense Mode"]
    # Data: the mode divides sig (552) by modeSignatureRadiusPostDiv (2001) and each armour
    # resonance (267-270) by its modeResistancePostDiv attr — all 1.5, all postDiv.
    base_sig = _attr(conf, 552)
    base_em_res = _attr(conf, 267)
    assert _attr(mode, 2001) == 1.5 and _attr(mode, 1997) == 1.5
    assert _stackable(552) is False and _stackable(267) is False   # penalisable, but lone

    ev = _graph(conf, mode_type_id=mode)
    assert ev.ship_value(552) == pytest.approx(_post_div(base_sig, 1.5))          # 65/1.5
    assert ev.ship_value(267) == pytest.approx(_post_div(base_em_res, 1.5))       # 0.5/1.5

    # End-to-end telemetry: smaller sig, better armour EM resist.
    tel = _telemetry(conf, mode_type_id=mode)
    assert tel.telemetry["mobility"]["signature_radius"] == round(_post_div(base_sig, 1.5), 1)
    exp_resist = round((1.0 - _post_div(base_em_res, 1.5)) * 100, 1)              # 66.7 %
    assert tel.telemetry["defence"]["layers"]["armor"]["resists"]["em"] == exp_resist
    # The mode is echoed for the UI and the fit is not flagged mode-invalid.
    assert tel.telemetry["ship"]["mode"] == {"type_id": mode, "name": "Confessor Defense Mode"}
    assert "mode_invalid_for_ship" not in _codes(tel)


# --------------------------------------------------------------------------- #
# (b) Defense Mode + Damage Control II: stacking interaction on armour resonance
# --------------------------------------------------------------------------- #
def test_defense_mode_with_damage_control(mode_ids):
    conf, mode, dc = (mode_ids["Confessor"], mode_ids["Confessor Defense Mode"],
                      mode_ids["Damage Control II"])
    # Data: DC II applies its own 267 (=0.85) as a preMul (op 0) to the ship's armour EM
    # resonance; the mode applies a postDiv (op 1.5). Both are penalisable on a non-stackable
    # attr, but they sit in DIFFERENT operator buckets (preMul precedes postDiv), so each is
    # alone in its chain and applies at full strength: base * 0.85 * (1/1.5).
    base_em_res = _attr(conf, 267)
    dc_factor = _attr(dc, 267)
    assert dc_factor == 0.85 and _attr(mode, 1997) == 1.5

    expected = _post_div(_pre_mul(base_em_res, dc_factor), 1.5)                   # 0.5*0.85/1.5
    ev = _graph(conf, modules=[_mod(dc, SlotKind.LOW, ModuleState.ACTIVE)], mode_type_id=mode)
    assert ev.ship_value(267) == pytest.approx(expected)                         # 0.283333

    tel = _telemetry(conf, modules=[_mod(dc, SlotKind.LOW, ModuleState.ACTIVE)], mode_type_id=mode)
    assert tel.telemetry["defence"]["layers"]["armor"]["resists"]["em"] == round(
        (1.0 - expected) * 100, 1)                                                # 71.7 %


# --------------------------------------------------------------------------- #
# (c) Sharpshooter Mode: lock range + sensor strength divided by 0.5 (doubled)
# --------------------------------------------------------------------------- #
def test_sharpshooter_mode_range_and_sensor(mode_ids):
    conf, mode = mode_ids["Confessor"], mode_ids["Confessor Sharpshooter Mode"]
    # Data: modeMaxTargetRangePostDiv (1991)=0.5 on maxTargetRange (76); the scan-strength
    # effect divides scanRadarStrength (208) by modeRadarStrengthPostDiv (1992)=0.5.
    base_range = _attr(conf, 76)
    base_radar = _attr(conf, 208)
    assert _attr(mode, 1991) == 0.5 and _attr(mode, 1992) == 0.5

    ev = _graph(conf, mode_type_id=mode)
    assert ev.ship_value(76) == pytest.approx(_post_div(base_range, 0.5))        # 45000/0.5=90000
    assert ev.ship_value(208) == pytest.approx(_post_div(base_radar, 0.5))       # 13/0.5=26

    tel = _telemetry(conf, mode_type_id=mode)
    assert tel.telemetry["targeting"]["max_target_range"] == round(_post_div(base_range, 0.5), 0)
    assert tel.telemetry["targeting"]["sensor_strength"] == round(_post_div(base_radar, 0.5), 1)


# --------------------------------------------------------------------------- #
# (d) mode on the WRONG hull (a different T3D) → mode_invalid_for_ship
# --------------------------------------------------------------------------- #
def test_mode_on_wrong_t3d_hull(mode_ids):
    svipul, conf_mode = mode_ids["Svipul"], mode_ids["Confessor Defense Mode"]
    # Both hulls are Tactical Destroyers, but a mode belongs to its OWN hull by name prefix —
    # "Confessor Defense Mode" does not begin with "Svipul", so it is invalid here.
    bad = _telemetry(svipul, mode_type_id=conf_mode)
    d = next(x for x in bad.diagnostics if x.code == "mode_invalid_for_ship")
    assert d.params["mode_type_id"] == conf_mode
    assert d.params["ship_type_id"] == svipul
    assert bad.status.value == "impossible"
    # A mismatched mode must not silently rewrite the hull: sig is the Svipul's base.
    assert bad.telemetry["mobility"]["signature_radius"] == round(_attr(svipul, 552), 1)

    # The Svipul's OWN mode is accepted on the Svipul (name-prefix specificity, not "any T3D").
    ok = _telemetry(svipul, mode_type_id=mode_ids["Svipul Defense Mode"])
    assert "mode_invalid_for_ship" not in _codes(ok)


# --------------------------------------------------------------------------- #
# (e) mode on a NON-T3D hull → mode_invalid_for_ship
# --------------------------------------------------------------------------- #
def test_mode_on_non_t3d_hull(mode_ids):
    rifter, conf_mode = mode_ids["Rifter"], mode_ids["Confessor Defense Mode"]
    bad = _telemetry(rifter, mode_type_id=conf_mode)
    d = next(x for x in bad.diagnostics if x.code == "mode_invalid_for_ship")
    assert d.params["ship_type_id"] == rifter
    assert bad.status.value == "impossible"
    # No mode effect leaked onto the frigate: sig unchanged from its base.
    assert bad.telemetry["mobility"]["signature_radius"] == round(_attr(rifter, 552), 1)


# --------------------------------------------------------------------------- #
# (f) T3D with NO mode → valid, base attributes unchanged
# --------------------------------------------------------------------------- #
def test_t3d_without_mode_is_valid_bare(mode_ids):
    conf = mode_ids["Confessor"]
    tel = _telemetry(conf, skills=SkillProfile.omniscient())
    assert tel.status.value == "valid"
    assert "mode_invalid_for_ship" not in _codes(tel)
    assert "mode" not in tel.telemetry["ship"]                     # nothing echoed
    # Base attributes: no mode → no division. Sig and armour EM resist are the hull's own.
    assert tel.telemetry["mobility"]["signature_radius"] == round(_attr(conf, 552), 1)  # 65.0
    assert tel.telemetry["defence"]["layers"]["armor"]["resists"]["em"] == round(
        (1.0 - _attr(conf, 267)) * 100, 1)                         # 50.0 %


# --------------------------------------------------------------------------- #
# (g) Siege Module II active on a Revelation: hand-derived deltas from effect 6582
# --------------------------------------------------------------------------- #
def test_siege_module_on_revelation(siege_ids):
    rev, siege = siege_ids["Revelation"], siege_ids["Siege Module II"]
    # effect 6582 (moduleBonusSiegeModule, category 1 = active) real modifier rows:
    #   mass(4)           postMul  siegeMassMultiplier(1471)=10   → ×10 (dread can't move)
    #   maxVelocity(37)   postPercent speedFactor(20)=-100        → ×0  (immobilised)
    #   sensorDampenerResistance(2112) postPercent sensorDampenerResistanceBonus(2351)=-70
    #   remoteRepairImpedance(2116)    postPercent remoteRepairImpedanceBonus(2342)=-99.9999
    base_mass, base_vel = _attr(rev, 4), _attr(rev, 37)
    mass_mult = _attr(siege, 1471)
    speed_factor = _attr(siege, 20)
    damp_res_bonus = _attr(siege, 2351)
    rep_imp_bonus = _attr(siege, 2342)
    assert (mass_mult, speed_factor, damp_res_bonus) == (10.0, -100.0, -70.0)

    ev = _graph(rev, modules=[_mod(siege, SlotKind.HIGH, ModuleState.ACTIVE)])
    assert ev.ship_value(4) == pytest.approx(_post_mul(base_mass, mass_mult))         # 1.29e10
    assert ev.ship_value(37) == pytest.approx(_post_percent(base_vel, speed_factor))  # 0.0
    # ewar-resistance attrs default to 1.0 on the hull; the siege module scales them.
    assert ev.ship_value(2112) == pytest.approx(_post_percent(1.0, damp_res_bonus))   # 0.30
    assert ev.ship_value(2116) == pytest.approx(_post_percent(1.0, rep_imp_bonus))    # ~1e-6

    tel = _telemetry(rev, modules=[_mod(siege, SlotKind.HIGH, ModuleState.ACTIVE)])
    assert tel.telemetry["mobility"]["max_velocity"] == 0.0
    assert tel.telemetry["mobility"]["mass"] == round(base_mass * mass_mult, 0)


# --------------------------------------------------------------------------- #
# (h) Bastion Module I active on a Golem: shield-boost + rate-of-fire deltas (effect 6658)
# --------------------------------------------------------------------------- #
def test_bastion_module_on_golem(bastion_ids):
    golem = bastion_ids["Golem"]
    bastion = bastion_ids["Bastion Module I"]
    booster = bastion_ids["Large Shield Booster II"]
    launcher = bastion_ids["Cruise Missile Launcher II"]
    # effect 6658 (moduleBonusBastionModule, category 1) real modifier rows used here:
    #   shieldBonus(68)  postPercent shieldBoostMultiplier(548)=60  (skill 3416 Shield Op)
    #   duration(73)     postPercent bastionModeShieldBoosterCapDurationBonus(6187)=-20 (3416)
    #   speed/RoF(51)    postPercent bastionMissileROFBonus(3108)=-50 (skill 20212 Cruise Spec)
    #   maxVelocity(37)  postPercent speedFactor(20)=-100 on the ship (immobilised)
    base_boost, base_dur = _attr(booster, 68), _attr(booster, 73)
    base_rof = _attr(launcher, 51)
    boost_mult, dur_bonus = _attr(bastion, 548), _attr(bastion, 6187)
    rof_bonus, speed_factor = _attr(bastion, 3108), _attr(bastion, 20)
    assert (boost_mult, dur_bonus, rof_bonus, speed_factor) == (60.0, -20.0, -50.0, -100.0)
    # Large Shield Booster II requires Shield Operation (3416); Cruise Missile Launcher II
    # requires Cruise Missile Specialization (20212) — exactly one bastion missile-RoF row
    # matches, so the bonus applies once.
    from apps.sde.models import SdeTypeSkill
    booster_skills = set(SdeTypeSkill.objects.filter(type_id=booster).values_list("skill_type_id", flat=True))
    launcher_skills = set(SdeTypeSkill.objects.filter(type_id=launcher).values_list("skill_type_id", flat=True))
    assert 3416 in booster_skills
    assert launcher_skills & {3325, 3326, 20212, 20213} == {20212}

    ev = _graph(golem, modules=[_mod(bastion, SlotKind.HIGH, ModuleState.ACTIVE),
                                _mod(booster, SlotKind.MED, ModuleState.ACTIVE),
                                _mod(launcher, SlotKind.HIGH, ModuleState.ACTIVE)])
    b = _module(ev, booster)
    la = _module(ev, launcher)
    exp_boost = _post_percent(base_boost, boost_mult)                 # 276*1.6 = 441.6
    exp_dur = _post_percent(base_dur, dur_bonus)                      # 4000*0.8 = 3200
    exp_rof = _post_percent(base_rof, rof_bonus)                      # 16540*0.5 = 8270
    assert ev.value(b, 68) == pytest.approx(exp_boost)
    assert ev.value(b, 73) == pytest.approx(exp_dur)
    assert ev.value(la, 51) == pytest.approx(exp_rof)
    assert ev.ship_value(37) == pytest.approx(_post_percent(_attr(golem, 37), speed_factor))  # 0.0

    # Telemetry: shield boost per second = boosted amount / boosted cycle.
    tel = _telemetry(golem, modules=[_mod(bastion, SlotKind.HIGH, ModuleState.ACTIVE),
                                     _mod(booster, SlotKind.MED, ModuleState.ACTIVE),
                                     _mod(launcher, SlotKind.HIGH, ModuleState.ACTIVE)])
    assert tel.telemetry["defence"]["active_tank"]["shield_hps"] == round(
        exp_boost / (exp_dur / 1000.0), 1)                           # 441.6 / 3.2 = 138.0


# --------------------------------------------------------------------------- #
# (i) mode persistence round-trip through the services layer (save → load → same mode)
# --------------------------------------------------------------------------- #
def test_mode_persistence_round_trip(mode_ids):
    from django.contrib.auth import get_user_model

    from apps.fitting import services

    conf, mode, dc = (mode_ids["Confessor"], mode_ids["Confessor Defense Mode"],
                      mode_ids["Damage Control II"])
    user = get_user_model().objects.create(username="eve:t3d-pilot", first_name="T3D Pilot")
    items = [
        {"type_id": dc, "slot": "low", "state": "active", "charge_type_id": None, "quantity": 1},
        {"type_id": mode, "slot": "mode", "state": "active", "charge_type_id": None, "quantity": 1},
    ]
    fit = services.create_fit(user, name="Confessor", ship_type_id=conf, items=items)
    rev = fit.current_revision

    # The mode persists in the revision's items blob as the slot="mode" entry ...
    stored = [it for it in rev.items if services.is_mode_item(it)]
    assert len(stored) == 1 and int(stored[0]["type_id"]) == mode
    # ... and is lifted back out as FitInput.mode_type_id, NOT fitted as a rack module.
    loaded = services.fit_input_from_items(rev.ship_type_id, rev.items)
    assert loaded.mode_type_id == mode
    assert [m.type_id for m in loaded.modules] == [dc]

    # The explicit live-editor override (API key) wins over the blob.
    override = services.fit_input_from_items(conf, items, mode_type_id=mode_ids["Confessor Sharpshooter Mode"])
    assert override.mode_type_id == mode_ids["Confessor Sharpshooter Mode"]
