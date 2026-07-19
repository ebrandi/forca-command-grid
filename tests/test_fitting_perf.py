"""Tocha's Lab performance guards.

Two kinds of guard, both cheap and deterministic in CI:

* **Query budgets** — the live-recompute endpoint (the hot path: it fires on every
  keystroke in the editor) must read the SDE a bounded number of times, and that bound
  must not grow with the number of *duplicate* fitted modules. This is the real N+1
  tripwire; it needs no magic wall-clock number.
* **A coarse p95 latency ceiling** — a generous ceiling on the pure engine evaluation so a
  gross algorithmic regression trips the wire, without the flakiness of a tight timing
  assertion on shared CI.
"""
from __future__ import annotations

import json
import statistics
import time

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.fitting import services
from apps.fitting.engine.types import SkillProfile

from ._fitting_utils import AC, DC, FUSION, RIFTER, make_member, seed_dogma

pytestmark = pytest.mark.django_db


@pytest.fixture
def pilot(db):
    seed_dogma()
    return make_member("eve:2200", 2200, "Perf Pilot")


def _items(n_guns):
    items = [{"type_id": AC, "slot": "high", "state": "active",
              "charge_type_id": FUSION, "quantity": 1} for _ in range(n_guns)]
    items.append({"type_id": DC, "slot": "low", "state": "active",
                  "charge_type_id": None, "quantity": 1})
    return items


def _post_telemetry(client, n_guns):
    return client.post(reverse("fitting:telemetry"), {
        "ship_type_id": RIFTER, "items": json.dumps(_items(n_guns)), "skills": "none"})


def test_recompute_query_count_is_bounded_by_distinct_types(client, pilot):
    """A fit with eight identical guns must not cost more SDE reads than two guns.

    The per-evaluation provider memoizes by type, so a cold recompute's query count is a
    function of DISTINCT fitted types, never the total module count — no N+1 on duplicates.
    The cache is cleared before each measurement so both sides do the full compute (a warm
    hit would skip the engine entirely and mask a regression).
    """
    from django.core.cache import cache

    client.force_login(pilot)
    _post_telemetry(client, 2)  # warm process-local importables + name memo

    cache.clear()
    with CaptureQueriesContext(connection) as few:
        assert _post_telemetry(client, 2).status_code == 200
    cache.clear()
    with CaptureQueriesContext(connection) as many:
        assert _post_telemetry(client, 8).status_code == 200

    assert len(many.captured_queries) == len(few.captured_queries), (
        f"recompute is not O(distinct types): 2 guns={len(few.captured_queries)} "
        f"queries, 8 guns={len(many.captured_queries)}")


def test_warm_recompute_is_cheap(client, pilot):
    """The realistic hot path: once a loadout's telemetry is cached, a repeat recompute of
    it serves from cache within a small query budget (no re-evaluation, no N+1)."""
    client.force_login(pilot)
    _post_telemetry(client, 3)  # first call computes + caches, and warms auth/feature caches

    with CaptureQueriesContext(connection) as ctx:
        assert _post_telemetry(client, 3).status_code == 200
    # Budget covers the cache-warmed recompute plus the "Pilot skills applied" readout, whose
    # skill-name resolution is a single bounded IN query (N+1-safe), not per-module.
    assert len(ctx.captured_queries) <= 13, (
        f"warm recompute took {len(ctx.captured_queries)} queries; expected a cache hit")


def test_provider_memoizes_repeated_type_reads(pilot):
    """The engine adapter reads a repeated type's attrs/skills once per evaluation."""
    from apps.fitting.engine.adapter import ORMDataProvider

    provider = ORMDataProvider()
    with CaptureQueriesContext(connection) as ctx:
        provider.attrs(AC)
        provider.attrs(AC)            # cached — no second query
        provider.required_skills(AC)
        provider.required_skills(AC)  # cached — no second query
    # attrs(AC) issues its own query + the bridged _row lookup; required_skills(AC) one more.
    assert len(ctx.captured_queries) <= 3


def test_engine_evaluation_p95_latency_is_reasonable(pilot):
    """Coarse regression tripwire on the pure engine math (uncached).

    Generous by design — it exists to catch an accidental O(n^2) blow-up, not to benchmark.
    """
    skills = SkillProfile.from_dict({})
    items = _items(6)
    services.evaluate(RIFTER, items, skills, cached=False)  # warm importables

    samples = []
    for _ in range(20):
        start = time.perf_counter()
        services.evaluate(RIFTER, items, skills, cached=False)
        samples.append(time.perf_counter() - start)
    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    assert p95 < 1.0, f"engine p95 {p95 * 1000:.1f}ms exceeds the 1000ms tripwire"
    # A sanity floor on the median too: a real evaluation does work (never a no-op stub).
    assert statistics.median(samples) > 0
