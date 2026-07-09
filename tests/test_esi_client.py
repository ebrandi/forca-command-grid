"""Tests for the disciplined ESI client and rate-limit guards."""
from __future__ import annotations

import time

import pytest
import responses
from django.core.cache import cache

from core.esi import ratelimit
from core.esi.client import ESIClient, ESIRateLimited


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@responses.activate
def test_get_sends_required_headers_and_returns_json(settings):
    settings.ESI_USER_AGENT = "forca-test/1.0 (qa@example.com)"
    settings.ESI_COMPATIBILITY_DATE = "2026-06-21"
    responses.add(
        responses.GET,
        "https://esi.evetech.net/characters/1/",
        json={"corporation_id": 98000001, "name": "X"},
        status=200,
    )
    client = ESIClient()
    resp = client.get("/characters/1/")
    assert resp.status == 200
    assert resp.data["corporation_id"] == 98000001
    sent = responses.calls[0].request
    assert sent.headers["User-Agent"] == "forca-test/1.0 (qa@example.com)"
    assert sent.headers["X-Compatibility-Date"] == "2026-06-21"


@responses.activate
def test_etag_304_returns_cached_payload():
    responses.add(
        responses.GET,
        "https://esi.evetech.net/universe/x/",
        json={"v": 1},
        status=200,
        headers={"ETag": "abc"},
    )
    client = ESIClient()
    first = client.get("/universe/x/")
    assert first.data == {"v": 1}

    responses.replace(
        responses.GET, "https://esi.evetech.net/universe/x/", status=304, headers={"ETag": "abc"}
    )
    second = client.get("/universe/x/")
    assert second.not_modified is True
    assert second.data == {"v": 1}
    assert responses.calls[1].request.headers.get("If-None-Match") == "abc"


@responses.activate
def test_429_sets_block_and_raises():
    responses.add(
        responses.GET,
        "https://esi.evetech.net/x/",
        status=429,
        headers={"Retry-After": "30"},
    )
    client = ESIClient()
    with pytest.raises(ESIRateLimited):
        client.get("/x/")
    # Subsequent non-essential calls are deferred without hitting the network.
    assert ratelimit.seconds_until_unblocked() > 0
    with pytest.raises(ESIRateLimited):
        client.get("/y/")


def test_error_budget_blocks_non_essential_but_allows_essential():
    ratelimit.record_response(
        {"X-Esi-Error-Limit-Remain": "2", "X-Esi-Error-Limit-Reset": "30"}
    )
    assert ratelimit.can_call(essential=False) is False
    assert ratelimit.can_call(essential=True) is True


@responses.activate
def test_paginated_uses_x_pages():
    for page in (1, 2):
        responses.add(
            responses.GET,
            "https://esi.evetech.net/list/",
            json=[{"p": page}],
            status=200,
            headers={"X-Pages": "2"},
        )
    client = ESIClient()
    items = client.get_paged("/list/")
    assert len(items) == 2


def test_seconds_until_unblocked_respects_retry_after():
    ratelimit.note_retry_after(5)
    assert 0 < ratelimit.seconds_until_unblocked() <= 5
    assert ratelimit.can_call(essential=True) is False  # hard 429 block
    time.sleep(0)  # no real wait; just asserting state
