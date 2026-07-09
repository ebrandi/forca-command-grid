"""Tests for killmail valuation pricing (the trillion-ISK-ship fix).

Two root causes are covered:
  * price_for must never fall back to SDE base_price (a bookkeeping figure that is
    wrong by orders of magnitude); it uses live Jita, then CCP adjusted, then 0.
  * Blueprint *copies* (singleton==2) must value at 0, not at a blueprint price.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import responses

from apps.killboard.models import Killmail, KillmailItem
from apps.killboard.valuation import _item_unit_value, compute_value
from apps.market.models import MarketPrice
from apps.market.pricing import build_price_index, price_for
from apps.market.services import ingest_adjusted_prices
from apps.sde.models import SdeType


def _jita(type_id: int, sell_min) -> None:
    MarketPrice.objects.create(
        type_id=type_id, location=None, profile=MarketPrice.Profile.JITA_SELL,
        sell_min=Decimal(sell_min),
    )


def _adjusted(type_id: int, adjusted) -> None:
    MarketPrice.objects.create(
        type_id=type_id, location=None, profile=MarketPrice.Profile.ADJUSTED,
        adjusted_price=Decimal(adjusted),
    )


# --- price_for resolution order --------------------------------------------
@pytest.mark.django_db
def test_price_for_prefers_live_jita_sell():
    _jita(34, 5)
    _adjusted(34, 999)
    assert price_for(34) == Decimal("5")


@pytest.mark.django_db
def test_price_for_falls_back_to_adjusted_when_no_jita():
    _adjusted(34, 7)
    assert price_for(34) == Decimal("7")


@pytest.mark.django_db
def test_price_for_returns_zero_when_no_market_signal():
    assert price_for(99999) == Decimal("0")


@pytest.mark.django_db
def test_price_for_never_uses_sde_base_price():
    # A type with an absurd SDE base_price but no market data must value at 0,
    # not 50 billion — this is exactly the blueprint/ammo inflation bug.
    grp = _group()
    SdeType.objects.create(type_id=12006, group=grp, name="Ishtar Blueprint",
                           volume=0.01, base_price=Decimal("50000000000"))
    assert price_for(12006) == Decimal("0")


# --- batch price index matches price_for -----------------------------------
@pytest.mark.django_db
def test_build_price_index_matches_price_for():
    _jita(34, 5)          # live Jita wins
    _adjusted(34, 999)
    _adjusted(35, 7)      # adjusted-only fallback
    lookup = build_price_index()
    assert lookup(34) == price_for(34) == Decimal("5")
    assert lookup(35) == price_for(35) == Decimal("7")
    assert lookup(99999) == price_for(99999) == Decimal("0")


# --- blueprint-copy (BPC) handling -----------------------------------------
@pytest.mark.django_db
def test_bpc_item_values_zero_even_with_a_market_price():
    _jita(12006, 50_000_000_000)  # even if some price exists, a *copy* is worth 0
    item = KillmailItem(item_type_id=12006, singleton=2, quantity_destroyed=1)
    assert _item_unit_value(item) == Decimal("0")


@pytest.mark.django_db
def test_non_bpc_item_uses_market_price():
    _jita(587, 380000)
    item = KillmailItem(item_type_id=587, singleton=0, quantity_destroyed=1)
    assert _item_unit_value(item) == Decimal("380000")


@pytest.mark.django_db
def test_compute_value_excludes_bpc_cargo():
    _jita(587, 1_000_000)        # hull
    _jita(12006, 50_000_000_000)  # blueprint type (a copy in cargo below)
    km = Killmail.objects.create(
        killmail_id=1, killmail_hash="h", killmail_time="2026-06-20T00:00:00Z",
        solar_system_id=30000142, victim_ship_type_id=587,
    )
    KillmailItem.objects.create(killmail=km, idx=0, item_type_id=12006, singleton=2,
                                quantity_destroyed=10)  # 10 BPCs -> 0, not 500B
    values = compute_value(km)
    assert values["destroyed_value"] == Decimal("1000000")  # just the hull
    assert values["total_value"] == Decimal("1000000")


# --- adjusted-price ingestion ----------------------------------------------
@responses.activate
@pytest.mark.django_db
def test_ingest_adjusted_prices_upserts_rows():
    responses.add(
        responses.GET, "https://esi.evetech.net/markets/prices/",
        json=[
            {"type_id": 34, "adjusted_price": 5.5, "average_price": 5.4},
            {"type_id": 35, "adjusted_price": 11.0},
            {"type_id": 36},  # no prices -> skipped
        ],
        status=200,
    )
    n = ingest_adjusted_prices()
    assert n == 2
    row = MarketPrice.objects.get(type_id=34, profile=MarketPrice.Profile.ADJUSTED)
    assert row.adjusted_price == Decimal("5.50")
    assert not MarketPrice.objects.filter(type_id=36).exists()


@responses.activate
@pytest.mark.django_db
def test_ingest_adjusted_prices_is_idempotent():
    body = [{"type_id": 34, "adjusted_price": 5.5}]
    for _ in range(2):
        responses.add(responses.GET, "https://esi.evetech.net/markets/prices/",
                      json=body, status=200)
        ingest_adjusted_prices()
    assert MarketPrice.objects.filter(
        type_id=34, profile=MarketPrice.Profile.ADJUSTED
    ).count() == 1


def _group():
    from apps.sde.models import SdeCategory, SdeGroup
    cat = SdeCategory.objects.create(category_id=1, name="Test")
    return SdeGroup.objects.create(group_id=1, category=cat, name="Test")
