"""Assets page: cached per-location summary + on-demand (lazy-loaded) item detail.

Regression cover for the 2026-07 perf fix — the page used to render every item row
(a multi-MB page) and recompute on every load; now it renders per-location summaries
(cached, busted on sync) and loads a location's items only when it is expanded, scoped
to the requesting owner.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.market.models import MarketPrice
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.stockpile.models import Asset, AssetLocation
from core import rbac

CORP_ID = 98000001
PILOT_ID = 900001


def _price(type_id, sell):
    MarketPrice.objects.create(
        type_id=type_id, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal(sell)
    )


def _loc(lid, name):
    return AssetLocation.objects.create(
        location_id=lid, name=name, kind=AssetLocation.Kind.STATION, system_id=30000142
    )


def _asset(owner_type, owner_id, loc, type_id, qty):
    Asset.objects.create(
        owner_type=owner_type, owner_id=owner_id, location=loc, type_id=type_id,
        quantity=qty, source="test", as_of=timezone.now(),
    )


def _member(dj, cid, role=rbac.ROLE_MEMBER):
    u = dj.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    EveCharacter.objects.create(character_id=cid, user=u, name=f"P{cid}", is_main=True,
                                is_corp_member=True)
    return u


# --- summary aggregation + cache --------------------------------------------
@pytest.mark.django_db
def test_summary_aggregates_and_caches(settings):
    from apps.market.pricing import reset_price_cache
    from apps.stockpile.assets import _store_assets, assets_summary

    _price(34, "10")
    _price(35, "20")
    reset_price_cache()
    a = _loc(60001, "Jita 4-4")
    b = _loc(60002, "Amarr")
    _asset("corporation", CORP_ID, a, 34, 100)   # 100 × 10 = 1000
    _asset("corporation", CORP_ID, a, 35, 5)      # 5 × 20 = 100
    _asset("corporation", CORP_ID, b, 34, 1)      # 1 × 10 = 10

    data = assets_summary("corporation", CORP_ID)
    assert data["total_value"] == Decimal("1110")
    # Sorted by value desc: Jita (1100) before Amarr (10).
    assert [loc_["name"] for loc_ in data["locations"]] == ["Jita 4-4", "Amarr"]
    jita = data["locations"][0]
    assert jita["value"] == Decimal("1100") and jita["item_count"] == 2 and jita["units"] == 105
    # No item detail in the summary (that loads on demand).
    assert "items" not in jita

    # Second call is served from cache (no DB query).
    with CaptureQueriesContext(connection) as ctx:
        assets_summary("corporation", CORP_ID)
    assert len(ctx.captured_queries) == 0

    # A sync (which writes via _store_assets) busts the cache.
    _store_assets("corporation", CORP_ID, {}, "test")  # wipes rows + invalidates
    assert assets_summary("corporation", CORP_ID)["total_value"] == Decimal("0")


# --- location item detail ----------------------------------------------------
@pytest.mark.django_db
def test_location_items_priced_and_bounded():
    from apps.market.pricing import reset_price_cache
    from apps.stockpile.assets import location_items

    _price(34, "10")
    reset_price_cache()
    a = _loc(60001, "Jita")
    b = _loc(60002, "Amarr")
    _asset("character", PILOT_ID, a, 34, 3)
    _asset("character", PILOT_ID, b, 34, 99)  # different location, must NOT appear

    items = location_items("character", PILOT_ID, 60001)
    assert items == [{"type_id": 34, "quantity": 3, "value": Decimal("30")}]


# --- endpoint owner scoping (security) ---------------------------------------
@pytest.mark.django_db
def test_items_endpoint_scopes_owner(client, django_user_model, settings):
    from apps.market.pricing import reset_price_cache

    settings.FORCA_HOME_CORP_ID = CORP_ID
    _price(34, "10")
    _price(35, "10")
    reset_price_cache()
    corp_loc = _loc(70001, "Corp Hangar")
    _asset("corporation", CORP_ID, corp_loc, 34, 424242)   # corp holding — distinctive qty
    member = _member(django_user_model, PILOT_ID)          # non-officer
    _asset("character", PILOT_ID, corp_loc, 35, 333)       # the member's own holding, same loc

    client.force_login(member)
    # A member asking for owner=corp must get THEIR OWN items, never the corp's — even at a
    # location id the corp uses. Distinctive quantities make the leak assertion unambiguous.
    html = client.get(f"/stockpile/assets/items/?owner=corp&location={corp_loc.location_id}").content.decode()
    assert "424242" not in html   # the corp's holding must NOT leak to a non-officer
    assert "333" in html          # the member sees their own holding

    # An officer asking for owner=corp gets the corp's items.
    officer = _member(django_user_model, 900002, role=rbac.ROLE_OFFICER)
    client.force_login(officer)
    html = client.get(f"/stockpile/assets/items/?owner=corp&location={corp_loc.location_id}").content.decode()
    assert "424242" in html


@pytest.mark.django_db
def test_unresolved_location_bucket_labels_and_expands():
    """Assets whose location couldn't be resolved (location=None) must show an 'Unknown
    location' card whose items still load on expand (via the location_id=0 sentinel)."""
    from apps.market.pricing import reset_price_cache
    from apps.stockpile.assets import assets_summary, location_items

    _price(34, "10")
    reset_price_cache()
    Asset.objects.create(owner_type="character", owner_id=PILOT_ID, location=None,
                         type_id=34, quantity=4, source="test", as_of=timezone.now())
    card = assets_summary("character", PILOT_ID)["locations"][0]
    assert card["name"] == "Unknown location"
    assert card["location_id"] == 0 and card["item_count"] == 1
    # Expanding it maps 0 → location__isnull and returns the items (no phantom empty card).
    assert location_items("character", PILOT_ID, 0) == [
        {"type_id": 34, "quantity": 4, "value": Decimal("40")}
    ]


# --- page renders summaries + lazy-load, not inline item rows ----------------
@pytest.mark.django_db
def test_assets_page_lazy_loads_items(client, django_user_model, settings):
    from apps.market.pricing import reset_price_cache

    _price(34, "10")
    reset_price_cache()
    loc = _loc(80001, "Home")
    member = _member(django_user_model, PILOT_ID)
    _asset("character", PILOT_ID, loc, 34, 7)

    client.force_login(member)
    html = client.get("/stockpile/assets/?owner=mine").content.decode()
    # Location header + total are present…
    assert "Home" in html and "1 item type" in html
    # …but the items load on demand (hx-get to the items endpoint), not inline.
    assert "/stockpile/assets/items/?owner=mine&location=80001" in html
    # The lazy-load {# #} comment must NOT leak into the page (multi-line {# #} renders literally).
    assert "Items load on demand" not in html
