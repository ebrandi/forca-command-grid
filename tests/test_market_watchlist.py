"""MKT-3 — market item search, pagination, and personal watchlist."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.market.models import MarketLocation, MarketPrice, MarketWatch
from apps.sde.models import SdeCategory, SdeGroup, SdeType
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db


def _member(django_user_model, name="eve:mkt"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    return user


def _loc():
    return MarketLocation.objects.create(name="Jita", location_type="system",
                                         region_id=10000002, system_id=30000142)


def _type(type_id, name):
    cat, _ = SdeCategory.objects.get_or_create(category_id=6, defaults={"name": "Ship"})
    grp, _ = SdeGroup.objects.get_or_create(group_id=25, defaults={"category": cat, "name": "Frigate"})
    SdeType.objects.get_or_create(type_id=type_id,
                                  defaults={"group": grp, "name": name, "published": True})


def _price(type_id, loc, sell="100"):
    return MarketPrice.objects.create(type_id=type_id, location=loc, sell_min=Decimal(sell),
                                      profile=MarketPrice.Profile.JITA_SELL)


# --- search ------------------------------------------------------------------
def test_search_filters_by_item_name(client, django_user_model):
    loc = _loc()
    _type(587, "Rifter")
    _price(587, loc)
    _type(34, "Tritanium")
    _price(34, loc)
    client.force_login(_member(django_user_model))
    resp = client.get(reverse("market:dashboard"), {"q": "Rifter"})
    assert resp.status_code == 200
    prices = resp.context["prices"]
    assert {p.type_id for p in prices} == {587}


def test_search_no_match_shows_empty(client, django_user_model):
    loc = _loc()
    _type(587, "Rifter")
    _price(587, loc)
    client.force_login(_member(django_user_model))
    resp = client.get(reverse("market:dashboard"), {"q": "zzzznope"})
    assert resp.status_code == 200
    assert list(resp.context["prices"]) == []


# --- pagination --------------------------------------------------------------
def test_pagination(client, django_user_model):
    loc = _loc()
    for i in range(60):
        _price(1000 + i, loc)
    client.force_login(_member(django_user_model))
    page1 = client.get(reverse("market:dashboard"))
    assert len(page1.context["prices"]) == 50
    assert page1.context["page_obj"].paginator.num_pages == 2
    page2 = client.get(reverse("market:dashboard"), {"page": 2})
    assert len(page2.context["prices"]) == 10


# --- watchlist ---------------------------------------------------------------
def test_toggle_watch_adds_then_removes(client, django_user_model):
    user = _member(django_user_model)
    client.force_login(user)
    url = reverse("market:toggle_watch")
    assert client.post(url, {"type_id": 587}).status_code == 302
    assert MarketWatch.objects.filter(user=user, type_id=587).exists()
    client.post(url, {"type_id": 587})  # toggle off
    assert not MarketWatch.objects.filter(user=user, type_id=587).exists()


def test_watchlist_shown_on_dashboard(client, django_user_model):
    loc = _loc()
    _type(587, "Rifter")
    _price(587, loc)
    user = _member(django_user_model)
    MarketWatch.objects.create(user=user, type_id=587)
    client.force_login(user)
    resp = client.get(reverse("market:dashboard"))
    assert {p.type_id for p in resp.context["watch_prices"]} == {587}
    assert b"My watchlist" in resp.content


def test_watchlist_is_self_scoped(client, django_user_model):
    loc = _loc()
    _price(587, loc)
    me = _member(django_user_model, "eve:me")
    other = _member(django_user_model, "eve:other")
    MarketWatch.objects.create(user=other, type_id=587)  # someone else's watch
    client.force_login(me)
    resp = client.get(reverse("market:dashboard"))
    assert list(resp.context["watch_prices"]) == []


def test_toggle_watch_bad_type_id_no_crash(client, django_user_model):
    client.force_login(_member(django_user_model))
    assert client.post(reverse("market:toggle_watch"), {"type_id": "abc"}).status_code == 302


def test_toggle_watch_out_of_range_type_id_no_crash(client, django_user_model):
    # > int4 max would 500 on the INSERT if not range-checked before the DB call
    user = _member(django_user_model)
    client.force_login(user)
    resp = client.post(reverse("market:toggle_watch"), {"type_id": "2147483648"})
    assert resp.status_code == 302
    assert not MarketWatch.objects.filter(user=user).exists()
