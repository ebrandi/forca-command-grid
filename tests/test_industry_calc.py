"""Domain calculation services: invention, manufacturing estimate, chain, availability.

Uses the bundled SDE sample (see apps/sde/fixtures/sde_sample.json):
  - 600 "Test Cruiser" — T2, built by BP 601 from 700 (x10) + Tritanium (x100000),
    invented via T1 BP 599 -> T2 BPC 601, base prob 0.34, 10 runs/success,
    2x datacore 900. Invention skills 20424 + 11433.
  - 700 "Construction Component" — built from 800 (x5) + Pyerite (x1000).
  - 800 "Reacted Alloy" — reaction from Tritanium (x1000, batch of 200).
  - Decryptors 34201/34202/34203.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.industry import availability, calc, chain, invention

pytestmark = pytest.mark.django_db

CRUISER = 600
COMPONENT = 700
ALLOY = 800
TRIT = 34
PYERITE = 35
DATACORE = 900
ACCELERANT = 34201


# ============================================================================
# Invention
# ============================================================================
def test_invention_path_resolves(priced_sde):
    path = invention.invention_path(CRUISER)
    assert path is not None
    assert path.t1_blueprint_type_id == 599 and path.t2_blueprint_type_id == 601
    assert path.base_probability == pytest.approx(0.34)
    assert path.base_runs == 10
    assert {d.type_id: d.quantity for d in path.datacores} == {DATACORE: 2}
    assert path.datacore_cost == Decimal("200000")  # 2 x 100000
    assert {s["skill_type_id"] for s in path.skills} == {20424, 11433}


def test_invention_path_none_for_t1(priced_sde):
    assert invention.invention_path(587) is None  # Rifter is T1


def test_skill_and_probability_formula():
    # All level 5: 1 + (5+5)/30 + 5/40 = 1.4583...
    assert invention.skill_multiplier(5, 5, 5) == pytest.approx(1.4583333, rel=1e-5)
    assert invention.skill_multiplier(0, 0, 0) == 1.0
    p = invention.effective_probability(0.34, science_1=5, science_2=5, encryption=5)
    assert p == pytest.approx(0.34 * 1.4583333, rel=1e-4)
    # Cap at 1.0
    assert invention.effective_probability(0.9, science_1=5, science_2=5, encryption=5,
                                           decryptor_multiplier=1.9) == 1.0


def test_expected_attempts():
    assert invention.expected_attempts(0.5) == 2.0
    assert invention.expected_attempts(0.0) == float("inf")


def test_invention_plan_no_decryptor(priced_sde):
    p = invention.plan(CRUISER)
    assert p["inventable"] is True
    assert p["probability"] == pytest.approx(0.34)
    assert p["expected_attempts"] == pytest.approx(1 / 0.34, rel=1e-4)
    assert p["runs_per_success"] == 10
    assert p["resulting_me"] == 2 and p["resulting_te"] == 4
    assert p["datacore_cost"] == Decimal("200000")
    # cost per BPC = attempts x datacores; per run = /10
    assert p["cost_per_bpc"] == pytest.approx(Decimal("200000") * Decimal(str(round(1 / 0.34, 4))))
    assert p["cost_per_run"] == p["cost_per_bpc"] / 10
    assert p["decryptor"] is None


def test_invention_plan_with_decryptor(priced_sde):
    p = invention.plan(CRUISER, decryptor_type_id=ACCELERANT)
    assert p["decryptor"]["name"] == "Accelerant Decryptor"
    assert p["probability"] == pytest.approx(0.34 * 1.2)
    assert p["runs_per_success"] == 11          # 10 + 1
    assert p["resulting_me"] == 4 and p["resulting_te"] == 14  # 2+2, 4+10
    # per-attempt cost now includes the decryptor price (base 800000)
    assert p["cost_per_attempt"] == Decimal("200000") + Decimal("800000")


def test_invention_plan_none_for_t1(priced_sde):
    assert invention.plan(587) is None


# ============================================================================
# Manufacturing estimate
# ============================================================================
def test_manufacturing_estimate_costs_and_profit(priced_sde):
    est = calc.manufacturing_estimate(CRUISER, runs=1, me=0)
    assert est["buildable"] is True
    assert est["total_units"] == 1
    # Leaves after full build-vs-buy expansion: Trit 101000 + Pyerite 10000.
    leaves = {m["type_id"]: m["required"] for m in est["materials"]}
    assert leaves == {TRIT: 101000, PYERITE: 10000}
    # Jita material cost = 101000*5 + 10000*12 = 625000
    assert est["material_cost"] == Decimal("625000")
    # EIV = 700*10*500000-adj + 34*100000*5 = 5,500,000; fee = *(0.05+0.0025)
    assert est["eiv"] == Decimal("5500000")
    assert est["install_fee"] == Decimal("288750.00")
    assert est["total_cost"] == Decimal("913750.00")
    assert est["revenue_gross"] == Decimal("50000000")
    # net = 50m*0.94 - 913750
    assert est["net_profit"] == Decimal("47000000") - Decimal("913750.00")
    assert est["margin"] and est["margin"] > Decimal("0.9")
    assert est["break_even_price"] and est["break_even_price"] < Decimal("1000000")
    assert est["production_seconds"] == 6000


def test_manufacturing_estimate_not_buildable(priced_sde):
    est = calc.manufacturing_estimate(DATACORE)  # a raw material, no recipe
    assert est["buildable"] is False
    assert est["warnings"]


def test_manufacturing_estimate_missing_price_warns_not_crashes(sde):
    # No MarketPrice rows -> price_for returns 0; must warn, not crash.
    est = calc.manufacturing_estimate(CRUISER, runs=1)
    assert est["buildable"] is True
    assert est["material_cost"] == Decimal("0")
    assert any("Missing market price" in w for w in est["warnings"])


def test_manufacturing_estimate_nets_on_hand(priced_sde):
    est = calc.manufacturing_estimate(CRUISER, runs=1, on_hand={TRIT: 101000})
    trit = next(m for m in est["materials"] if m["type_id"] == TRIT)
    assert trit["available"] == 101000 and trit["to_buy"] == 0
    # Only Pyerite left to buy: 10000*12 = 120000
    assert est["material_cost"] == Decimal("120000")


def test_manufacturing_estimate_folds_invention_cost(priced_sde):
    est = calc.manufacturing_estimate(CRUISER, runs=1, invention_cost_per_unit=Decimal("58823.53"))
    assert est["invention_cost"] == Decimal("58823.53")
    assert est["total_cost"] == Decimal("625000") + Decimal("288750.00") + Decimal("58823.53")


def test_build_vs_buy(priced_sde):
    d = calc.build_vs_buy(CRUISER)
    assert d["buildable"] is True and d["decision"] == "build"
    assert d["build_cost"] < d["buy_cost"]


# ============================================================================
# Production-chain explorer
# ============================================================================
def test_chain_tree_structure(priced_sde):
    tree = chain.chain_tree(CRUISER, 1)
    assert tree["type_id"] == CRUISER and tree["name"] == "Test Cruiser"
    assert tree["decision"] == "build" and tree["activity"] == "manufacturing"
    kids = {c["type_id"]: c for c in tree["children"]}
    assert set(kids) == {COMPONENT, TRIT}
    # Construction Component is built; its children include the reaction alloy.
    comp = kids[COMPONENT]
    assert comp["decision"] == "build"
    alloy = next(c for c in comp["children"] if c["type_id"] == ALLOY)
    assert alloy["activity"] == "reaction"
    # Tritanium is a raw buy leaf.
    assert kids[TRIT]["decision"] == "buy" and kids[TRIT]["buildable"] is False


def test_chain_tree_respects_on_hand(priced_sde):
    tree = chain.chain_tree(CRUISER, 1, on_hand={TRIT: 5})
    assert next(c for c in tree["children"] if c["type_id"] == TRIT)["on_hand"] == 5


# ============================================================================
# Availability
# ============================================================================
def test_availability_maps(sde):
    from apps.stockpile.models import Asset, AssetLocation

    loc = AssetLocation.objects.create(location_id=60003760, name="Jita 4-4")
    Asset.objects.create(owner_type=Asset.Owner.CHARACTER, owner_id=1001, location=loc,
                         type_id=TRIT, quantity=5000)
    Asset.objects.create(owner_type=Asset.Owner.CHARACTER, owner_id=1001, location=loc,
                         type_id=PYERITE, quantity=200)
    Asset.objects.create(owner_type=Asset.Owner.CORPORATION, owner_id=2002, location=loc,
                         type_id=TRIT, quantity=100000)

    assert availability.character_on_hand(1001) == {TRIT: 5000, PYERITE: 200}
    assert availability.corp_on_hand(2002) == {TRIT: 100000}
    combined = availability.combined_on_hand(character_id=1001, corp_id=2002)
    assert combined[TRIT] == 105000 and combined[PYERITE] == 200
    # Filtered by type_ids
    assert availability.character_on_hand(1001, type_ids=[TRIT]) == {TRIT: 5000}
