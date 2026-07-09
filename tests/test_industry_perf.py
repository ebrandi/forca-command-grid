"""Performance guardrails: the price + recipe caches keep the hot paths query-cheap.

These lock in the N+1 fixes — if someone reintroduces a per-call DB hit in pricing
or recipe lookup, the query-count assertions here fail.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.industry import bom, calc
from apps.market.pricing import price_for, reset_price_cache

pytestmark = pytest.mark.django_db

CRUISER = 600


def test_price_for_snapshot_is_cached(priced_sde):
    reset_price_cache()
    with CaptureQueriesContext(connection) as first:
        price_for(34)
        price_for(35)
        price_for(CRUISER)
    # The snapshot is built once (jita + adjusted = 2 queries); the rest are free.
    assert len(first.captured_queries) == 2

    with CaptureQueriesContext(connection) as warm:
        for tid in (34, 35, 587, CRUISER, 700, 800, 999999):
            price_for(tid)
    assert len(warm.captured_queries) == 0


def test_recipe_lookups_are_cached(sde):
    bom.reset_recipe_cache()
    # Warm each independent lookup cache once...
    bom.buildable_recipe(CRUISER)
    bom.direct_materials(CRUISER, runs=2, me=10)
    bom.blueprint_for(CRUISER)
    bom.product_for(601)
    # ...then repeats hit no database.
    with CaptureQueriesContext(connection) as warm:
        bom.buildable_recipe(CRUISER)
        bom.direct_materials(CRUISER, runs=5, me=0)
        bom.blueprint_for(CRUISER)
        bom.product_for(601)
    assert len(warm.captured_queries) == 0


def test_manufacturing_estimate_is_query_bounded(priced_sde):
    calc.manufacturing_estimate(CRUISER, runs=1)  # warm price + recipe caches
    with CaptureQueriesContext(connection) as ctx:
        est = calc.manufacturing_estimate(CRUISER, runs=1)
    assert est["buildable"]
    # With warm caches the whole recursive estimate is a handful of queries
    # (volumes + activity time), not one-per-material.
    assert len(ctx.captured_queries) <= 5, [q["sql"][:90] for q in ctx.captured_queries]
