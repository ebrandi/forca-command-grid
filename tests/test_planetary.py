"""Planetary Industry: static data, chain maths, profit calc, recommendations,
CRUD, permissions/IDOR, rendering, and degraded-mode (missing price / stale ESI).

The PI static rulebook is loaded by the ``pi_static`` fixture (``load_pi_static``),
which is self-contained — it upserts the SdeType rows for PI materials too, so these
tests don't need the full SDE.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def pi_static(db):
    call_command("load_pi_static")
    return True


@pytest.fixture
def pi_priced(pi_static):
    """Price every PI material by tier so profit tests get real numbers."""
    from apps.market.models import MarketPrice
    from apps.planetary.models import PiMaterial

    tier_price = {"P0": 5, "P1": 100, "P2": 1000, "P3": 8000, "P4": 60000}
    MarketPrice.objects.bulk_create([
        MarketPrice(type_id=m.type_id, location=None,
                    profile=MarketPrice.Profile.JITA_SELL,
                    sell_min=Decimal(tier_price[m.tier]),
                    buy_max=Decimal(tier_price[m.tier]) * Decimal("0.9"))
        for m in PiMaterial.objects.all()
    ])
    return True


def _member(dum, suffix, cid=None):
    user, _ = dum.objects.get_or_create(username=f"pi-{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.get_or_create(
        character_id=cid or (990000 + abs(hash(suffix)) % 9000),
        defaults={"user": user, "name": suffix, "is_main": True, "is_corp_member": True})
    return user


def _officer(dum, suffix="off"):
    user, _ = dum.objects.get_or_create(username=f"pio-{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _mat(name):
    from apps.planetary.models import PiMaterial
    return PiMaterial.objects.get(name=name)


# --------------------------------------------------------------------------- #
# static data + chains
# --------------------------------------------------------------------------- #
def test_load_pi_static_populates_rulebook(pi_static):
    from apps.planetary.models import (
        PiMaterial,
        PiPlanetResource,
        PiPlanetType,
        PiSchematic,
        PiSchematicInput,
    )
    assert PiMaterial.objects.count() == 83
    assert {t: PiMaterial.objects.filter(tier=t).count() for t in ["P0", "P1", "P2", "P3", "P4"]} == \
        {"P0": 15, "P1": 15, "P2": 24, "P3": 21, "P4": 8}
    assert PiPlanetType.objects.count() == 8
    assert PiPlanetResource.objects.count() == 40  # 8 planets × 5 resources
    assert PiSchematic.objects.count() == 68
    assert PiSchematicInput.objects.exists()


def test_load_pi_static_is_idempotent(pi_static):
    from apps.planetary.models import PiMaterial, PiSchematic
    call_command("load_pi_static")
    assert PiMaterial.objects.count() == 83
    assert PiSchematic.objects.count() == 68


def test_p1_schematic_shape(pi_static):
    """Water: 3000 Aqueous Liquids → 20, 30-minute cycle (authoritative from EveRef)."""
    water = _mat("Water")
    sch = water.schematic
    assert sch.output_quantity == 20 and sch.cycle_seconds == 1800
    assert {i.material.name: i.quantity for i in sch.inputs.all()} == {"Aqueous Liquids": 3000}


def test_chain_requirements_to_raw(pi_static):
    from apps.planetary import chains
    g = chains.build_graph()
    node = g.requirements(_mat("Coolant").type_id, 5)
    leaves = {g.material(t).name: float(q) for t, q in g.raw_leaves(node).items()}
    # 5 Coolant → 40 Electrolytes + 40 Water → 6000 Ionic Solutions + 6000 Aqueous Liquids
    assert leaves == {"Ionic Solutions": 6000.0, "Aqueous Liquids": 6000.0}


def test_planet_cover_and_resources(pi_static):
    from apps.planetary import chains
    g = chains.build_graph()
    leaves = list(g.raw_leaves(g.requirements(_mat("Coolant").type_id, 1)))
    assert g.planet_cover(leaves) == ["gas"]  # Gas yields both Aqueous Liquids and Ionic Solutions
    assert set(m.name for m in g.planet_types["temperate"].resource_materials) == {
        "Aqueous Liquids", "Autotrophs", "Carbon Compounds", "Complex Organisms", "Microorganisms"}


def test_reachable_and_becomes(pi_static):
    from apps.planetary import chains
    g = chains.build_graph()
    reachable = {m.name for m in g.reachable_products(g.resources_by_planet["gas"])}
    assert "Water" in reachable and "Electrolytes" in reachable
    becomes = {s.name for s in g.becomes(_mat("Water").type_id)}
    assert "Coolant" in becomes


# --------------------------------------------------------------------------- #
# profit calc
# --------------------------------------------------------------------------- #
def _make_plan(user, **overrides):
    from apps.planetary.models import PiPlan
    defaults = dict(owner=user, name="Test", goal="p0_p2", market_region_id=10000002,
                    market_region_name="The Forge (Jita)", extraction_rate_per_hour=2000)
    defaults.update(overrides)
    plan = PiPlan.objects.create(**defaults)
    return plan


def test_extraction_plan_is_profitable(pi_priced, django_user_model):
    from apps.planetary import calc
    from apps.planetary.models import PiPlanetType
    u = _member(django_user_model, "calc1")
    plan = _make_plan(u)
    plan.planets.create(planet_type=PiPlanetType.objects.get(slug="gas"),
                        role="extract", primary_material=_mat("Water"))
    econ = calc.plan_economics(plan)
    # 2000/hr → 48000 P0/day → 320 Water/day × 100 ISK, minus customs/fees → positive
    assert econ["totals"]["net_day"] > 0
    assert not econ["missing_prices"]
    assert econ["planets"][0]["daily_units"] == 320.0


def test_missing_price_degrades_gracefully(pi_static, django_user_model):
    from apps.planetary import calc
    from apps.planetary.models import PiPlanetType
    u = _member(django_user_model, "calc2")
    plan = _make_plan(u)
    plan.planets.create(planet_type=PiPlanetType.objects.get(slug="gas"),
                        role="extract", primary_material=_mat("Water"))
    econ = calc.plan_economics(plan)  # no prices seeded
    assert econ["totals"]["gross_day"] == 0.0          # no crash, revenue 0
    assert econ["missing_prices"]                       # surfaced honestly
    assert any("unpriced" in w.lower() for w in econ["warnings"])


def test_refine_vs_sell(pi_priced):
    from apps.planetary import calc, chains
    from apps.planetary.prices import PriceProvider
    g = chains.build_graph()
    p = PriceProvider()
    cmp = calc.refine_vs_sell(_mat("Coolant").type_id, p, g)
    # 40 Electrolytes + 40 Water = 80 × 100 = 8000; output 5 Coolant × 1000 = 5000 → sell inputs
    assert cmp["input_value"] == 8000.0 and cmp["output_value"] == 5000.0
    assert cmp["better"] == "sell_inputs"


def test_output_override_used(pi_priced, django_user_model):
    from apps.planetary import calc
    from apps.planetary.models import PiPlanetType
    u = _member(django_user_model, "calc3")
    plan = _make_plan(u)
    plan.planets.create(planet_type=PiPlanetType.objects.get(slug="gas"), role="extract",
                        primary_material=_mat("Water"), output_override=1000)
    econ = calc.plan_economics(plan)
    assert econ["planets"][0]["daily_units"] == 1000.0
    assert econ["planets"][0]["basis"] == "override"


# --------------------------------------------------------------------------- #
# recommendations
# --------------------------------------------------------------------------- #
def test_recommendations_ranked_and_badged(pi_priced):
    from apps.planetary import recommend, services
    cfg = services.active_config()
    items = recommend.recommend(config=cfg, goal="p0_p1", limit=5)
    assert items
    nets = [i["net_day"] for i in items]
    assert nets == sorted(nets, reverse=True)              # ranked by net/day
    assert any(label == "Best profit" for label, _ in items[0]["badges"])


def test_corp_priority_badge(pi_priced):
    from apps.planetary import recommend, services
    cfg = services.active_config()
    cfg.recommended_products = [_mat("Coolant").type_id]
    cfg.save()
    items = recommend.recommend(config=cfg, goal="p0_p2", limit=24)
    coolant = next(i for i in items if i["name"] == "Coolant")
    assert any(label == "Corp priority" for label, _ in coolant["badges"])


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def test_form_rejects_bad_tax_and_planet_count(pi_static, django_user_model):
    from apps.planetary.forms import PiPlanForm
    u = _member(django_user_model, "val")
    form = PiPlanForm(data={"name": "X", "goal": "beginner", "planet_count": "9",
                            "market_region_id": "10000002", "customs_export_tax": "150",
                            "customs_import_tax": "5", "sales_tax": "4.5", "broker_fee": "3",
                            "hauling_cost_per_m3": "0", "corp_buyback_rate": "90",
                            "extraction_rate_per_hour": "2000", "effort": "daily", "risk": "highsec",
                            "export_strategy": "haul_hub", "visibility": "private"}, user=u)
    assert not form.is_valid()
    assert "customs_export_tax" in form.errors and "planet_count" in form.errors


# --------------------------------------------------------------------------- #
# CRUD via views
# --------------------------------------------------------------------------- #
def _create_plan_via_post(client, cid):
    coolant = _mat("Coolant").type_id
    water = _mat("Water").type_id
    return client.post("/industry/pi/plans/new/", {
        "name": "My Coolant Plan", "goal": "p0_p2", "character": str(cid), "system_name": "Jita",
        "planet_count": "2", "risk": "highsec", "market_region_id": "10000002",
        "customs_export_tax": "5", "customs_import_tax": "5", "sales_tax": "4.5", "broker_fee": "3",
        "hauling_cost_per_m3": "0", "corp_buyback_rate": "90", "extraction_rate_per_hour": "2000",
        "effort": "daily", "export_strategy": "haul_hub", "visibility": "private", "notes": "",
        "planet_type": ["gas", "gas"], "planet_role": ["extract", "factory"],
        "planet_product": [str(water), str(coolant)],
    })


@pytest.mark.django_db
def test_create_edit_duplicate_delete(client, pi_priced, django_user_model):
    from apps.planetary.models import PiPlan, PiStatus
    u = _member(django_user_model, "crud", cid=991001)
    client.force_login(u)

    resp = _create_plan_via_post(client, 991001)
    assert resp.status_code == 302
    plan = PiPlan.objects.get(owner=u, name="My Coolant Plan")
    assert plan.planets.count() == 2
    assert plan.snapshot  # recomputed on create

    assert client.get(f"/industry/pi/plans/{plan.id}/").status_code == 200
    assert client.post(f"/industry/pi/plans/{plan.id}/recalc/").status_code == 302

    # duplicate
    assert client.post(f"/industry/pi/plans/{plan.id}/duplicate/").status_code == 302
    assert PiPlan.objects.filter(owner=u, name="My Coolant Plan (copy)").exists()

    # two-step delete: archive, then permanently delete
    client.post(f"/industry/pi/plans/{plan.id}/delete/")
    plan.refresh_from_db()
    assert plan.status == PiStatus.ARCHIVED
    client.post(f"/industry/pi/plans/{plan.id}/delete/")
    assert not PiPlan.objects.filter(id=plan.id).exists()


# --------------------------------------------------------------------------- #
# permissions / IDOR
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_cannot_view_or_edit_others_private_plan(client, pi_static, django_user_model):
    owner = _member(django_user_model, "owner", cid=992001)
    plan = _make_plan(owner, visibility="private")
    intruder = _member(django_user_model, "intruder", cid=992002)
    client.force_login(intruder)
    assert client.get(f"/industry/pi/plans/{plan.id}/").status_code == 404      # not leaked
    assert client.post(f"/industry/pi/plans/{plan.id}/recalc/").status_code == 403


@pytest.mark.django_db
def test_officer_can_view_shared_plan(client, pi_static, django_user_model):
    from apps.planetary import services
    owner = _member(django_user_model, "o2", cid=993001)
    plan = _make_plan(owner, visibility="leadership")
    officer = _officer(django_user_model, "shareoff")
    assert services.can_view(officer, plan) is True
    assert services.can_manage(officer, plan) is True   # officers may manage


# --------------------------------------------------------------------------- #
# page rendering
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
@pytest.mark.parametrize("path", [
    "/industry/pi/", "/industry/pi/learn/", "/industry/pi/explore/",
    "/industry/pi/explore/?planet=lava", "/industry/pi/recommend/",
    "/industry/pi/colonies/", "/industry/pi/plans/new/",
])
def test_pages_render(client, pi_priced, django_user_model, path):
    u = _member(django_user_model, "render", cid=994001)
    client.force_login(u)
    assert client.get(path).status_code == 200


@pytest.mark.django_db
def test_explore_material_page(client, pi_static, django_user_model):
    u = _member(django_user_model, "expl", cid=994002)
    client.force_login(u)
    r = client.get(f"/industry/pi/explore/?material={_mat('Robotics').type_id}")
    assert r.status_code == 200
    assert b"Robotics" in r.content


# --------------------------------------------------------------------------- #
# ESI colonies (stale warning) + admin
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_colonies_page_shows_stale_caveat(client, pi_static, django_user_model):
    u = _member(django_user_model, "col", cid=995001)
    client.force_login(u)
    r = client.get("/industry/pi/colonies/")
    assert r.status_code == 200
    assert b"open that colony in the game client" in r.content   # staleness warning always shown


def test_import_colonies_without_scope(pi_static, django_user_model):
    from apps.planetary.esi import import_colonies
    u = _member(django_user_model, "noscope", cid=995002)
    char = u.characters.first()
    assert import_colonies(char)["status"] == "no_scope"        # no ESI call, honest status


@pytest.mark.django_db
def test_admin_config_saves_priority_products(client, pi_static, django_user_model):
    from apps.planetary import services
    u = _officer(django_user_model, "cfg")
    u.is_superuser = True   # director+ for config
    u.save()
    client.force_login(u)
    assert client.get("/ops/admin/planetary/").status_code == 200
    resp = client.post("/ops/admin/planetary/config/", {
        "enabled": "on", "name": "Standard", "default_market_region_id": "10000002",
        "default_extraction_rate_per_hour": "2500", "default_customs_export_tax": "6",
        "default_customs_import_tax": "6", "default_sales_tax": "4.5", "default_broker_fee": "3",
        "default_hauling_cost_per_m3": "0", "corp_buyback_rate": "88",
        "recommended_products_text": "Coolant, Robotics", "recommended_regions": "",
        "priority_note": "", "default_visibility": "private",
    })
    assert resp.status_code == 302
    cfg = services.active_config()
    assert cfg.default_extraction_rate_per_hour == 2500
    assert _mat("Coolant").type_id in cfg.recommended_products
    assert _mat("Robotics").type_id in cfg.recommended_products
