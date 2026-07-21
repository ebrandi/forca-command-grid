"""Golden fits: WS-8 mining yield telemetry (engine v2, real SDE slices, hand-derived).

Every expected value is DERIVED IN THE TEST from the fixture slice's base attributes plus
the documented mining mechanic — never read back from the engine. Attribute ids are
verified against the SDE (scout-data §E) and asserted from the slice before they drive an
expectation, so a fixture drift fails loudly.

Mechanics proven here
---------------------
* **Per-cycle yield** is the evaluated miningAmount(77); **cycle** is the evaluated
  duration(73). Both flow through the dogma graph, so a hull's role/skill mining bonuses
  are already folded into the number the telemetry reports.
* **m³/hour** = yield_per_cycle / cycle_s × 3600.
* **Mining crystal** — a modulated strip miner's loaded crystal is an ItemModifier that
  pre-multiplies the module's miningAmount by the crystal's
  specializationAsteroidYieldMultiplier(782); the evaluated 77 is therefore the crystal-
  boosted yield (NOT a 789→77 preAssign — that was an early misread; §E).
* **Kind classification** is data-driven: gas = the miningClouds effect, ice = requires the
  Ice Harvesting skill, ore = every other mining laser, drone = a mining drone entity.
* **Residue (mining waste)** — miningWasteProbability(3154) / miningWastedVolumeMultiplier
  (3153) are surfaced per module when present.
* The industry section is ABSENT when the fit has no mining modules/drones (no empty noise).

Fixture: tests/fixtures/fitting/mining.json.
"""
from __future__ import annotations

import pytest

from apps.fitting.engine.types import ModuleInput, SkillProfile, SlotKind

from ._fitting_graph_utils import evaluate_fit, load_graph_fixture

pytestmark = pytest.mark.django_db

NO_SKILLS = SkillProfile.from_dict({})

ATTR_MINING_AMOUNT = 77
ATTR_DURATION = 73
ATTR_MINING_AMOUNT_MULTIPLIER = 207   # Venture role +100% ore yield (postMul)
ATTR_GAS_ROLE_YIELD = 3239            # Venture role +100% gas yield (postPercent)
ATTR_CRYSTAL_YIELD_MULT = 782         # crystal specializationAsteroidYieldMultiplier
ATTR_WASTE_PROBABILITY = 3154
ATTR_WASTE_VOLUME_MULT = 3153


@pytest.fixture()
def ids():
    return load_graph_fixture("mining")


def _attr(type_id, attr_id):
    from apps.sde.models import SdeTypeAttribute
    return SdeTypeAttribute.objects.get(type_id=type_id, attribute_id=attr_id).value


def approx(v):
    return pytest.approx(v, rel=2e-3, abs=0.1)


def _row(industry, type_id):
    return next(r for r in industry["modules"] if r["type_id"] == type_id)


# --------------------------------------------------------------------------- #
# (a) Venture + 2x Miner II — hull role yield bonus via the graph, ore m³/hr, totals
# --------------------------------------------------------------------------- #
def test_venture_miner_yield_and_totals(ids):
    """Venture role bonus is +100% ore yield (miningAmountMultiplier 207 = 2.0), applied to
    modules requiring Mining even untrained, so a bare Miner II yields base × 2 with NO
    skills; its per-level +5% (attr 1842) is 0 at level 0."""
    venture, miner2 = ids["Venture"], ids["Miner II"]
    base_yield = _attr(miner2, ATTR_MINING_AMOUNT)          # 15
    base_cycle_s = _attr(miner2, ATTR_DURATION) / 1000.0    # 15 s (Venture has no ore cycle bonus)
    role_mult = _attr(venture, ATTR_MINING_AMOUNT_MULTIPLIER)  # 2.0
    assert base_yield == 15.0 and role_mult == 2.0

    expected_yield = base_yield * role_mult                 # 30
    expected_per_hour = expected_yield / base_cycle_s * 3600.0  # 7200

    mods = [ModuleInput(type_id=miner2, slot=SlotKind.HIGH) for _ in range(2)]
    ind = evaluate_fit(venture, mods, skills=NO_SKILLS).telemetry["industry"]

    row = _row(ind, miner2)
    assert row["kind"] == "ore"
    assert row["yield_per_cycle"] == approx(expected_yield)
    assert row["cycle_s"] == approx(base_cycle_s)
    assert row["m3_per_hour"] == approx(expected_per_hour)
    # Residue is surfaced from the module's own attrs.
    assert row["waste_probability"] == approx(_attr(miner2, ATTR_WASTE_PROBABILITY))
    assert row["waste_volume_multiplier"] == approx(_attr(miner2, ATTR_WASTE_VOLUME_MULT))
    # Two identical lasers → the ore subtotal and grand total are 2× a single laser.
    assert ind["by_kind"]["ore"] == approx(2 * expected_per_hour)
    assert ind["m3_per_hour_total"] == approx(2 * expected_per_hour)


# --------------------------------------------------------------------------- #
# (b) Modulated strip miner + crystal — the crystal pre-multiplies miningAmount
# --------------------------------------------------------------------------- #
def test_modulated_strip_miner_crystal_premul(ids):
    """Covetor + Modulated Strip Miner II. A loaded Veldspar Mining Crystal I pre-multiplies
    the module's miningAmount by the crystal's specializationAsteroidYieldMultiplier(782), so
    the evaluated yield is base × 1.625 — proving the crystal chain flows through the graph."""
    covetor, mstrip = ids["Covetor"], ids["Modulated Strip Miner II"]
    crystal = ids["Veldspar Mining Crystal I"]
    base_yield = _attr(mstrip, ATTR_MINING_AMOUNT)          # 120
    crystal_mult = _attr(crystal, ATTR_CRYSTAL_YIELD_MULT)  # 1.625
    assert base_yield == 120.0 and crystal_mult == 1.625

    with_crystal = evaluate_fit(
        covetor, [ModuleInput(type_id=mstrip, slot=SlotKind.HIGH, charge_type_id=crystal)],
        skills=NO_SKILLS).telemetry["industry"]
    row = _row(with_crystal, mstrip)
    assert row["kind"] == "ore"
    assert row["yield_per_cycle"] == approx(base_yield * crystal_mult)     # 195
    # m³/hour is internally consistent with the reported (hull-cycle-bonused) cycle.
    assert row["m3_per_hour"] == approx(row["yield_per_cycle"] / row["cycle_s"] * 3600.0)

    # Without the crystal the same module yields only its base amount — proving it is the
    # crystal, not the hull, that raised the yield above 120.
    without = evaluate_fit(covetor, [ModuleInput(type_id=mstrip, slot=SlotKind.HIGH)],
                           skills=NO_SKILLS).telemetry["industry"]
    assert _row(without, mstrip)["yield_per_cycle"] == approx(base_yield)  # 120


# --------------------------------------------------------------------------- #
# (c) Ice harvester — ice classification + a long-cycle m³/hr
# --------------------------------------------------------------------------- #
def test_ice_harvester_kind_and_rate(ids):
    """Ice Harvester II is classified ice (it requires the Ice Harvesting skill, sharing the
    miningLaser effect with ore lasers). miningAmount 1000 is the m³ of one ice block per
    cycle; m³/hour is derived from it and the evaluated cycle."""
    covetor, ice = ids["Covetor"], ids["Ice Harvester II"]
    base_yield = _attr(ice, ATTR_MINING_AMOUNT)             # 1000
    assert base_yield == 1000.0

    ind = evaluate_fit(covetor, [ModuleInput(type_id=ice, slot=SlotKind.HIGH)],
                       skills=NO_SKILLS).telemetry["industry"]
    row = _row(ind, ice)
    assert row["kind"] == "ice"
    assert row["yield_per_cycle"] == approx(base_yield)     # no L0 yield bonus on ice
    assert row["m3_per_hour"] == approx(row["yield_per_cycle"] / row["cycle_s"] * 3600.0)
    assert ind["by_kind"]["ice"] == approx(row["m3_per_hour"])


# --------------------------------------------------------------------------- #
# (d) Gas cloud harvester — gas classification + role yield bonus
# --------------------------------------------------------------------------- #
def test_gas_harvester_kind_and_role_bonus(ids):
    """Gas Cloud Harvester II is classified gas (it carries the miningClouds effect). The
    Venture's +100% gas-yield role bonus (shipRoleBonusGasHarvestingYield 3239 = 100)
    applies untrained, so a bare harvester yields base × 2; its cycle is unbonused at L0."""
    venture, gas = ids["Venture"], ids["Gas Cloud Harvester II"]
    base_yield = _attr(gas, ATTR_MINING_AMOUNT)             # 100
    base_cycle_s = _attr(gas, ATTR_DURATION) / 1000.0       # 80 s
    role_pct = _attr(venture, ATTR_GAS_ROLE_YIELD)          # 100 (%)
    assert base_yield == 100.0 and role_pct == 100.0

    expected_yield = base_yield * (1.0 + role_pct / 100.0)  # 200
    ind = evaluate_fit(venture, [ModuleInput(type_id=gas, slot=SlotKind.HIGH)],
                       skills=NO_SKILLS).telemetry["industry"]
    row = _row(ind, gas)
    assert row["kind"] == "gas"
    assert row["yield_per_cycle"] == approx(expected_yield)
    assert row["cycle_s"] == approx(base_cycle_s)
    assert row["m3_per_hour"] == approx(expected_yield / base_cycle_s * 3600.0)  # 9000


# --------------------------------------------------------------------------- #
# (e) Mining drone — drone classification, yield scales with the drone count
# --------------------------------------------------------------------------- #
def test_mining_drone_yield(ids):
    """Vexor + 2× Mining Drone II. A Vexor has no mining bonus, so each drone yields its base
    miningAmount; the row's m³/hour scales with the fielded count."""
    vexor, drone = ids["Vexor"], ids["Mining Drone II"]
    base_yield = _attr(drone, ATTR_MINING_AMOUNT)           # 33
    cycle_s = _attr(drone, ATTR_DURATION) / 1000.0          # 60 s
    assert base_yield == 33.0

    ind = evaluate_fit(vexor, [ModuleInput(type_id=drone, slot=SlotKind.DRONE, quantity=2)],
                       skills=NO_SKILLS).telemetry["industry"]
    row = _row(ind, drone)
    assert row["kind"] == "drone"
    assert row["yield_per_cycle"] == approx(base_yield)
    per_drone_hour = base_yield / cycle_s * 3600.0          # 1980
    assert row["m3_per_hour"] == approx(2 * per_drone_hour)  # scaled by count 2
    assert ind["by_kind"]["drone"] == approx(2 * per_drone_hour)


# --------------------------------------------------------------------------- #
# (f) Multi-kind totals + absence when nothing mines
# --------------------------------------------------------------------------- #
def test_mixed_kinds_subtotals(ids):
    """Venture + Miner II (ore) + Gas Cloud Harvester II (gas): per-kind subtotals sum to the
    grand total, each kind independent."""
    venture, miner2, gas = ids["Venture"], ids["Miner II"], ids["Gas Cloud Harvester II"]
    ore_hour = (_attr(miner2, ATTR_MINING_AMOUNT) * _attr(venture, ATTR_MINING_AMOUNT_MULTIPLIER)
                / (_attr(miner2, ATTR_DURATION) / 1000.0) * 3600.0)          # 7200
    gas_hour = (_attr(gas, ATTR_MINING_AMOUNT) * (1.0 + _attr(venture, ATTR_GAS_ROLE_YIELD) / 100.0)
                / (_attr(gas, ATTR_DURATION) / 1000.0) * 3600.0)            # 9000

    ind = evaluate_fit(venture, [ModuleInput(type_id=miner2, slot=SlotKind.HIGH),
                                 ModuleInput(type_id=gas, slot=SlotKind.HIGH)],
                       skills=NO_SKILLS).telemetry["industry"]
    assert ind["by_kind"]["ore"] == approx(ore_hour)
    assert ind["by_kind"]["gas"] == approx(gas_hour)
    assert ind["m3_per_hour_total"] == approx(ore_hour + gas_hour)


def test_no_industry_section_without_mining(ids):
    """A combat fit (no mining module or drone) has no industry section at all."""
    covetor = ids["Covetor"]
    res = evaluate_fit(covetor, [], skills=NO_SKILLS)
    assert "industry" not in res.telemetry
