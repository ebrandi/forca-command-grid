"""Golden fits: tank mechanics (engine v2, real SDE slices, hand-derived numbers).

Fixtures: tests/fixtures/fitting/tank_shield.json (Caracal + shield modules) and
tank_armor.json (Rupture + armor/hull modules), both extracted from the live SDE import
through scripts/tochas_lab_extract_fixture.py. Every expected value is DERIVED IN THE
TEST from the slice's base attributes plus documented EVE mechanics:

* stacking S(i) = exp(-(i/2.67)^2), applied per (target attribute, operator) bucket,
  positive and negative chains separately, ONLY when the target attribute has
  stackable=false and the source is not ship/charge/skill/implant/subsystem;
* skill percentages read from the skill's own dogma bonus attribute x trained level
  (per-level scaling is CCP data: the skill pre-multiplies its bonus attr by attr 280);
* passive regen peak = 2.5 * shield / tau;  align = -ln(0.25) * mass * agility / 1e6.

Both hulls were chosen because their traits touch NO tank attribute (Caracal: missile
RoF/velocity only; Rupture: projectile damage/tracking only) — verified against the
ship types' dogma effect rows in the live DB.

Verified stackable flags from the slice (drive every penalty decision below):
  263 shieldCapacity=t, 265 armorHP=t, 9 hp=t, 479 shieldRechargeRate=t (also true in
  CCP's published data — no penalty on recharge chains), 552 signatureRadius=f,
  70 agility=f, 267-274 shield/armor resonances=f, 109-113 hull resonances=f.
"""
from __future__ import annotations

import math

import pytest

from apps.fitting.engine import attributes as A
from apps.fitting.engine.types import (
    DamageProfileInput,
    ModuleInput,
    ModuleState,
    OperatingProfile,
    SkillProfile,
    SlotKind,
)

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db

S1 = math.exp(-((1 / 2.67) ** 2))       # 0.8692 — 2nd module in a penalised chain
LN4 = -math.log(0.25)                   # align-time constant

# --- Skill type ids (category 16; percentages are read from THEIR dogma below) ------
SHIELD_MANAGEMENT = 3419        # +5%/lvl shield capacity  (own attr 337, x attr 280)
SHIELD_OPERATION = 3416         # -5%/lvl shield recharge time (own attr 338)
SHIELD_COMPENSATION = 21059     # -2%/lvl cap need of modules requiring Shield Operation
HULL_UPGRADES = 3394            # +5%/lvl armor HP (own attr 335)
MECHANICS = 3392                # +5%/lvl structure HP (own attr 327)
REPAIR_SYSTEMS = 3393           # -5%/lvl repair duration of modules requiring it (312)
ARMOR_COMPENSATION = {          # +5%/lvl to resist bonus of coatings/energized platings
    "em": 22806, "explosive": 22807, "kinetic": 22808, "thermal": 22809,
}                               # (LocationGroupModifier on groups 98 + 326; own attr 958)
SHIELD_RIGGING = 26261          # -10%/lvl rig drawback (attr 1138) on group 774 rigs

# --- Dogma attribute ids not named in apps.fitting.engine.attributes ---------------
ATTR_SHIELD_RECHARGE_MULT = 134   # shieldRechargeRateMultiplier (Shield Recharger)
ATTR_RECHARGE_RATE_BONUS = 338    # rechargeratebonus % (purger rig / Shield Operation)
ATTR_ARMOR_HP_BONUS_PCT = 335     # armorHpBonus % (trimark rig / Hull Upgrades)
ATTR_STRUCT_HP_BONUS_PCT = 327    # structureHpBonus % (Mechanics)
ATTR_REPAIR_DURATION_PCT = 312    # duration bonus % (Repair Systems)
ATTR_CAP_NEED_BONUS_PCT = 851     # capNeedBonus % (Shield Compensation)
ATTR_RESIST_SKILL_PCT = 958       # resistance skill bonus % (compensation skills)
ATTR_RIG_DRAWBACK = 1138          # rig drawback % (sig radius / agility)
ATTR_RIGGING_SKILL_PCT = 1139     # rigging skill drawback reduction %/lvl
ATTR_CARGO_MULT = 149             # cargoCapacityMultiplier (bulkheads)
ATTR_AGILITY_MULT_PCT = 169       # agilityMultiplier % (bulkheads, postPercent)


@pytest.fixture()
def shield_ids():
    return load_graph_fixture("tank_shield")


@pytest.fixture()
def armor_ids():
    return load_graph_fixture("tank_armor")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def _stackable(attr_id) -> bool:
    from apps.sde.models import SdeDogmaAttribute
    return SdeDogmaAttribute.objects.get(attribute_id=attr_id).stackable


def _mod(type_id, slot, state=ModuleState.ONLINE, charge=None):
    return ModuleInput(type_id=type_id, slot=slot, state=state, charge_type_id=charge)


NO_SKILLS = SkillProfile.from_dict({})


# =========================================================================== #
# Shield tank (Caracal)
# =========================================================================== #
def test_shield_buffer_extender_rig_and_skill(shield_ids):
    """LSE II flat HP + sig penalty, CDFE rig %HP + sig drawback, Shield Management.

    Order of application (CCP operator order): the extender's modAdd lands before the
    rig / skill postPercent multipliers. shieldCapacity (263) is stackable=true, so the
    rig % and skill % are plain multipliers (no penalty)."""
    ship = shield_ids["Caracal"]
    lse = shield_ids["Large Shield Extender II"]
    rig = shield_ids["Medium Core Defense Field Extender I"]
    mods = [_mod(lse, SlotKind.MED), _mod(rig, SlotKind.RIG)]

    assert _stackable(A.SHIELD_HP)
    base_hp = _attr(ship, A.SHIELD_HP)                       # 1700
    add = _attr(lse, A.SHIELD_EXTENDER_HP_BONUS)             # +2600 flat (attr 72)
    rig_pct = _attr(rig, A.SHIELD_RIG_HP_BONUS)              # +15% (attr 337)
    hp_untrained = (base_hp + add) * (1 + rig_pct / 100.0)

    res0 = evaluate_fit(ship, mods, skills=NO_SKILLS)
    assert res0.telemetry["defence"]["layers"]["shield"]["hp"] == pytest.approx(
        hp_untrained, rel=2e-3)

    # Shield Management: +5%/level shield capacity — the skill's OWN attr 337 (=5)
    # times level 5 (per-level scaling is the CCP premul of 337 by attr 280).
    sm_pct = _attr(SHIELD_MANAGEMENT, A.SHIELD_RIG_HP_BONUS) * 5     # 25
    res5 = evaluate_fit(ship, mods)                                   # omniscient
    assert res5.status.value in ("valid", "warnings")
    assert res5.telemetry["defence"]["layers"]["shield"]["hp"] == pytest.approx(
        hp_untrained * (1 + sm_pct / 100.0), rel=2e-3)

    # Signature: extender modAdd +25 (attr 983), then the rig drawback +10% postPercent
    # (attr 1138). 552 is stackable=false but the drawback is the only entry in its
    # chain, so index 0 -> full strength. At All V, Shield Rigging halves the drawback
    # (-10%/level on the rig's attr 1138, LocationGroupModifier on group 774).
    assert not _stackable(A.SIGNATURE_RADIUS)
    base_sig = _attr(ship, A.SIGNATURE_RADIUS) + _attr(lse, A.SIG_RADIUS_ADD)
    drawback = _attr(rig, ATTR_RIG_DRAWBACK)                              # 10
    rigging = _attr(SHIELD_RIGGING, ATTR_RIGGING_SKILL_PCT) * 5           # -50
    assert res0.telemetry["mobility"]["signature_radius"] == pytest.approx(
        base_sig * (1 + drawback / 100.0), rel=2e-3)
    assert res5.telemetry["mobility"]["signature_radius"] == pytest.approx(
        base_sig * (1 + drawback * (1 + rigging / 100.0) / 100.0), rel=2e-3)


def test_two_identical_shield_hardeners_penalised(shield_ids):
    """Two active EM Shield Hardener II: both postPercent -55% on shieldEmDamageResonance
    (271, stackable=false, module source) -> second entry takes S1 = exp(-(1/2.67)^2)."""
    ship = shield_ids["Caracal"]
    hard = shield_ids["EM Shield Hardener II"]
    mods = [_mod(hard, SlotKind.MED, ModuleState.ACTIVE) for _ in range(2)]
    res = evaluate_fit(ship, mods, skills=NO_SKILLS)

    assert not _stackable(A.SHIELD_RESONANCE["em"])
    b = _attr(hard, A.SHIELD_RESIST_BONUS["em"]) / 100.0     # -0.55 (attr 984)
    base_em = _attr(ship, A.SHIELD_RESONANCE["em"])          # 1.0 (Caldari: 0% EM)
    expected_em = base_em * (1 + b) * (1 + b * S1)
    resists = res.telemetry["defence"]["layers"]["shield"]["resists"]
    assert resists["em"] == pytest.approx((1 - expected_em) * 100, abs=0.2)
    # The hardener's thermal/kinetic/explosive bonus attrs are 0 -> those resonances
    # stay at hull base.
    for d in ("thermal", "kinetic", "explosive"):
        assert _attr(hard, A.SHIELD_RESIST_BONUS[d]) == 0
        base = _attr(ship, A.SHIELD_RESONANCE[d])
        assert resists[d] == pytest.approx((1 - base) * 100, abs=0.2)


def test_multispectrum_shield_hardener_online_vs_active(shield_ids):
    """The hardening effect (modifyActiveShieldResonancePostPercent, category 1=active)
    only applies when the module is ACTIVE; merely online it contributes nothing."""
    ship = shield_ids["Caracal"]
    hard = shield_ids["Multispectrum Shield Hardener II"]

    online = evaluate_fit(ship, [_mod(hard, SlotKind.MED, ModuleState.ONLINE)],
                          skills=NO_SKILLS)
    active = evaluate_fit(ship, [_mod(hard, SlotKind.MED, ModuleState.ACTIVE)],
                          skills=NO_SKILLS)
    r_on = online.telemetry["defence"]["layers"]["shield"]["resists"]
    r_act = active.telemetry["defence"]["layers"]["shield"]["resists"]
    for d in A.DAMAGE_TYPES:
        base = _attr(ship, A.SHIELD_RESONANCE[d])
        bonus = _attr(hard, A.SHIELD_RESIST_BONUS[d]) / 100.0      # -0.325 each
        assert r_on[d] == pytest.approx((1 - base) * 100, abs=0.2)
        assert r_act[d] == pytest.approx((1 - base * (1 + bonus)) * 100, abs=0.2)


def test_passive_regen_recharger_and_purger(shield_ids):
    """Peak passive regen = 2.5 * shield / tau. tau chain: Shield Recharger II postMul
    x0.85 (attr 134), purger rig postPercent -20% (attr 338), Shield Operation -5%/lvl.
    shieldRechargeRate (479) is stackable=TRUE in the CCP data -> NO stacking penalty
    anywhere in this chain (verified from the slice's dogma attribute row)."""
    ship = shield_ids["Caracal"]
    rech = shield_ids["Shield Recharger II"]
    purger = shield_ids["Medium Core Defense Field Purger I"]
    res = evaluate_fit(ship, [_mod(rech, SlotKind.MED), _mod(purger, SlotKind.RIG)])

    assert _stackable(A.SHIELD_RECHARGE_RATE)
    so_pct = _attr(SHIELD_OPERATION, ATTR_RECHARGE_RATE_BONUS) * 5    # -25 (%/lvl x5)
    tau_ms = (_attr(ship, A.SHIELD_RECHARGE_RATE)
              * _attr(rech, ATTR_SHIELD_RECHARGE_MULT)                # x0.85
              * (1 + _attr(purger, ATTR_RECHARGE_RATE_BONUS) / 100.0)  # x0.80
              * (1 + so_pct / 100.0))                                 # x0.75
    sm_pct = _attr(SHIELD_MANAGEMENT, A.SHIELD_RIG_HP_BONUS) * 5      # +25
    shield_hp = _attr(ship, A.SHIELD_HP) * (1 + sm_pct / 100.0)

    regen = res.telemetry["defence"]["passive_shield_regen"]
    assert regen["recharge_time_s"] == pytest.approx(tau_ms / 1000.0, rel=2e-3)
    assert regen["peak_hps"] == pytest.approx(
        2.5 * shield_hp / (tau_ms / 1000.0), abs=0.06)
    # Purger drawback: +10% sig, halved by Shield Rigging V (-10%/level on attr 1138),
    # single entry in the penalised chain -> full strength of the reduced drawback.
    rigging = _attr(SHIELD_RIGGING, ATTR_RIGGING_SKILL_PCT) * 5           # -50
    assert res.telemetry["mobility"]["signature_radius"] == pytest.approx(
        _attr(ship, A.SIGNATURE_RADIUS)
        * (1 + _attr(purger, ATTR_RIG_DRAWBACK) * (1 + rigging / 100.0) / 100.0),
        rel=2e-3)


def test_medium_shield_booster_rate(shield_ids):
    """Active tank HP/s = shieldBonus / cycle. No skill in the data modifies a shield
    booster's boost amount (68) or duration (73); Shield Compensation reduces its CAP
    NEED by 2%/level (LocationRequiredSkillModifier keyed on Shield Operation 3416)."""
    ship = shield_ids["Caracal"]
    msb = shield_ids["Medium Shield Booster II"]
    res = evaluate_fit(ship, [_mod(msb, SlotKind.MED, ModuleState.ACTIVE)])
    assert res.status.value in ("valid", "warnings")

    boost = _attr(msb, A.SHIELD_BOOST_AMOUNT)                # 104 HP / cycle
    cycle_s = _attr(msb, A.CYCLE_TIME) / 1000.0              # 3.0 s
    assert res.telemetry["defence"]["active_tank"]["shield_hps"] == pytest.approx(
        boost / cycle_s, abs=0.06)

    sc_pct = _attr(SHIELD_COMPENSATION, ATTR_CAP_NEED_BONUS_PCT) * 5   # -10
    cap_need = _attr(msb, A.CAP_NEED) * (1 + sc_pct / 100.0)           # 54 GJ
    assert res.telemetry["capacitor"]["usage"] == pytest.approx(
        cap_need / cycle_s, rel=2e-3)


def test_ancillary_shield_booster_with_navy_charge(shield_ids):
    """Medium ASB + Navy Cap Booster 100 (chargeSize 1 = the module's size; the charge
    even declares launcherGroup2 = 1156, the ASB group). The loaded charge's
    ammoInfluenceCapNeed postPercent (-100%, attr 317) zeroes the module's cap need;
    boost amount and cycle are unmodified by any skill."""
    ship = shield_ids["Caracal"]
    asb = shield_ids["Medium Ancillary Shield Booster"]
    charge = shield_ids["Navy Cap Booster 100"]
    res = evaluate_fit(
        ship, [_mod(asb, SlotKind.MED, ModuleState.ACTIVE, charge=charge)])
    assert res.status.value in ("valid", "warnings")
    assert not any(d.code in ("incompatible_charge", "charge_size_mismatch")
                   for d in res.diagnostics)

    boost = _attr(asb, A.SHIELD_BOOST_AMOUNT)                # 146 HP / cycle
    cycle_s = _attr(asb, A.CYCLE_TIME) / 1000.0              # 3.0 s
    assert res.telemetry["defence"]["active_tank"]["shield_hps"] == pytest.approx(
        boost / cycle_s, abs=0.06)
    # 198 GJ base need x (1 - 10% Shield Compensation V) x (1 - 100% charge) = 0.
    assert _attr(charge, 317) == -100
    assert res.telemetry["capacitor"]["usage"] == pytest.approx(0.0, abs=1e-6)


def test_ehp_uniform_and_custom_damage_profiles(shield_ids):
    """EHP per layer = HP / sum(profile_d * resonance_d). Fit: LSE II (flat shield HP)
    + one active EM Shield Hardener II, no skills — every layer hand-computable."""
    ship = shield_ids["Caracal"]
    lse = shield_ids["Large Shield Extender II"]
    hard = shield_ids["EM Shield Hardener II"]
    mods = [_mod(lse, SlotKind.MED), _mod(hard, SlotKind.MED, ModuleState.ACTIVE)]

    shield_hp = _attr(ship, A.SHIELD_HP) + _attr(lse, A.SHIELD_EXTENDER_HP_BONUS)
    s_res = {d: _attr(ship, A.SHIELD_RESONANCE[d]) for d in A.DAMAGE_TYPES}
    s_res["em"] *= 1 + _attr(hard, A.SHIELD_RESIST_BONUS["em"]) / 100.0   # x0.45
    a_hp = _attr(ship, A.ARMOR_HP)
    a_res = {d: _attr(ship, A.ARMOR_RESONANCE[d]) for d in A.DAMAGE_TYPES}
    h_hp = _attr(ship, A.HULL_HP)
    h_res = {d: _attr(ship, A.HULL_RESONANCE[d]) for d in A.DAMAGE_TYPES}

    # Uniform 25/25/25/25 profile.
    uni = evaluate_fit(ship, mods, skills=NO_SKILLS)
    layers = uni.telemetry["defence"]["layers"]
    for name, hp, resmap in (("shield", shield_hp, s_res), ("armor", a_hp, a_res),
                             ("hull", h_hp, h_res)):
        expected = hp / (0.25 * sum(resmap.values()))
        assert layers[name]["ehp"] == pytest.approx(expected, rel=2e-3)

    # Custom 100% EM profile: each layer divides by its EM resonance alone.
    em_only = OperatingProfile(
        propulsion_active=False,
        damage_profile=DamageProfileInput(em=1.0, thermal=0.0, kinetic=0.0,
                                          explosive=0.0))
    custom = evaluate_fit(ship, mods, skills=NO_SKILLS, op=em_only)
    layers = custom.telemetry["defence"]["layers"]
    assert layers["shield"]["ehp"] == pytest.approx(shield_hp / s_res["em"], rel=2e-3)
    total = shield_hp / s_res["em"] + a_hp / a_res["em"] + h_hp / h_res["em"]
    assert custom.telemetry["defence"]["ehp_total"] == pytest.approx(total, rel=2e-3)


# =========================================================================== #
# Armor / hull tank (Rupture)
# =========================================================================== #
def test_armor_buffer_plate_trimark_and_hull_upgrades(armor_ids):
    """800mm plate modAdd +2400 armor HP, trimark +15% postPercent, Hull Upgrades
    +5%/lvl — armorHP (265) is stackable=true so both percentages are plain."""
    ship = armor_ids["Rupture"]
    plate = armor_ids["800mm Steel Plates II"]
    trimark = armor_ids["Medium Trimark Armor Pump I"]
    mods = [_mod(plate, SlotKind.LOW), _mod(trimark, SlotKind.RIG)]

    assert _stackable(A.ARMOR_HP)
    base = _attr(ship, A.ARMOR_HP)                            # 1800
    add = _attr(plate, A.ARMOR_PLATE_HP_BONUS)                # +2400 (attr 1159)
    tri_pct = _attr(trimark, ATTR_ARMOR_HP_BONUS_PCT)         # +15 (attr 335)
    hu_pct = _attr(HULL_UPGRADES, ATTR_ARMOR_HP_BONUS_PCT) * 5  # +25 (5%/lvl x5)
    res5 = evaluate_fit(ship, mods)                           # omniscient
    assert res5.status.value in ("valid", "warnings")
    assert res5.telemetry["defence"]["layers"]["armor"]["hp"] == pytest.approx(
        (base + add) * (1 + tri_pct / 100.0) * (1 + hu_pct / 100.0), rel=2e-3)


def test_plate_mass_raises_align_time(armor_ids):
    """The plate's massAddition (attr 796) is a modAdd on ship mass; trimark drawback
    is +10% agility (attr 1138 postPercent; 70 stackable=false but single entry).
    align = -ln(0.25) * mass * agility / 1e6. No skills -> agility skills at level 0."""
    ship = armor_ids["Rupture"]
    plate = armor_ids["800mm Steel Plates II"]
    trimark = armor_ids["Medium Trimark Armor Pump I"]

    base_mass = _attr(ship, A.MASS)
    agility = _attr(ship, A.AGILITY) * (1 + _attr(trimark, ATTR_RIG_DRAWBACK) / 100.0)

    without = evaluate_fit(ship, [_mod(trimark, SlotKind.RIG)], skills=NO_SKILLS)
    with_plate = evaluate_fit(
        ship, [_mod(plate, SlotKind.LOW), _mod(trimark, SlotKind.RIG)],
        skills=NO_SKILLS)

    align_bare = LN4 * base_mass * agility / 1e6
    mass_plated = base_mass + _attr(plate, A.MASS_ADDITION)   # +1,450,000 kg
    align_plated = LN4 * mass_plated * agility / 1e6

    assert without.telemetry["mobility"]["align_time_s"] == pytest.approx(
        align_bare, rel=2e-3)
    assert with_plate.telemetry["mobility"]["mass"] == pytest.approx(mass_plated, rel=1e-6)
    assert with_plate.telemetry["mobility"]["align_time_s"] == pytest.approx(
        align_plated, rel=2e-3)
    assert align_plated > align_bare


def test_hull_tank_dcu_and_bulkheads(armor_ids):
    """Reinforced Bulkheads II: postMul x1.25 structure HP (attr 150), x0.89 cargo,
    +5% agility. DCU II online: hull resonances pre-multiplied by its OWN hull attrs
    974-977 (=0.6). structure hp (9) is stackable=true -> plain multipliers."""
    ship = armor_ids["Rupture"]
    dcu = armor_ids["Damage Control II"]
    bulk = armor_ids["Reinforced Bulkheads II"]
    mods = [_mod(dcu, SlotKind.LOW), _mod(bulk, SlotKind.LOW)]
    res = evaluate_fit(ship, mods, skills=NO_SKILLS)

    assert _stackable(A.HULL_HP)
    hull_hp = _attr(ship, A.HULL_HP) * _attr(bulk, A.STRUCTURE_HP_MULTIPLIER)  # x1.25
    t = res.telemetry
    assert t["defence"]["layers"]["hull"]["hp"] == pytest.approx(hull_hp, rel=2e-3)
    for d in A.DAMAGE_TYPES:
        expected = _attr(ship, A.HULL_RESONANCE[d]) \
            * _attr(dcu, A.HULL_RESONANCE_MODULE[d])          # 0.67 * 0.6
        assert t["defence"]["layers"]["hull"]["resists"][d] == pytest.approx(
            (1 - expected) * 100, abs=0.2)
    assert t["utility"]["cargo"] == pytest.approx(
        _attr(ship, A.CAPACITY_CARGO) * _attr(bulk, ATTR_CARGO_MULT), rel=2e-3)
    assert t["mobility"]["agility"] == pytest.approx(
        _attr(ship, A.AGILITY) * (1 + _attr(bulk, ATTR_AGILITY_MULT_PCT) / 100.0),
        rel=2e-3)

    # Mechanics: +5%/level structure HP (skill attr 327 x level; skill source exempt).
    mech_pct = _attr(MECHANICS, ATTR_STRUCT_HP_BONUS_PCT) * 5     # +25
    res5 = evaluate_fit(ship, mods)
    assert res5.telemetry["defence"]["layers"]["hull"]["hp"] == pytest.approx(
        hull_hp * (1 + mech_pct / 100.0), rel=2e-3)


def test_armor_hardener_online_vs_active(armor_ids):
    """EM Armor Hardener II's resist effect (modifyActiveArmorResonancePostPercent) is
    category 1 = active-only: online it gives the base resist, active -55% EM."""
    ship = armor_ids["Rupture"]
    hard = armor_ids["EM Armor Hardener II"]
    online = evaluate_fit(ship, [_mod(hard, SlotKind.LOW, ModuleState.ONLINE)],
                          skills=NO_SKILLS)
    active = evaluate_fit(ship, [_mod(hard, SlotKind.LOW, ModuleState.ACTIVE)],
                          skills=NO_SKILLS)
    base_em = _attr(ship, A.ARMOR_RESONANCE["em"])            # 0.4
    bonus = _attr(hard, A.SHIELD_RESIST_BONUS["em"]) / 100.0  # -0.55 (attr 984)
    r_on = online.telemetry["defence"]["layers"]["armor"]["resists"]
    r_act = active.telemetry["defence"]["layers"]["armor"]["resists"]
    assert r_on["em"] == pytest.approx((1 - base_em) * 100, abs=0.2)
    assert r_act["em"] == pytest.approx((1 - base_em * (1 + bonus)) * 100, abs=0.2)
    # Non-bonused damage types unchanged in both states.
    for d in ("thermal", "kinetic", "explosive"):
        base = _attr(ship, A.ARMOR_RESONANCE[d])
        assert r_act[d] == pytest.approx((1 - base) * 100, abs=0.2)


def test_dcu_bucket_vs_hardener_and_membrane_chain(armor_ids):
    """DCU exemption is NOT by category (DCU is a category-7 module like the others):
    its armor resonance modifier uses the preMul OPERATOR (op 0, its own attrs
    267-270 = 0.85), while hardener/membrane use postPercent (op 6). Penalty chains
    form per (target attribute, operator) bucket, so the DCU sits alone in the preMul
    bucket at full strength, and the hardener (-55) + membrane (-20) share the
    postPercent bucket: strongest first, membrane takes S1."""
    ship = armor_ids["Rupture"]
    dcu = armor_ids["Damage Control II"]
    hard = armor_ids["EM Armor Hardener II"]
    memb = armor_ids["Multispectrum Energized Membrane II"]
    res = evaluate_fit(
        ship, [_mod(dcu, SlotKind.LOW), _mod(memb, SlotKind.LOW),
               _mod(hard, SlotKind.LOW, ModuleState.ACTIVE)],
        skills=NO_SKILLS)
    resists = res.telemetry["defence"]["layers"]["armor"]["resists"]

    assert not _stackable(A.ARMOR_RESONANCE["em"])
    dcu_em = _attr(dcu, A.ARMOR_RESONANCE["em"])              # 0.85 (module's own 267)
    h = _attr(hard, A.SHIELD_RESIST_BONUS["em"]) / 100.0      # -0.55
    m = _attr(memb, A.SHIELD_RESIST_BONUS["em"]) / 100.0      # -0.20
    expected_em = _attr(ship, A.ARMOR_RESONANCE["em"]) \
        * dcu_em * (1 + h) * (1 + m * S1)
    assert resists["em"] == pytest.approx((1 - expected_em) * 100, abs=0.2)

    # Thermal/kinetic/explosive: the hardener's bonus attr is 0 there, so the membrane
    # is alone in the postPercent chain (index 0 -> full -20%), DCU preMul x0.85.
    for d in ("thermal", "kinetic", "explosive"):
        expected = _attr(ship, A.ARMOR_RESONANCE[d]) \
            * _attr(dcu, A.ARMOR_RESONANCE[d]) \
            * (1 + _attr(memb, A.SHIELD_RESIST_BONUS[d]) / 100.0)
        assert resists[d] == pytest.approx((1 - expected) * 100, abs=0.2)


def test_medium_armor_repairer_rate(armor_ids):
    """Armor rep HP/s = armorDamageAmount / cycle; Repair Systems shortens the duration
    by 5%/level (LocationRequiredSkillModifier on modules requiring skill 3393). No
    skill modifies the repair AMOUNT (attr 84)."""
    ship = armor_ids["Rupture"]
    mar = armor_ids["Medium Armor Repairer II"]
    res = evaluate_fit(ship, [_mod(mar, SlotKind.LOW, ModuleState.ACTIVE)])
    assert res.status.value in ("valid", "warnings")

    rs_pct = _attr(REPAIR_SYSTEMS, ATTR_REPAIR_DURATION_PCT) * 5   # -25
    cycle_s = _attr(mar, A.CYCLE_TIME) * (1 + rs_pct / 100.0) / 1000.0   # 9.0 s
    amount = _attr(mar, A.ARMOR_REPAIR_AMOUNT)                     # 368 HP
    assert res.telemetry["defence"]["active_tank"]["armor_hps"] == pytest.approx(
        amount / cycle_s, abs=0.06)
    assert res.telemetry["capacitor"]["usage"] == pytest.approx(
        _attr(mar, A.CAP_NEED) / cycle_s, rel=2e-3)


def test_realistic_armor_rupture_all_v(armor_ids):
    """Realistic plated Rupture, All V: DCU + MAR II + 800mm + membrane + EM hardener
    + trimark. Exercises buffer maths, per-operator resist buckets, the armor
    compensation skills boosting the MEMBRANE's own resist attrs (+5%/lvl, group-326
    LocationGroupModifier), rep rate, and the three-layer EHP total."""
    ship = armor_ids["Rupture"]
    dcu = armor_ids["Damage Control II"]
    mar = armor_ids["Medium Armor Repairer II"]
    plate = armor_ids["800mm Steel Plates II"]
    memb = armor_ids["Multispectrum Energized Membrane II"]
    hard = armor_ids["EM Armor Hardener II"]
    trimark = armor_ids["Medium Trimark Armor Pump I"]
    res = evaluate_fit(ship, [
        _mod(dcu, SlotKind.LOW),
        _mod(mar, SlotKind.LOW, ModuleState.ACTIVE),
        _mod(plate, SlotKind.LOW),
        _mod(memb, SlotKind.LOW),
        _mod(hard, SlotKind.LOW, ModuleState.ACTIVE),
        _mod(trimark, SlotKind.RIG),
    ])
    assert res.status.value in ("valid", "warnings")
    t = res.telemetry["defence"]

    # Armor buffer: (1800 + 2400) x 1.15 trimark x 1.25 Hull Upgrades V.
    hu_pct = _attr(HULL_UPGRADES, ATTR_ARMOR_HP_BONUS_PCT) * 5
    armor_hp = (_attr(ship, A.ARMOR_HP) + _attr(plate, A.ARMOR_PLATE_HP_BONUS)) \
        * (1 + _attr(trimark, ATTR_ARMOR_HP_BONUS_PCT) / 100.0) * (1 + hu_pct / 100.0)
    assert t["layers"]["armor"]["hp"] == pytest.approx(armor_hp, rel=2e-3)

    # Armor resonances. Membrane bonus at All V: -20% x (1 + 25%) = -25% per type
    # (each Armor Compensation skill's attr 958 = 5%/lvl on groups 98/326).
    a_res = {}
    for d in A.DAMAGE_TYPES:
        comp_pct = _attr(ARMOR_COMPENSATION[d], ATTR_RESIST_SKILL_PCT) * 5   # +25
        m = _attr(memb, A.SHIELD_RESIST_BONUS[d]) / 100.0 * (1 + comp_pct / 100.0)
        h = _attr(hard, A.SHIELD_RESIST_BONUS[d]) / 100.0
        # postPercent bucket: sort by |magnitude| (h=-0.55 > m=-0.25 for EM; the other
        # types have h=0, leaving the membrane alone at index 0).
        chain = sorted([v for v in (h, m) if v != 0], key=abs, reverse=True)
        factor = 1.0
        for i, v in enumerate(chain):
            factor *= 1 + v * (S1 ** (i * i))
        a_res[d] = _attr(ship, A.ARMOR_RESONANCE[d]) \
            * _attr(dcu, A.ARMOR_RESONANCE[d]) * factor      # DCU preMul bucket: full
        assert t["layers"]["armor"]["resists"][d] == pytest.approx(
            (1 - a_res[d]) * 100, abs=0.2)

    # Rep rate (Repair Systems V) as in the isolation test.
    rs_pct = _attr(REPAIR_SYSTEMS, ATTR_REPAIR_DURATION_PCT) * 5
    cycle_s = _attr(mar, A.CYCLE_TIME) * (1 + rs_pct / 100.0) / 1000.0
    assert t["active_tank"]["armor_hps"] == pytest.approx(
        _attr(mar, A.ARMOR_REPAIR_AMOUNT) / cycle_s, abs=0.06)

    # Three-layer uniform EHP. Shield: base x1.25 (Shield Management V), resonances
    # x DCU shield preMul (0.875). Hull: base x1.25 (Mechanics V), x DCU hull attrs.
    sm_pct = _attr(SHIELD_MANAGEMENT, A.SHIELD_RIG_HP_BONUS) * 5
    shield_hp = _attr(ship, A.SHIELD_HP) * (1 + sm_pct / 100.0)
    shield_w = 0.25 * sum(
        _attr(ship, A.SHIELD_RESONANCE[d]) * _attr(dcu, A.SHIELD_RESONANCE[d])
        for d in A.DAMAGE_TYPES)
    mech_pct = _attr(MECHANICS, ATTR_STRUCT_HP_BONUS_PCT) * 5
    hull_hp = _attr(ship, A.HULL_HP) * (1 + mech_pct / 100.0)
    hull_w = 0.25 * sum(
        _attr(ship, A.HULL_RESONANCE[d]) * _attr(dcu, A.HULL_RESONANCE_MODULE[d])
        for d in A.DAMAGE_TYPES)
    armor_w = 0.25 * sum(a_res.values())
    ehp_total = shield_hp / shield_w + armor_hp / armor_w + hull_hp / hull_w
    assert t["ehp_total"] == pytest.approx(ehp_total, rel=2e-3)
