"""Industry BOM / build-vs-buy and stockpile reservation tests."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.industry import bom
from apps.industry.models import IndustryProject, IndustryProjectItem
from apps.industry.services import (
    compute_project_bom,
    detect_bottlenecks,
    generate_shopping_list,
)
from apps.stockpile.models import Stockpile, StockReservation
from apps.stockpile.services import (
    available_quantity,
    consume_reservation,
    record_manual_stock,
    reserve_for_project,
)

# Rifter (587) built from Tritanium(34 x32000) + Pyerite(35 x6000); base prices
# 587=380000, 34=5, 35=12 -> build 232000 < buy 380000.


@pytest.mark.django_db
def test_direct_materials_and_me(sde):
    mats = bom.direct_materials(587, runs=1, me=0)
    assert mats == {34: 32000, 35: 6000}
    # 10% ME reduces material quantities.
    mats_me = bom.direct_materials(587, runs=1, me=10)
    assert mats_me[34] == 28800  # ceil(32000*0.9)


@pytest.mark.django_db
def test_build_vs_buy_decision(priced_sde):
    d = bom.decide_build_or_buy(587, quantity=1)
    assert d["decision"] == "build"
    assert d["build_cost"] == Decimal("232000")
    assert d["buy_cost"] == Decimal("380000")
    # Non-buildable item (no blueprint) -> buy.
    d2 = bom.decide_build_or_buy(192, quantity=10)
    assert d2["decision"] == "buy"
    assert d2["buildable"] is False


@pytest.mark.django_db
def test_compute_project_bom_and_shopping_list(priced_sde):
    project = IndustryProject.objects.create(name="Build 2 Rifters")
    IndustryProjectItem.objects.create(
        project=project, type_id=587, quantity=2, build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD
    )
    summary = compute_project_bom(project)
    # 64000 Trit * 5 + 12000 Pye * 12 = 320000 + 144000
    assert summary["estimated_cost"] == Decimal("464000")

    sl = generate_shopping_list(project)
    assert sl.items.count() == 2
    quantities = {i.type_id: i.quantity for i in sl.items.all()}
    assert quantities == {34: 64000, 35: 12000}

    bottlenecks = detect_bottlenecks(project)
    assert bottlenecks[0]["type_id"] == 34  # most expensive to acquire


@pytest.mark.django_db
def test_stock_nets_off_requirements(priced_sde):
    stock = Stockpile.objects.create(name="Staging")
    record_manual_stock(stock, type_id=34, quantity_current=50000)
    project = IndustryProject.objects.create(name="Build 1 Rifter")
    IndustryProjectItem.objects.create(
        project=project, type_id=587, quantity=1, build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD
    )
    compute_project_bom(project)
    trit_req = project.items.first().material_requirements.get(type_id=34)
    assert trit_req.quantity_required == 32000
    # P1: quantity_available is what the shared pool ALLOCATED to this line
    # (capped at its requirement), no longer the corp-wide free total.
    assert trit_req.quantity_available == 32000
    assert trit_req.quantity_to_acquire == 0  # fully covered by stock


@pytest.mark.django_db
def test_industry_and_stock_views_require_member(client, django_user_model):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    # Anonymous is redirected to login.
    assert client.get("/industry/").status_code == 302
    assert client.get("/stockpile/").status_code == 302

    member = django_user_model.objects.create(username="m1")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get("/industry/").status_code == 200
    assert client.get("/stockpile/").status_code == 200
    assert client.get("/stockpile/logistics/").status_code == 200


# --- Recursive multi-level BOM (craft anything) -------------------------------
# Test Cruiser(600) <- 10x Component(700) + 100000x Trit(34)
# Component(700)     <- 5x Reacted Alloy(800) + 1000x Pyerite(35)
# Reacted Alloy(800) <- REACTION 1000x Trit(34), yields 200 per run.


@pytest.mark.django_db
def test_expand_to_minerals_recurses_and_reacts(sde):
    result = bom.expand(600, 1, strategy=bom.STRATEGY_BUILD_TO_MINERALS)
    # 34 comes from two depths: 100000 (hull) + 1000 (one reaction run) = 101000.
    assert result.leaves == {34: 101000, 35: 10000}
    built = {s.type_id for s in result.steps}
    assert built == {600, 700, 800}
    react = next(s for s in result.steps if s.type_id == 800)
    assert react.activity == "reaction"
    # Need 5x alloy per component x10 components = 50; one run yields 200.
    assert react.runs == 1
    assert react.produced == 200
    # Build order: reaction is deepest.
    assert result.steps[0].type_id == 800


@pytest.mark.django_db
def test_expand_reaction_batches_multiple_runs(sde):
    # 5 cruisers need 250 alloy -> ceil(250/200) = 2 reaction runs (consumes 2000 Trit).
    result = bom.expand(600, 5, strategy=bom.STRATEGY_BUILD_TO_MINERALS)
    react = next(s for s in result.steps if s.type_id == 800)
    assert react.runs == 2
    assert result.leaves[34] == 500000 + 2000  # 5x hull trit + 2 reaction runs


@pytest.mark.django_db
def test_expand_cycle_guard(sde):
    from apps.sde.models import SdeBlueprintMaterial

    # Introduce an impossible A<-B, B<-A cycle; expansion must terminate.
    SdeBlueprintMaterial.objects.create(
        blueprint_type_id=9001, product_type_id=34, material_type_id=35, quantity=1,
        output_quantity=1, activity="manufacturing",
    )
    SdeBlueprintMaterial.objects.create(
        blueprint_type_id=9002, product_type_id=35, material_type_id=34, quantity=1,
        output_quantity=1, activity="manufacturing",
    )
    result = bom.expand(34, 1, strategy=bom.STRATEGY_BUILD_TO_MINERALS, max_depth=20)
    # Terminates (cycle collapses to a buy leaf) rather than recursing forever.
    assert result.leaves  # has some raw leaf


@pytest.mark.django_db
def test_recursive_project_bom_records_steps(sde):
    project = IndustryProject.objects.create(name="Build a Cruiser")
    IndustryProjectItem.objects.create(
        project=project, type_id=600, quantity=1,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD,
        strategy=IndustryProjectItem.Strategy.BUILD_TO_MINERALS,
    )
    compute_project_bom(project)
    item = project.items.first()
    from apps.industry.models import MaterialRequirement

    leaves = {
        r.type_id: r.quantity_required
        for r in item.material_requirements.exclude(
            acquire_method=MaterialRequirement.AcquireMethod.INVENT
        )
    }
    assert leaves == {34: 101000, 35: 10000}
    # Three intermediate jobs recorded, deepest first.
    steps = list(item.production_steps.all())
    assert [s.type_id for s in steps] == [800, 700, 600]


@pytest.mark.django_db
def test_invention_datacores_costed_and_profit(priced_sde):
    from apps.industry.models import MaterialRequirement
    from apps.industry.services import project_economics

    project = IndustryProject.objects.create(name="Build a Cruiser (T2)")
    IndustryProjectItem.objects.create(
        project=project, type_id=600, quantity=1,
        build_or_buy=IndustryProjectItem.BuildOrBuy.BUILD,
        strategy=IndustryProjectItem.Strategy.BUILD_TO_MINERALS,
    )
    compute_project_bom(project)
    item = project.items.first()
    # Datacore (900) appears as an INVENT requirement (2 cores for the cruiser BPC).
    invent = item.material_requirements.filter(
        acquire_method=MaterialRequirement.AcquireMethod.INVENT
    )
    assert invent.count() == 1
    core = invent.get()
    assert core.type_id == 900 and core.quantity_required == 2

    # Economics: value from product price (600 = 50,000,000), profit = value - cost.
    econ = project_economics(project)
    assert econ["value"] == Decimal("50000000")
    assert econ["profit"] == econ["value"] - econ["cost"]
    project.refresh_from_db()
    assert project.estimated_value == Decimal("50000000")


@pytest.mark.django_db
def test_fifo_reservation_and_consume(sde):
    stock = Stockpile.objects.create(name="Staging")
    record_manual_stock(stock, type_id=34, quantity_current=100000)
    project = IndustryProject.objects.create(name="P")

    reserved = reserve_for_project(project, 34, 30000)
    assert reserved == 30000
    assert available_quantity(34) == 70000  # current 100k - reserved 30k

    # Reserving more than available reserves only what's free.
    reserved2 = reserve_for_project(project, 34, 80000)
    assert reserved2 == 70000
    assert available_quantity(34) == 0

    # Consuming a reservation decrements current stock.
    res = StockReservation.objects.filter(project=project).first()
    consume_reservation(res)
    item = stock.items.get(type_id=34)
    assert item.quantity_current == 100000 - res.quantity_reserved
