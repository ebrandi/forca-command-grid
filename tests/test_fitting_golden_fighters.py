"""Golden fits: fighter squadrons (WS-12, engine v2, real SDE slice, hand-derived numbers).

Fixture: tests/fixtures/fitting/fighters_carrier.json — a real CCP data slice
(Archon + Nidhoggur carriers, Templar II / Einherji II light fighters, Dromi I support
fighter, Shadow heavy fighter, Rifter, one PLACEHOLDER row, and every transitively required
skill + the carrier trait chain). Every expected value is DERIVED IN THE TEST from the
slice's base attributes plus documented EVE mechanics — never read back from the engine.

Fighter damage chain (all verified live 2026-07-21 against the dev DB's dogma rows):
* A squadron is materialised as a graph entity (graph.build_entities) and joins the ship's
  "located" items, so every fighter-damage bonus reaches it through the ordinary
  OwnerRequiredSkillModifier pipeline — there is NO fighter-specific modifier code.
* Standard-attack squadron DPS = Σ(damage 2227-2230) × multiplier(2226) × count /
  (duration 2233 / 1000). Support fighters (Dromi, no 2226) deal no damage.
* Skills postPercent the multiplier(2226), magnitude = the skill's damageMultiplierBonus(292)
  × level (292 pre-multiplied by skillLevel via effect 152). Applying to any fighter that
  requires the filter skill: Fighters(23069) 5%/lvl, Drone Interfacing(3442) 10%/lvl,
  racial Fighter Specialization 2%/lvl; Heavy Fighters(32339) 5%/lvl (heavy only). All are
  skill sources (category 16, stacking-EXEMPT) so they multiply cleanly, no penalty.
* Carrier hull trait: the Nidhoggur's shipBonusCarrierM1FighterDamage (effect 6602)
  postPercents the same multiplier by shipBonusCarrierM1(2371), itself pre-multiplied by the
  Minmatar Carrier skill level (effect 6586). Ship source (category 6, exempt). The Archon
  carries NO fighter-damage trait (only support-fighter range/neut) — verified.
"""
from __future__ import annotations

import pytest

from apps.fitting.engine.adapter import FittingEngine
from apps.fitting.engine.types import (
    FighterInput,
    FitInput,
    ModuleInput,
    OperatingProfile,
    SkillProfile,
    SlotKind,
    TargetProfile,
)

from ._fitting_graph_utils import load_graph_fixture

pytestmark = pytest.mark.django_db

# Dogma attribute ids (CCP SDE) used by the derivations below.
FA_MULT = 2226           # fighterAbilityAttackMissileDamageMultiplier (stackable=false)
FA_DUR = 2233            # fighterAbilityAttackMissileDuration (ms)
FA_DMG = (2227, 2228, 2229, 2230)   # em / thermal / kinetic / explosive
SKILL_DMG_BONUS = 292    # damageMultiplierBonus (%/level on fighter skills)
SHIP_CARRIER_M1 = 2371   # shipBonusCarrierM1 on the Nidhoggur (%/Minmatar Carrier level)
SQUAD_MAX = 2215         # fighterSquadronMaxSize (on the fighter type)
VOLUME = 161


@pytest.fixture()
def ids():
    # with_skills=False: the slice is self-contained for fighters (it carries every skill
    # that touches fighter damage), so isolating to it makes the trained-skill set exact.
    return load_graph_fixture("fighters_carrier", with_skills=False)


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _shot(type_id) -> float:
    return sum(_attr(type_id, a) for a in FA_DMG)


def _base_squadron_dps(type_id, count) -> float:
    """Unmodified squadron DPS from base attributes alone (all skills at level 0)."""
    mult = _attr(type_id, FA_MULT)                     # 1.0 base
    return count * _shot(type_id) * mult / (_attr(type_id, FA_DUR) / 1000.0)


def _eval(ship, fighters=(), modules=(), skills=None, op=None):
    engine = FittingEngine()
    return engine.evaluate(
        FitInput(ship_type_id=ship, modules=tuple(modules), fighters=tuple(fighters)),
        skills or SkillProfile.omniscient(),
        op or OperatingProfile(propulsion_active=False))


def _light_allv_mult(ids, spec_skill_name) -> float:
    """The all-V damage multiplier for a LIGHT fighter (no carrier damage bonus): base 1.0 ×
    Fighters × Drone Interfacing × racial Specialization, each postPercent = its DB attr
    292 × 5. Light Fighters is a prereq with no damage bonus; Heavy Fighters does not apply."""
    fighters = 1 + _attr(ids["Fighters"], SKILL_DMG_BONUS) * 5 / 100.0            # +25%
    di = 1 + _attr(ids["Drone Interfacing"], SKILL_DMG_BONUS) * 5 / 100.0         # +50%
    spec = 1 + _attr(ids[spec_skill_name], SKILL_DMG_BONUS) * 5 / 100.0           # +10%
    return fighters * di * spec


# --------------------------------------------------------------------------- #
# (a) Isolation: base squadron maths, no skills
# --------------------------------------------------------------------------- #
def test_templar_squadron_untrained_base_dps(ids):
    """6x Templar II with NO skills on an Archon: every skill sits at level 0, so the damage
    multiplier(2226) stays at its base 1.0 and the squadron fires at raw attributes:
    108 EM x 1.0 x 6 / 5.0 s = 129.6 dps."""
    archon, templar = ids["Archon"], ids["Templar II"]
    res = _eval(archon, fighters=[FighterInput(templar, 6)], skills=SkillProfile.from_dict({}))
    off = res.telemetry["offence"]
    expected = _base_squadron_dps(templar, 6)                # 129.6
    assert off["fighter_dps"] == pytest.approx(expected, rel=2e-3)
    assert off["total_dps"] == pytest.approx(expected, rel=2e-3)
    assert off["turret_dps"] == 0.0 and off["missile_dps"] == 0.0
    fs = res.telemetry["fighters"]
    assert fs["totals"]["fighter_dps"] == pytest.approx(expected, rel=2e-3)
    assert fs["totals"]["tubes_used"] == 1 and fs["totals"]["tubes_total"] == 4
    sq = fs["squadrons"][0]
    assert sq["role"] == "light" and sq["count"] == 6 and sq["max_squadron_size"] == 6
    assert sq["unit_dps"] == pytest.approx(expected / 6, rel=2e-3)
    assert res.status.value == "missing_skills"              # fighters need trained skills


# --------------------------------------------------------------------------- #
# (b) All-V skills (Archon has NO carrier damage bonus — skills only)
# --------------------------------------------------------------------------- #
def test_templar_squadron_all_v_on_archon(ids):
    """6x Templar II, all V, on an Archon. The Archon grants no fighter-damage trait, so the
    multiplier is the pure skill chain: 1.0 x 1.25 (Fighters) x 1.50 (Drone Interfacing)
    x 1.10 (Amarr Fighter Specialization) = 2.0625 -> 108 x 2.0625 x 6 / 5.0 = 267.3 dps."""
    archon, templar = ids["Archon"], ids["Templar II"]
    res = _eval(archon, fighters=[FighterInput(templar, 6)])
    mult = _light_allv_mult(ids, "Amarr Fighter Specialization")
    expected = _base_squadron_dps(templar, 6) * mult          # 267.3
    assert res.telemetry["offence"]["fighter_dps"] == pytest.approx(expected, rel=2e-3)
    assert res.status.value in ("valid", "warnings")


def test_einherji_squadron_carrier_bonus_proof(ids):
    """PROOF that a carrier HULL bonus reaches fighters. 6x Einherji II, all V:
    * on an Archon (no fighter-damage trait): 1.25 x 1.50 x 1.10 (Minmatar spec) = 2.0625.
    * on a Nidhoggur: additionally x(1 + shipBonusCarrierM1 x Carrier-V / 100). The Nidhoggur's
      2371 base is 5, pre-multiplied by Minmatar Carrier level 5 -> 25 -> x1.25.
    The Nidhoggur DPS is therefore EXACTLY the Archon DPS times the carrier factor, proving the
    hull trait flows onto the squadron through the OwnerRequiredSkillModifier graph."""
    archon, nidhoggur, einherji = ids["Archon"], ids["Nidhoggur"], ids["Einherji II"]
    skill_mult = _light_allv_mult(ids, "Minmatar Fighter Specialization")
    carrier_factor = 1 + _attr(nidhoggur, SHIP_CARRIER_M1) * 5 / 100.0      # x1.25

    on_archon = _eval(archon, fighters=[FighterInput(einherji, 6)])
    on_nidhog = _eval(nidhoggur, fighters=[FighterInput(einherji, 6)])
    base = _base_squadron_dps(einherji, 6)

    assert on_archon.telemetry["offence"]["fighter_dps"] == pytest.approx(
        base * skill_mult, rel=2e-3)                          # 252.45
    assert on_nidhog.telemetry["offence"]["fighter_dps"] == pytest.approx(
        base * skill_mult * carrier_factor, rel=2e-3)         # 315.56
    # The ONLY difference between the two is the carrier's damage trait.
    assert (on_nidhog.telemetry["offence"]["fighter_dps"]
            / on_archon.telemetry["offence"]["fighter_dps"]) == pytest.approx(
        carrier_factor, rel=2e-3)


def test_heavy_fighter_only_on_heavy(ids):
    """The Heavy Fighters skill (32339, 5%/lvl) boosts only fighters that REQUIRE it. A light
    Templar squadron therefore does not get it — its all-V multiplier excludes Heavy Fighters,
    which the _light_allv_mult chain already omits (proven by the exact Archon match above)."""
    archon, templar = ids["Archon"], ids["Templar II"]
    res = _eval(archon, fighters=[FighterInput(templar, 6)])
    # Multiplier with Heavy Fighters wrongly folded in would be ~1.25 higher; the exact match
    # to the light-only chain confirms it is correctly filtered out.
    mult = _light_allv_mult(ids, "Amarr Fighter Specialization")
    assert res.telemetry["offence"]["fighter_dps"] == pytest.approx(
        _base_squadron_dps(templar, 6) * mult, rel=2e-3)


def test_support_fighter_deals_no_damage(ids):
    """A Dromi I support squadron carries no standard-attack multiplier (2226 absent), so it
    deals zero DPS and reports no damaging ability, but still occupies a support slot."""
    archon, dromi = ids["Archon"], ids["Dromi I"]
    res = _eval(archon, fighters=[FighterInput(dromi, 3)])
    off = res.telemetry["offence"]
    assert off["fighter_dps"] == 0.0
    sq = res.telemetry["fighters"]["squadrons"][0]
    assert sq["role"] == "support" and sq["abilities"] == []
    assert "no_weapons_detected" in res.unsupported


# --------------------------------------------------------------------------- #
# (c)-(f) Structural validations
# --------------------------------------------------------------------------- #
def test_tubes_exceeded(ids):
    """3 Templar (light) + 2 Dromi (support) = 5 squadrons fills the Archon's role slots
    (3 light, 2 support) exactly but exceeds its 4 fighter TUBES -> impossible, tubes only."""
    archon, templar, dromi = ids["Archon"], ids["Templar II"], ids["Dromi I"]
    res = _eval(archon, fighters=[FighterInput(templar, 6), FighterInput(templar, 6),
                                  FighterInput(templar, 6), FighterInput(dromi, 3),
                                  FighterInput(dromi, 3)])
    codes = {d.code for d in res.diagnostics}
    assert "fighter_tubes_exceeded" in codes
    assert "fighter_role_slots_exceeded" not in codes
    tubes = next(d for d in res.diagnostics if d.code == "fighter_tubes_exceeded")
    assert tubes.params == {"used": 5, "cap": 4}
    assert res.status.value == "impossible"


def test_role_slots_exceeded(ids):
    """4 Templar squadrons: 4 tubes (ok) but 4 light > the Archon's 3 light slots -> impossible
    on the light role, not on tubes."""
    archon, templar = ids["Archon"], ids["Templar II"]
    res = _eval(archon, fighters=[FighterInput(templar, 6)] * 4)
    codes = {d.code for d in res.diagnostics}
    assert "fighter_role_slots_exceeded" in codes
    assert "fighter_tubes_exceeded" not in codes
    role = next(d for d in res.diagnostics if d.code == "fighter_role_slots_exceeded")
    assert role.params == {"role": "light", "used": 4, "cap": 3}
    assert res.status.value == "impossible"


def test_heavy_fighter_no_heavy_slots_on_archon(ids):
    """A Shadow heavy squadron on the Archon: the hull declares no fighterHeavySlots (heavy
    bays are supercarrier-only), so its heavy role slot count is 0 -> role slots exceeded."""
    archon, shadow = ids["Archon"], ids["Shadow"]
    res = _eval(archon, fighters=[FighterInput(shadow, 6)])
    role = next((d for d in res.diagnostics if d.code == "fighter_role_slots_exceeded"), None)
    assert role is not None and role.params == {"role": "heavy", "used": 1, "cap": 0}
    assert res.status.value == "impossible"


def test_squadron_oversized(ids):
    """7 Templar in a squadron whose max size is 6 -> impossible, oversized."""
    archon, templar = ids["Archon"], ids["Templar II"]
    res = _eval(archon, fighters=[FighterInput(templar, 7)])
    over = next((d for d in res.diagnostics if d.code == "fighter_squadron_oversized"), None)
    assert over is not None
    assert over.params == {"type_id": templar, "name": "Templar II",
                           "count": 7, "max": int(_attr(templar, SQUAD_MAX))}
    assert res.status.value == "impossible"


def test_fighter_bay_exceeded(ids):
    """Enough Dromi squadrons (3000 m3 each x3) to overflow the Archon's 65000 m3 bay ->
    fighter_bay_exceeded fires (alongside the tube/role overflow that this over-cram implies).
    In v1 only launched squadrons are modelled, so the bay check is a belt-and-braces guard."""
    archon, dromi = ids["Archon"], ids["Dromi I"]
    n = 8                                                       # 8 x 3 x 3000 = 72000 > 65000
    res = _eval(archon, fighters=[FighterInput(dromi, 3)] * n)
    codes = {d.code for d in res.diagnostics}
    assert "fighter_bay_exceeded" in codes
    bay = next(d for d in res.diagnostics if d.code == "fighter_bay_exceeded")
    assert bay.params["used"] == pytest.approx(n * 3 * _attr(dromi, VOLUME))
    assert bay.params["cap"] == pytest.approx(_attr(archon, 2055))
    assert res.status.value == "impossible"


def test_fighters_on_non_carrier(ids):
    """A Templar squadron on a Rifter: the hull has no fighterTubes attribute at all ->
    fighter_on_non_carrier, impossible."""
    rifter, templar = ids["Rifter"], ids["Templar II"]
    res = _eval(rifter, fighters=[FighterInput(templar, 6)])
    non = next((d for d in res.diagnostics if d.code == "fighter_on_non_carrier"), None)
    assert non is not None and non.params == {"squadrons": 1}
    assert res.status.value == "impossible"


def test_placeholder_type_rejected(ids):
    """A category-87 PLACEHOLDER scaffold row is not a real fighter -> fighter_invalid_type,
    and it contributes no DPS."""
    archon, placeholder = ids["Archon"], ids["Able_PLACEHOLDER"]
    res = _eval(archon, fighters=[FighterInput(placeholder, 1)])
    inv = next((d for d in res.diagnostics if d.code == "fighter_invalid_type"), None)
    assert inv is not None and inv.params["type_id"] == placeholder
    assert res.telemetry["offence"]["fighter_dps"] == 0.0
    assert res.status.value == "impossible"


# --------------------------------------------------------------------------- #
# (i) Applied DPS is not modelled for fighters in v1
# --------------------------------------------------------------------------- #
def test_fighter_applied_null_with_reason(ids):
    """With a target profile set, every squadron reports applied_dps=None + a reason (fighter
    tracking/explosion application is a documented v1 gap), and the fit's applied total is
    honestly flagged incomplete rather than silently counting fighters at full damage."""
    archon, templar = ids["Archon"], ids["Templar II"]
    op = OperatingProfile(propulsion_active=False,
                          target=TargetProfile(signature_radius=125.0, velocity=300.0,
                                               target_distance_m=10000.0))
    res = _eval(archon, fighters=[FighterInput(templar, 6)], op=op)
    sq = res.telemetry["fighters"]["squadrons"][0]
    assert sq["applied_dps"] is None
    assert sq["applied_reason"] == "fighter_application_not_modelled"
    off = res.telemetry["offence"]
    assert off["fighter_dps"] > 0                      # raw output still reported
    assert off["applied_complete"] is False            # fighters could not be applied


# --------------------------------------------------------------------------- #
# (h) EFT round-trip: fighters render as "Name xN" and re-import identically
# --------------------------------------------------------------------------- #
def test_eft_round_trip_fighters(ids):
    from apps.fitting import services
    archon, templar = ids["Archon"], ids["Templar II"]
    items = [{"type_id": templar, "slot": "fighter", "state": "active",
              "charge_type_id": None, "quantity": 6}]
    eft = services.export_eft(archon, items, fit_name="Carrier")
    assert "Templar II x6" in eft
    back = services.import_eft(eft)
    assert back["ship_type_id"] == archon
    fighter_items = [it for it in back["items"] if it["slot"] == "fighter"]
    assert len(fighter_items) == 1
    assert fighter_items[0]["type_id"] == templar
    assert fighter_items[0]["quantity"] == 6
    # And the loadout builder lifts it into FitInput.fighters (not a rack module).
    fit = services.fit_input_from_items(archon, back["items"])
    assert fit.fighters == (FighterInput(type_id=templar, count=6),)
    assert fit.modules == ()


def test_missing_fighter_skills_surface(ids):
    """A no-skills carrier fit surfaces the fighter's required skills (Fighters, Light
    Fighters, Amarr Fighter Specialization) as missing, so the pilot is told what to train."""
    archon, templar = ids["Archon"], ids["Templar II"]
    res = _eval(archon, fighters=[FighterInput(templar, 6)], skills=SkillProfile.from_dict({}))
    missing = {m.skill_type_id for m in res.missing_skills}
    assert ids["Fighters"] in missing
    assert ids["Light Fighters"] in missing
    assert ids["Amarr Fighter Specialization"] in missing
