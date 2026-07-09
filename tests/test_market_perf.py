"""Performance regression guards for the market dashboard.

The dashboard used to issue one query per displayed row (price_trend) and a BOM +
price query per buildable type (build_opportunities), which timed out against a
real database. These lock in the batched/cached behaviour.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.market.models import MarketHistory, MarketPrice
from apps.sde.models import SdeBlueprintMaterial

FORGE = 10000002


def _recipe(product, materials):
    for mat, qty in materials:
        SdeBlueprintMaterial.objects.create(
            blueprint_type_id=product + 1, product_type_id=product,
            material_type_id=mat, quantity=qty, activity=SdeBlueprintMaterial.MANUFACTURING,
        )


@pytest.mark.django_db
def test_build_opportunities_query_count_is_constant(priced_sde, django_assert_max_num_queries):
    # Several buildable products — the query count must NOT scale with the number
    # of products (the old code did one BOM + N price_for queries per product).
    for product in (587, 484, 2046):
        _recipe(product, [(34, 100), (35, 50)])
        MarketPrice.objects.create(type_id=product, profile=MarketPrice.Profile.JITA_SELL,
                                   sell_min=Decimal("9999999"))
    from apps.market.services import build_opportunities

    with django_assert_max_num_queries(5):  # build_price_index(2) + priced(1) + recipes(1)
        ops = build_opportunities(min_profit=1, limit=10)
    assert {o["type_id"] for o in ops} >= {587, 484, 2046}


@pytest.mark.django_db
def test_price_trends_is_a_single_query(django_assert_num_queries):
    today = timezone.now().date()
    for tid in (34, 35, 587):
        for d in range(5):
            MarketHistory.objects.create(
                type_id=tid, region_id=FORGE, date=today - timedelta(days=d),
                average=Decimal("100") + d, highest=Decimal("110"), lowest=Decimal("90"),
                volume=1000, order_count=10,
            )
    from apps.market.services import price_trends

    with django_assert_num_queries(1):
        trends = price_trends([34, 35, 587], FORGE, days=30)
    assert set(trends) == {34, 35, 587}
    assert trends[34]["days"] == 5 and trends[34]["avg_volume"] == 1000


@pytest.mark.django_db
def test_dashboard_signals_are_cached(priced_sde, django_assert_num_queries):
    cache.clear()
    from apps.market.services import dashboard_signals

    first = dashboard_signals()              # computes + caches
    with django_assert_num_queries(0):       # second call is a pure cache read
        second = dashboard_signals()
    assert second == first
