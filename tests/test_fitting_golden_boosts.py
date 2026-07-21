"""Golden fits: WS-7 fleet boosts (warfare buffs from friendly command bursts).

Every expectation is hand-derived from the fixture's own base attributes and the imported
``SdeDbuff`` rows (operation + aggregateMode) plus the burst charge's warfareBuff multiplier
— never read back from the engine. Attribute ids and base values are asserted from the slice
before they drive an expectation, so a fixture drift fails loudly.

Mechanics proven here
---------------------
* **Data-driven buff table**: a burst CHARGE names a warfare buff id (warfareBuff1ID, attr
  2468) + a strength multiplier (warfareBuff1Multiplier, 2596). The buff id resolves to an
  ``SdeDbuff`` (imported from CCP's ``dbuffCollections.yaml``) that says what it changes, on
  what, with which operator. The burst MODULE's default effect has ZERO dogma modifiers — the
  per-ally application is not dogma — so the engine applies the buff by hand from this table.
* **Unbonused default strength**: an UNBONUSED T1 burst carries warfareBuffValue 1.0, and its
  chargeBonusWarfareCharge effect postMultiplies that by the charge's multiplier — so the
  effective strength is exactly the multiplier (−8 for Shield Harmonizing). ``strength_pct``
  overrides that default (the "boosted by a real command ship" seam), scaling proportionally.
* **aggregateMode**: several boosts granting the same buff id do NOT sum — the strongest
  single instance wins (Maximum → max value, Minimum → min). Two identical boosts equal one.
* **Stacking penalty is governed by the TARGET attribute's stackable flag**, exactly like a
  fitted module bonus (verified against pyfa: every warfare-buff penalty choice matches the
  attribute's ``stackable`` flag; a resonance/scan-res/sig buff is penalised and shares the
  penalty chain with local modules, an HP/capacity buff is not). NOT a penalty-free source.
* **Modifier kinds**: ``item`` → the boosted ship (Shield Harmonizing resonances, Armor
  Reinforcement HP); ``locationRequiredSkill`` → fitted modules requiring a skill (Rapid
  Deployment → an afterburner's speedFactor).
* A boost referencing a buff id absent from the table → ``boost_unknown_buff`` (advisory),
  changes nothing. Boosts are excluded from EFT export / price / stock / doctrine.

Fixture: tests/fixtures/fitting/boosts.json (Rifter victim + burst charges + an afterburner +
a shield hardener for the shared-penalty case).
"""
from __future__ import annotations

import math

import pytest

from apps.fitting.engine.types import (
    BoostInput,
    FitInput,
    ModuleInput,
    ModuleState,
    OperatingProfile,
    SkillProfile,
    SlotKind,
)

from ._fitting_graph_utils import load_graph_fixture

pytestmark = pytest.mark.django_db

# The engine's stacking-penalty factor, reproduced so the shared-chain expectation runs the
# same maths as graph._calculate (the second element of a penalised chain is multiplied by
# this once — _PENALTY_FACTOR ** (i*i) with i=1).
_PENALTY = math.exp(-((1.0 / 2.67) ** 2))

# warfareBuff1{ID,Multiplier} attribute ids on a burst charge.
_BUFF1_ID, _BUFF1_MULT = 2468, 2596
# Shield resonances (item buff 10) / armorHP (item buff 15) / speedFactor (loc-skill buff 22).
_SHIELD_RESONANCES = (271, 272, 273, 274)
_ARMOR_HP = 265
_SPEED_FACTOR = 20
_HARDENER_RES_BONUS = (984, 985, 986, 987)   # Multispectrum Shield Hardener II resonance bonus


@pytest.fixture()
def ids():
    return load_graph_fixture("boosts")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _boost(charge_type_id, strength_pct=None):
    return BoostInput(charge_type_id=charge_type_id, strength_pct=strength_pct)


def _mod(tid, slot, state=ModuleState.ACTIVE):
    return ModuleInput(type_id=tid, slot=slot, state=state)


def _graph(ship, modules=(), boosts=(), skills=None):
    """Passes 1-3 only (raw evaluated attributes). Empty skills so nothing but the fitted
    modules + boosts move an attribute; the full skill catalogue is still materialised at
    level 0."""
    from apps.fitting.engine.adapter import ORMDataProvider
    from apps.fitting.engine.graph import evaluate_attributes

    prov = ORMDataProvider()
    fit = FitInput(ship_type_id=ship, modules=tuple(modules), boosts=tuple(boosts))
    return evaluate_attributes(fit, skills or SkillProfile.from_dict({}), prov,
                               skill_ids=prov.trained_skill_ids())


def _telemetry(ship, modules=(), boosts=(), skills=None, op=None):
    from apps.fitting.engine.adapter import FittingEngine

    engine = FittingEngine()
    fit = FitInput(ship_type_id=ship, modules=tuple(modules), boosts=tuple(boosts))
    return engine.evaluate(fit, skills or SkillProfile.from_dict({}),
                           op or OperatingProfile(propulsion_active=False))


def _codes(res):
    return {d.code for d in res.diagnostics}


# --------------------------------------------------------------------------- #
# (a) Shield Harmonizing: −8% to every shield resonance (item buff, resistance improves)
# --------------------------------------------------------------------------- #
def test_shield_harmonizing_default_strength(ids):
    rifter, charge = ids["Rifter"], ids["Shield Harmonizing Charge"]
    assert _attr(charge, _BUFF1_ID) == 10.0          # warfareBuff1ID → dbuff 10
    mult = _attr(charge, _BUFF1_MULT)
    assert mult == -8.0                              # warfareBuff1Multiplier
    # Unbonused T1 burst base warfareBuffValue is 1.0, so the effective strength is the mult.
    strength = 1.0 * mult                            # −8.0

    ev = _graph(rifter, boosts=[_boost(charge)])
    for res in _SHIELD_RESONANCES:
        base = _attr(rifter, res)
        expected = base * (1.0 + strength / 100.0)   # PostPercent → ×0.92 (resist improves)
        assert ev.ship_value(res) == pytest.approx(expected)

    # Telemetry lists the boost with its resolved buff.
    tel = _telemetry(rifter, boosts=[_boost(charge)])
    b = tel.telemetry["boosts"]
    assert b["count"] == 1
    assert b["boosts"][0]["charge_type_id"] == charge
    buff = b["boosts"][0]["buffs"][0]
    assert buff["buff_id"] == 10 and buff["applied"] is True
    assert buff["strength_pct"] == pytest.approx(-8.0)
    assert "boost_unknown_buff" not in _codes(tel)


# --------------------------------------------------------------------------- #
# (b) two identical boosts → aggregateMode Minimum → identical to one (bursts don't sum)
# --------------------------------------------------------------------------- #
def test_two_identical_boosts_do_not_sum(ids):
    rifter, charge = ids["Rifter"], ids["Shield Harmonizing Charge"]
    res = _SHIELD_RESONANCES[0]
    one = _graph(rifter, boosts=[_boost(charge)]).ship_value(res)
    two = _graph(rifter, boosts=[_boost(charge), _boost(charge)]).ship_value(res)
    # Buff 10 aggregateMode is Minimum: min(−8, −8) = −8 → the same single application.
    assert two == pytest.approx(one)
    # And strictly better than nothing (a real change, not a no-op).
    assert one < _attr(rifter, res)


# --------------------------------------------------------------------------- #
# (c) strength_pct override scales the delta proportionally
# --------------------------------------------------------------------------- #
def test_strength_override_scales_delta(ids):
    rifter, charge = ids["Rifter"], ids["Shield Harmonizing Charge"]
    res = _SHIELD_RESONANCES[0]
    base = _attr(rifter, res)

    ev = _graph(rifter, boosts=[_boost(charge, strength_pct=-16.0)])
    expected = base * (1.0 + (-16.0) / 100.0)        # override replaces −8 with −16 → ×0.84
    assert ev.ship_value(res) == pytest.approx(expected)
    # Exactly double the default's delta from base.
    default_delta = base - _graph(rifter, boosts=[_boost(charge)]).ship_value(res)
    override_delta = base - ev.ship_value(res)
    assert override_delta == pytest.approx(2.0 * default_delta)


# --------------------------------------------------------------------------- #
# (d) Rapid Deployment (locationRequiredSkill): +12% speedFactor on the afterburner only
# --------------------------------------------------------------------------- #
def test_rapid_deployment_boosts_afterburner_speed(ids):
    rifter, charge, ab = (ids["Rifter"], ids["Rapid Deployment Charge"],
                          ids["1MN Afterburner II"])
    assert _attr(charge, _BUFF1_ID) == 22.0          # warfareBuff1ID → dbuff 22 (loc-skill)
    mult = _attr(charge, _BUFF1_MULT)
    assert mult == 12.0

    bare = _graph(rifter, modules=[_mod(ab, SlotKind.MED)])
    ab_bare = next(m for m in bare.modules if m.type_id == ab)
    base_sf = bare.value(ab_bare, _SPEED_FACTOR)
    assert base_sf == pytest.approx(_attr(ab, _SPEED_FACTOR))   # 135, unmodified at level 0

    boosted = _graph(rifter, modules=[_mod(ab, SlotKind.MED)], boosts=[_boost(charge)])
    ab_ent = next(m for m in boosted.modules if m.type_id == ab)
    expected = base_sf * (1.0 + mult / 100.0)        # 135 × 1.12 = 151.2
    assert boosted.value(ab_ent, _SPEED_FACTOR) == pytest.approx(expected)
    # The buff is skill-scoped: it does NOT touch the ship's own speedFactor default.
    assert boosted.ship_value(_SPEED_FACTOR) == pytest.approx(bare.ship_value(_SPEED_FACTOR))


# --------------------------------------------------------------------------- #
# (e1) Armor Reinforcement on armorHP: a STACKABLE attr → applied, never penalised
# --------------------------------------------------------------------------- #
def test_armor_reinforcement_hp_not_penalised(ids):
    rifter, charge = ids["Rifter"], ids["Armor Reinforcement Charge"]
    assert _attr(charge, _BUFF1_ID) == 15.0          # dbuff 15 (armorHP, Maximum)
    mult = _attr(charge, _BUFF1_MULT)
    assert mult == 8.0

    base_hp = _attr(rifter, _ARMOR_HP)
    ev = _graph(rifter, boosts=[_boost(charge)])
    # armorHP (265) is stackable=True → PostPercent applies at full strength (no penalty).
    assert ev.ship_value(_ARMOR_HP) == pytest.approx(base_hp * (1.0 + mult / 100.0))


# --------------------------------------------------------------------------- #
# (e2) boost + a local module on the SAME non-stackable attr → shared penalty chain
# --------------------------------------------------------------------------- #
def test_boost_shares_stacking_penalty_with_local_module(ids):
    rifter, charge, hardener = (ids["Rifter"], ids["Shield Harmonizing Charge"],
                                ids["Multispectrum Shield Hardener II"])
    res = _SHIELD_RESONANCES[0]                       # EM resonance
    base = _attr(rifter, res)
    h = _attr(hardener, _HARDENER_RES_BONUS[0]) / 100.0   # −0.325 (hardener PostPercent)
    b = _attr(charge, _BUFF1_MULT) / 100.0                # −0.08  (boost PostPercent)
    assert h == -0.325 and b == -0.08

    ev = _graph(rifter, modules=[_mod(hardener, SlotKind.MED)], boosts=[_boost(charge)])
    # Both are penalised negative multipliers on a non-stackable attr; the larger magnitude
    # (hardener) takes no penalty, the boost is penalised once (×_PENALTY).
    expected = base * (1.0 + h) * (1.0 + b * _PENALTY)
    assert ev.ship_value(res) == pytest.approx(expected)

    # Bracketed by the two wrong answers: strictly stronger reduction than the hardener alone
    # (the boost still helps), but weaker than a naive un-penalised stack.
    hardener_alone = base * (1.0 + h)
    naive_unpenalised = base * (1.0 + h) * (1.0 + b)
    assert naive_unpenalised < ev.ship_value(res) < hardener_alone


# --------------------------------------------------------------------------- #
# (f) a charge whose buff id is absent from the table → boost_unknown_buff, no change
# --------------------------------------------------------------------------- #
def test_boost_unknown_buff(ids):
    from apps.fitting.engine import adapter
    from apps.sde.models import SdeDbuff

    rifter, charge = ids["Rifter"], ids["Shield Harmonizing Charge"]
    res = _SHIELD_RESONANCES[0]
    bare = _attr(rifter, res)

    # Strip buff 10 from the imported table; clear the per-data-version dbuff cache around the
    # mutation so neither this test nor its neighbours see a stale entry (the cache is module
    # state, not rolled back with the test transaction).
    adapter._DBUFF_CACHE.clear()
    SdeDbuff.objects.filter(buff_id=10).delete()
    try:
        allv = SkillProfile.omniscient()
        tel = _telemetry(rifter, boosts=[_boost(charge)], skills=allv)
        d = next(x for x in tel.diagnostics if x.code == "boost_unknown_buff")
        assert d.severity.value == "warning"
        assert d.params["charge_type_id"] == charge and d.params["buff_id"] == 10
        # Advisory only — never structural.
        assert tel.status.value in ("valid", "warnings")
        # The resonance is unchanged: an unknown buff applies nothing.
        assert tel.telemetry["defence"]["layers"]["shield"]["resists"]["em"] == \
            round((1.0 - bare) * 100, 1)
        assert tel.telemetry["boosts"]["boosts"][0]["buffs"][0]["applied"] is False
    finally:
        adapter._DBUFF_CACHE.clear()


# --------------------------------------------------------------------------- #
# (g) boosts are excluded from EFT export / price / stock / doctrine
# --------------------------------------------------------------------------- #
def test_boost_excluded_from_export_and_overlays(ids):
    from apps.fitting import services

    rifter, charge = ids["Rifter"], ids["Shield Harmonizing Charge"]
    items = [
        {"type_id": charge, "slot": "boost", "state": "active",
         "charge_type_id": None, "quantity": 1, "strength_pct": -12.0},
    ]
    # Persistence path: a slot="boost" entry becomes FitInput.boosts, not a module, and it
    # carries the strength override through.
    loaded = services.fit_input_from_items(rifter, items)
    assert [b.charge_type_id for b in loaded.boosts] == [charge]
    assert loaded.boosts[0].strength_pct == -12.0
    assert loaded.modules == ()

    # EFT export omits the boost marker (EFT has no such concept).
    eft = services.export_eft(rifter, items, "Boosted")
    assert "Shield Harmonizing" not in eft
    # Pricing / stock never count a boost (it is not part of the fit's cost).
    assert charge not in {li["type_id"] for li in services.price_fit(rifter, items)["lines"]}
    assert charge not in {r["type_id"] for r in services.stock_coverage(rifter, items)["rows"]}
