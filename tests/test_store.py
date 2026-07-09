"""Corp Store: pricing, capital classification, audience, ordering and fulfilment."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.doctrines.models import Doctrine, DoctrineFit
from apps.identity.models import RoleAssignment
from apps.market.models import MarketPrice
from apps.sde.models import SdeCategory, SdeGroup, SdeType
from apps.sso.services import ensure_role
from apps.store.models import Audience, HullClass, StoreConfig, StoreOrder
from apps.store.pricing import classify_hull, price_doctrine_fit, price_hull
from apps.store.services import active_config, can_access, invalidate_audience_cache, next_status
from core import rbac

HOME_CORP = 98000001


@pytest.fixture
def ships(db):
    ship_cat = SdeCategory.objects.create(category_id=6, name="Ship")
    mat_cat = SdeCategory.objects.create(category_id=4, name="Material")
    cruiser = SdeGroup.objects.create(group_id=26, category=ship_cat, name="Cruiser")
    dread = SdeGroup.objects.create(group_id=485, category=ship_cat, name="Dreadnought")
    titan = SdeGroup.objects.create(group_id=30, category=ship_cat, name="Titan")
    modgrp = SdeGroup.objects.create(group_id=60, category=mat_cat, name="Module")

    ferox = SdeType.objects.create(type_id=16227, group=cruiser, name="Ferox", volume=101000.0)
    nag = SdeType.objects.create(type_id=19720, group=dread, name="Naglfar", volume=18500000.0)
    titan_t = SdeType.objects.create(type_id=11567, group=titan, name="Avatar", volume=160000000.0)
    mod = SdeType.objects.create(type_id=1234, group=modgrp, name="Heavy Neutron Blaster", volume=5.0)
    for t, price in [(ferox, "39000000"), (nag, "2500000000"), (titan_t, "90000000000"), (mod, "1000000")]:
        MarketPrice.objects.create(
            type_id=t.type_id, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal(price)
        )
    return {"ferox": ferox, "nag": nag, "titan": titan_t, "mod": mod}


# --- Pricing ---------------------------------------------------------------
@pytest.mark.django_db
def test_hull_price_is_jita_sell_plus_10(ships):
    p = price_hull(16227, Decimal("1.10"))
    assert p.ok
    assert p.unit_jita == Decimal("39000000.00")
    assert p.unit_price == Decimal("42900000.00")  # +10%
    assert p.hull_class == HullClass.SUBCAP


@pytest.mark.django_db
def test_doctrine_fit_prices_hull_plus_modules(ships):
    doc = Doctrine.objects.create(name="Ferox Fleet")
    fit = DoctrineFit.objects.create(
        doctrine=doc, name="Ferox Railgun", ship_type_id=16227,
        modules=[{"type_id": 1234, "quantity": 7}],
    )
    p = price_doctrine_fit(fit, Decimal("1.10"))
    # hull 39M + 7×1M modules = 46M Jita; ×1.10 = 50.6M.
    assert p.unit_jita == Decimal("46000000.00")
    assert p.unit_price == Decimal("50600000.00")


@pytest.mark.django_db
def test_bulk_pricer_matches_per_fit_and_is_batched(ships):
    """price_doctrine_fits_bulk must return the SAME unit_price/unit_jita as the per-fit
    pricer, in a constant number of queries regardless of how many fits (the Shipyard fix)."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.doctrines.models import Doctrine, DoctrineFit
    from apps.store.pricing import price_doctrine_fit, price_doctrine_fits_bulk

    doc = Doctrine.objects.create(name="Fleet")
    fits = [
        DoctrineFit.objects.create(doctrine=doc, name="Ferox", ship_type_id=16227,
                                   modules=[{"type_id": 1234, "quantity": 7}]),
        DoctrineFit.objects.create(doctrine=doc, name="Nag", ship_type_id=19720,
                                   modules=[{"type_id": 1234, "quantity": 2}]),
        DoctrineFit.objects.create(doctrine=doc, name="Bare", ship_type_id=16227, modules=[]),
    ]
    markup = Decimal("1.10")
    with CaptureQueriesContext(connection) as ctx:
        bulk = price_doctrine_fits_bulk(fits, markup)
    # Identical prices to the single-fit pricer.
    for f in fits:
        p = price_doctrine_fit(f, markup)
        assert bulk[f.id] == (p.unit_price, p.unit_jita), f"mismatch on {f.name}"
    # Batched via the shared price_for snapshot: a constant 2 MarketPrice scans
    # (Jita-sell + CCP-adjusted) for ALL fits on a cold snapshot, not one per module
    # per fit — and 0 when the snapshot is already warm from another page.
    mp_queries = sum(1 for q in ctx.captured_queries if "market_marketprice" in q["sql"].lower())
    assert mp_queries <= 2, f"expected the batched snapshot (≤2 queries), got {mp_queries}"


@pytest.mark.django_db
def test_store_never_uses_sde_base_price(db):
    """MKT-2: a type with no live Jita price must resolve to the CCP adjusted
    reference (or 0), never SdeType.base_price — which is wrong by orders of
    magnitude and would fabricate a quote / made-to-order deposit."""
    from apps.market.pricing import reset_price_cache

    cat = SdeCategory.objects.create(category_id=6, name="Ship")
    grp = SdeGroup.objects.create(group_id=26, category=cat, name="Cruiser")
    # base_price is a huge bogus SDE figure; there is NO Jita sell row for this hull.
    SdeType.objects.create(
        type_id=55555, group=grp, name="NoMarketHull", volume=100000.0,
        base_price=Decimal("999000000000"),
    )

    # No Jita and no adjusted → price is 0 (unknown), NOT the base_price.
    reset_price_cache()
    p = price_hull(55555, Decimal("1.10"))
    assert p.ok
    assert p.unit_jita == Decimal("0.00")
    assert p.unit_price == Decimal("0.00")

    # With a CCP adjusted reference present, the store uses that (still not base_price).
    MarketPrice.objects.create(
        type_id=55555, profile=MarketPrice.Profile.ADJUSTED, adjusted_price=Decimal("42000000")
    )
    reset_price_cache()
    p2 = price_hull(55555, Decimal("1.10"))
    assert p2.unit_jita == Decimal("42000000.00")
    assert p2.unit_price == Decimal("46200000.00")  # +10%, off adjusted — never base_price


@pytest.mark.django_db
def test_capital_and_supercapital_classification(ships):
    assert classify_hull(19720) == HullClass.CAPITAL       # Naglfar (dread)
    assert classify_hull(11567) == HullClass.SUPERCAPITAL  # Avatar (titan)
    assert classify_hull(16227) == HullClass.SUBCAP        # Ferox


@pytest.mark.django_db
def test_hull_rejects_non_ship(db):
    cat = SdeCategory.objects.create(category_id=4, name="Material")
    grp = SdeGroup.objects.create(group_id=18, category=cat, name="Mineral")
    SdeType.objects.create(type_id=34, group=grp, name="Tritanium", volume=0.01)
    assert price_hull(34, Decimal("1.10")).ok is False


# --- Audience --------------------------------------------------------------
def _set_audience(value):
    cfg = active_config()
    cfg.audience = value
    cfg.save(update_fields=["audience"])
    invalidate_audience_cache()


@pytest.mark.django_db
def test_default_audience_is_alliance():
    assert StoreConfig._meta.get_field("audience").default == Audience.ALLIANCE


@pytest.mark.django_db
def test_corp_audience_blocks_outsider(django_user_model):
    _set_audience(Audience.CORP)
    from django.contrib.auth.models import AnonymousUser
    member = django_user_model.objects.create(username="eve:9001")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    assert can_access(AnonymousUser()) is False
    assert can_access(member) is True


# --- Status flow -----------------------------------------------------------
def test_doctrine_flow_skips_build_steps():
    order = StoreOrder(requires_build=False, status=StoreOrder.Status.CLAIMED)
    assert next_status(order) == StoreOrder.Status.READY
    order.status = StoreOrder.Status.READY
    assert next_status(order) == StoreOrder.Status.DELIVERED
    order.status = StoreOrder.Status.DELIVERED
    assert next_status(order) is None


def test_hull_flow_walks_deposit_and_production():
    order = StoreOrder(requires_build=True, status=StoreOrder.Status.CLAIMED)
    assert next_status(order) == StoreOrder.Status.DEPOSIT_PAID
    order.status = StoreOrder.Status.DEPOSIT_PAID
    assert next_status(order) == StoreOrder.Status.IN_PRODUCTION
    order.status = StoreOrder.Status.IN_PRODUCTION
    assert next_status(order) == StoreOrder.Status.READY


# --- View flow -------------------------------------------------------------
@pytest.mark.django_db
def test_anonymous_blocked_when_members_only(client):
    _set_audience(Audience.ALLIANCE)
    assert client.get("/store/").status_code == 403


@pytest.mark.django_db
def test_delivery_system_search_autocomplete(client, django_user_model):
    """The 'Deliver to' picker: solar-system autocomplete, gated by the store audience."""
    from apps.sde.models import SdeRegion, SdeSolarSystem

    region = SdeRegion.objects.create(region_id=10000002, name="The Forge")
    SdeSolarSystem.objects.create(system_id=30000142, region=region, name="Jita", security=0.9)
    SdeSolarSystem.objects.create(system_id=30000144, region=region, name="Perimeter", security=0.9)

    _set_audience(Audience.CORP)
    member = django_user_model.objects.create(username="eve:9100")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    rows = client.get("/store/systems/search/?q=Jit").json()
    assert any(r["type_id"] == 30000142 and r["name"] == "Jita" for r in rows)

    # Same store-audience gate as the storefront: an outsider is refused (no leak).
    client.logout()
    resp = client.get("/store/systems/search/?q=Jit")
    assert resp.status_code == 403
    assert resp.json() == []


@pytest.mark.django_db
def test_hull_order_creates_deposit_and_board_claim_flow(client, django_user_model, ships):
    _set_audience(Audience.CORP)
    from apps.sso.models import EveCharacter

    buyer = django_user_model.objects.create(username="eve:9100")
    RoleAssignment.objects.create(user=buyer, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=9100, user=buyer, name="Buyer", is_main=True, is_corp_member=True)
    client.force_login(buyer)

    # Order a Naglfar (capital) — made to order, deposit applies.
    resp = client.post("/store/order/hull/", {"ship_type_id": 19720, "quantity": 1})
    assert resp.status_code == 302
    order = StoreOrder.objects.get()
    assert order.kind == StoreOrder.Kind.HULL
    assert order.hull_class == HullClass.CAPITAL
    assert order.requires_build is True
    # 2.5B ×1.10 = 2.75B total; 25% deposit = 687.5M.
    assert order.total_price == Decimal("2750000000.00")
    assert order.deposit_amount == Decimal("687500000.00")

    # A corp member claims it on the board and walks the build flow.
    builder = django_user_model.objects.create(username="eve:9101")
    RoleAssignment.objects.create(user=builder, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=9101, user=builder, name="Builder", is_main=True, is_corp_member=True)
    client.force_login(builder)
    client.post(f"/store/orders/{order.pk}/claim/")
    order.refresh_from_db()
    assert order.status == StoreOrder.Status.CLAIMED
    assert order.claimed_by_id == builder.id

    for expected in ["deposit_paid", "in_production", "ready", "delivered"]:
        client.post(f"/store/orders/{order.pk}/advance/")
        order.refresh_from_db()
        assert order.status == expected


@pytest.mark.django_db
def test_doctrine_order_has_no_deposit(client, django_user_model, ships):
    _set_audience(Audience.CORP)
    from apps.sso.models import EveCharacter

    doc = Doctrine.objects.create(name="Ferox Fleet")
    fit = DoctrineFit.objects.create(doctrine=doc, name="Ferox", ship_type_id=16227, modules=[])
    user = django_user_model.objects.create(username="eve:9200")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=9200, user=user, name="P", is_main=True, is_corp_member=True)
    client.force_login(user)

    client.post("/store/order/fit/", {"fit_id": fit.id, "quantity": 2})
    order = StoreOrder.objects.get()
    assert order.kind == StoreOrder.Kind.DOCTRINE_FIT
    assert order.requires_build is False
    assert order.deposit_amount == Decimal("0.00")
    assert order.total_price == Decimal("85800000.00")  # 39M×1.10×2


@pytest.mark.django_db
def test_board_is_corp_only(client, django_user_model):
    _set_audience(Audience.PUBLIC)  # even when shopping is public...
    user = django_user_model.objects.create(username="eve:9300")  # ...non-member
    client.force_login(user)
    assert client.get("/store/board/").status_code == 403  # board stays corp-only


# --- Buyer-facing "My orders" ----------------------------------------------
@pytest.mark.django_db
def test_my_orders_shows_only_the_buyers_orders(client, django_user_model, ships):
    _set_audience(Audience.CORP)
    from apps.sso.models import EveCharacter

    me = django_user_model.objects.create(username="eve:9400")
    RoleAssignment.objects.create(user=me, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=9400, user=me, name="Me", is_main=True, is_corp_member=True)
    other = django_user_model.objects.create(username="eve:9401")

    mine = StoreOrder.objects.create(
        buyer=me, kind=StoreOrder.Kind.HULL, ship_type_id=16227, ship_name="Ferox",
        total_price=Decimal("42900000.00"), status=StoreOrder.Status.OPEN,
    )
    theirs = StoreOrder.objects.create(
        buyer=other, kind=StoreOrder.Kind.HULL, ship_type_id=16227, ship_name="Ferox",
        total_price=Decimal("42900000.00"), status=StoreOrder.Status.OPEN,
    )
    client.force_login(me)
    resp = client.get("/store/orders/mine/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert f"/store/orders/{mine.pk}/" in html
    assert f"/store/orders/{theirs.pk}/" not in html  # never another buyer's order


@pytest.mark.django_db
def test_my_orders_splits_active_and_history(client, django_user_model, ships):
    _set_audience(Audience.CORP)
    from apps.sso.models import EveCharacter

    me = django_user_model.objects.create(username="eve:9410")
    RoleAssignment.objects.create(user=me, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=9410, user=me, name="Me", is_main=True, is_corp_member=True)
    active = StoreOrder.objects.create(
        buyer=me, kind=StoreOrder.Kind.HULL, ship_type_id=16227, ship_name="Ferox",
        total_price=Decimal("100"), status=StoreOrder.Status.IN_PRODUCTION,
    )
    delivered = StoreOrder.objects.create(
        buyer=me, kind=StoreOrder.Kind.HULL, ship_type_id=16227, ship_name="Ferox",
        total_price=Decimal("100"), status=StoreOrder.Status.DELIVERED,
    )
    client.force_login(me)
    ctx = client.get("/store/orders/mine/").context
    assert active in ctx["active"] and active not in ctx["history"]
    assert delivered in ctx["history"] and delivered not in ctx["active"]


@pytest.mark.django_db
def test_my_orders_requires_login(client):
    _set_audience(Audience.CORP)
    assert client.get("/store/orders/mine/").status_code == 302  # to login
