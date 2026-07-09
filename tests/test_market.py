"""Market ingestion / seeding and hauling-generation tests."""
from __future__ import annotations

from decimal import Decimal

import pytest
import responses

from apps.market.models import MarketLocation, MarketPrice
from apps.market.services import (
    ingest_market_prices,
    local_sell_volume,
    seeding_deficit,
    update_price_from_orders,
)
from apps.stockpile.models import HaulingTask, Stockpile
from apps.stockpile.services import generate_hauling_tasks, record_manual_stock


def _loc(name="Jita", region=10000002, staging=False):
    return MarketLocation.objects.create(
        name=name,
        location_type=MarketLocation.LocationType.SYSTEM,
        region_id=region,
        system_id=30000142,
        is_staging=staging,
    )


@pytest.mark.django_db
def test_update_price_from_orders():
    loc = _loc()
    orders = [
        {"is_buy_order": False, "price": 1_000_000.0, "volume_remain": 50},
        {"is_buy_order": False, "price": 1_100_000, "volume_remain": 30},
        {"is_buy_order": True, "price": 900_000, "volume_remain": 10},
    ]
    price = update_price_from_orders(loc, 587, orders)
    assert price.sell_min == Decimal("1000000")
    assert price.buy_max == Decimal("900000")
    assert price.volume == 80
    assert local_sell_volume(587, loc) == 80


@pytest.mark.django_db
def test_seeding_deficit():
    loc = _loc()
    update_price_from_orders(
        loc, 587, [{"is_buy_order": False, "price": 1_000_000, "volume_remain": 12}]
    )
    assert seeding_deficit(587, loc, target=40) == 28
    assert seeding_deficit(587, loc, target=10) == 0


@responses.activate
@pytest.mark.django_db
def test_ingest_market_prices_from_esi():
    loc = _loc()
    responses.add(
        responses.GET,
        "https://esi.evetech.net/markets/10000002/orders/",
        json=[
            {"is_buy_order": False, "price": 5_000_000, "volume_remain": 3},
            {"is_buy_order": True, "price": 4_000_000, "volume_remain": 2},
        ],
        status=200,
    )
    n = ingest_market_prices(loc, [587])
    assert n == 1
    p = MarketPrice.objects.get(type_id=587, location=loc)
    assert p.sell_min == Decimal("5000000")


@pytest.mark.django_db
def test_generate_hauling_tasks_from_shortfall(sde):
    jita = _loc("Jita")
    staging = _loc("Staging", staging=True)
    sp = Stockpile.objects.create(name="Staging hangar", location=staging)
    record_manual_stock(sp, type_id=587, quantity_current=4, quantity_target=40)

    created = generate_hauling_tasks(jita, staging)
    assert created == 1
    task = HaulingTask.objects.get(type_id=587)
    assert task.quantity == 36
    assert task.volume_m3 == pytest.approx(36 * 27289.0)  # SDE volume for Rifter

    # Idempotent: re-running does not duplicate the open task.
    generate_hauling_tasks(jita, staging)
    assert HaulingTask.objects.filter(type_id=587).count() == 1


@pytest.mark.django_db
def test_margin_opportunities_ranks_by_spread(sde):
    from apps.market.services import margin_opportunities

    loc = _loc()
    # Item 34: 20% margin (buy 80 / sell 100). Item 35: 2% (below threshold).
    MarketPrice.objects.create(type_id=34, location=loc, sell_min=Decimal("100"), buy_max=Decimal("80"),
                               profile=MarketPrice.Profile.JITA_SELL)
    MarketPrice.objects.create(type_id=35, location=loc, sell_min=Decimal("100"), buy_max=Decimal("98"),
                               profile=MarketPrice.Profile.JITA_SELL)
    rows = margin_opportunities(min_margin=5.0)
    assert [r["type_id"] for r in rows] == [34]  # 35 filtered out (2% < 5%)
    assert round(rows[0]["margin"]) == 20


@pytest.mark.django_db
def test_officer_adds_market_location(client, django_user_model):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    member = django_user_model.objects.create(username="mm")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    # Member cannot create a location.
    assert client.post("/market/locations/create/", {"name": "X", "location_type": "station"}).status_code == 403

    officer = django_user_model.objects.create(username="oo")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    client.post("/market/locations/create/", {
        "name": "Amarr VIII", "location_type": "station", "region_id": 10000043, "system_id": 30002187,
    })
    assert MarketLocation.objects.filter(name="Amarr VIII").exists()


@pytest.mark.django_db
def test_officer_edits_market_location(client, django_user_model):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    loc = _loc(name="Amarr")
    member = django_user_model.objects.create(username="mm2")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.post(f"/market/locations/{loc.pk}/edit/",
                       {"name": "X", "location_type": "station"}).status_code == 403

    officer = django_user_model.objects.create(username="oo2")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    client.post(f"/market/locations/{loc.pk}/edit/", {
        "name": "Amarr Renamed", "location_type": "station", "region_id": 10000043, "system_id": 30002187,
    })
    loc.refresh_from_db()
    assert loc.name == "Amarr Renamed"


@pytest.mark.django_db
def test_officer_toggles_market_location(client, django_user_model):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    loc = _loc(name="Rens")
    assert loc.active is True
    member = django_user_model.objects.create(username="mm3")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.post(f"/market/locations/{loc.pk}/toggle/").status_code == 403

    officer = django_user_model.objects.create(username="oo3")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    client.post(f"/market/locations/{loc.pk}/toggle/")
    loc.refresh_from_db()
    assert loc.active is False
    client.post(f"/market/locations/{loc.pk}/toggle/")
    loc.refresh_from_db()
    assert loc.active is True


@responses.activate
@pytest.mark.django_db
def test_refresh_jita_prices_from_fuzzwork():
    """MKT-1: the scheduled/manual live-price refresh upserts JITA_SELL from Fuzzwork
    so price_for resolves to the live Jita sell, not the CCP adjusted fallback."""
    from apps.market.pricing import price_for, reset_price_cache
    from apps.market.services import FUZZWORK_AGGREGATES, refresh_jita_prices

    loc = MarketLocation.objects.create(
        name="Jita (ref)", location_type=MarketLocation.LocationType.SYSTEM,
        region_id=10000002, system_id=30000142, is_price_reference=True,
    )
    responses.add(
        responses.GET, FUZZWORK_AGGREGATES,
        json={"587": {"sell": {"min": "1500000.0"}, "buy": {"max": "1400000.0"}}},
        status=200,
    )
    n = refresh_jita_prices(type_ids=[587])
    assert n == 1
    row = MarketPrice.objects.get(type_id=587, profile=MarketPrice.Profile.JITA_SELL)
    assert row.sell_min == Decimal("1500000.0")
    assert row.buy_max == Decimal("1400000.0")
    assert row.location_id == loc.pk
    reset_price_cache()
    assert price_for(587) == Decimal("1500000.0")


@pytest.mark.django_db
def test_sync_jita_prices_task_records_health_and_is_best_effort(monkeypatch):
    """The task refreshes + stamps health first, then re-values best-effort: a
    revalue failure must not lose the price refresh nor raise."""
    from apps.admin_audit.health import _last_sync
    from apps.market import services, tasks

    monkeypatch.setattr(services, "refresh_jita_prices", lambda *a, **k: 42)

    def _boom():
        raise RuntimeError("revalue blew up")

    monkeypatch.setattr(services, "revalue_from_prices", _boom)

    priced = tasks.sync_jita_prices()
    assert priced == 42
    rec = _last_sync("market_jita_prices")
    assert rec and rec.get("types") == 42
