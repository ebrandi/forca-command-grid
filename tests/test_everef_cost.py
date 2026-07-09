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
