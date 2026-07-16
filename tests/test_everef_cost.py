"""EVE Ref industry-cost client: parsing, not-manufacturable, and the breaker."""
from __future__ import annotations

from decimal import Decimal

import requests
import responses
from django.core.cache import cache

from apps.industry import everef_cost
from apps.industry.everef_cost import manufacturing_cost_per_unit


@responses.activate
def test_parses_total_cost_per_unit():
    cache.clear()
    responses.add(
        responses.GET, everef_cost._API,
        json={"manufacturing": {"16227": {"total_cost_per_unit": 50597555.8}}}, status=200,
    )
    assert manufacturing_cost_per_unit(16227) == Decimal("50597555.80")


@responses.activate
def test_not_manufacturable_returns_none():
    cache.clear()
    responses.add(responses.GET, everef_cost._API, json={"manufacturing": {}}, status=200)
    assert manufacturing_cost_per_unit(99999) is None


@responses.activate
def test_timeout_trips_breaker_and_returns_none():
    cache.clear()
    responses.add(responses.GET, everef_cost._API, body=requests.exceptions.ConnectTimeout())
    assert manufacturing_cost_per_unit(16227) is None
    assert everef_cost._api_down() is True
    # With the breaker open, a second call short-circuits without another request.
    assert manufacturing_cost_per_unit(587) is None
    assert len(responses.calls) == 1


@responses.activate
def test_zero_cost_is_not_a_price():
    """A 0 (or negative) job cost is bogus, must not be returned, and must not be
    cached as a real figure — the store would freeze it into an order."""
    cache.clear()
    responses.add(
        responses.GET, everef_cost._API,
        json={"manufacturing": {"16227": {"total_cost_per_unit": 0}}}, status=200,
    )
    assert manufacturing_cost_per_unit(16227) is None
    # Cached as "not manufacturable" (empty), so a repeat stays None without a refetch.
    assert manufacturing_cost_per_unit(16227) is None
    assert len(responses.calls) == 1


@responses.activate
def test_malformed_and_nonfinite_costs_fall_back_instead_of_raising():
    """decimal.InvalidOperation is NOT a ValueError — a garbage payload must degrade
    to None (caller falls back to the local estimate), never 500 the order POST.
    JSON NaN parses quietly into Decimal('NaN') and must be rejected too."""
    cache.clear()
    responses.add(
        responses.GET, everef_cost._API,
        json={"manufacturing": {"16227": {"total_cost_per_unit": "N/A"}}}, status=200,
    )
    assert manufacturing_cost_per_unit(16227) is None

    cache.clear()
    responses.replace(
        responses.GET, everef_cost._API,
        json={"manufacturing": {"16227": {"total_cost_per_unit": float("nan")}}}, status=200,
    )
    assert manufacturing_cost_per_unit(16227) is None


@responses.activate
def test_inflight_guard_dedupes_concurrent_fetches():
    """While one caller is fetching a cold key, others fall back (None) instead of
    piling parallel outbound requests onto EVE Ref from the server IP."""
    cache.clear()
    cache.add("everef:indcost:16227:10:1:0:inflight", 1, 10)
    assert manufacturing_cost_per_unit(16227) is None
    assert len(responses.calls) == 0  # never touched the network
