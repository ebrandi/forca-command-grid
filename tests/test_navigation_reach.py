"""4.10 — Multi-jump cyno-chain / reach planner.

Acceptance: systems_within_jumps returns every cyno-capable system reachable within N
cyno jumps of an origin, annotated with the FEWEST jumps; avoided systems cut the chain
(can't route through them); require_stations filters the RESULT only (you can chain
through a stationless system); the origin's own coords being absent returns None.
"""
from __future__ import annotations

import math

import pytest
from django.urls import reverse

from apps.logistics import jumps as J
from apps.logistics.jumps import (
    LIGHT_YEAR_M,
    MAX_JUMP_RANGE_LY,
    clear_graph_cache,
    systems_within_jumps,
)
from apps.sde.models import SdeRegion, SdeSolarSystem, SdeStation

pytestmark = pytest.mark.django_db
LY = LIGHT_YEAR_M


def _sys(sid, x_ly, sec=0.2, name=None):
    # y is a fixed non-zero offset so the x=0 origin isn't (0,0,0) — that triple is the
    # "no coordinates" sentinel (and is excluded from the graph). Distances stay |Δx|.
    return SdeSolarSystem.objects.create(
        system_id=sid, region_id=10000001, name=name or f"S{sid}", security=sec,
        x=x_ly * LY, y=5.0 * LY, z=0.0,
    )


@pytest.fixture
def chain(db):
    """A straight cyno chain O—A—B—C at 5 ly spacing (low-sec, cyno-capable)."""
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "TestRegion"})
    clear_graph_cache()
    J._station_cache.clear()
    _sys(1, 0, name="Origin")
    _sys(2, 5, name="A")
    _sys(3, 10, name="B")
    _sys(4, 15, name="C")
    return None


def _reach(origin, rng, hops, **kw):
    return {r["system_id"]: r["jumps"] for r in systems_within_jumps(origin, rng, hops, **kw)}


def test_one_jump_matches_single_hop(chain):
    r = _reach(1, 6.0, 1)          # range 6 ly: only A (5 ly) is within one jump
    assert r == {2: 1}


def test_two_and_three_jumps_extend_the_chain(chain):
    assert _reach(1, 6.0, 2) == {2: 1, 3: 2}        # +B via A
    assert _reach(1, 6.0, 3) == {2: 1, 3: 2, 4: 3}  # +C via B


def test_reports_fewest_jumps(chain):
    # A shortcut hop straight to B within one jump must report B as 1 jump, not 2.
    r = _reach(1, 11.0, 3)  # range 11 ly: origin reaches A(5) AND B(10) directly
    assert r[2] == 1 and r[3] == 1 and r[4] == 2  # C still 2 (via B, 5 ly)


def test_avoid_cuts_the_chain(chain):
    # Avoiding B makes C unreachable (its only path runs through B).
    r = _reach(1, 6.0, 3, avoid={3})
    assert r == {2: 1}


def test_require_stations_filters_results_not_traversal(chain):
    # Only C has a station; A and B are stationless but must still be traversable.
    SdeStation.objects.create(station_id=60000004, name="C Station", system_id=4)
    J._station_cache.clear()
    r = _reach(1, 6.0, 3, require_stations=True)
    assert r == {4: 3}  # C reached (chained THROUGH stationless A,B), A/B filtered out


def test_origin_without_coords_returns_none(db):
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "TestRegion"})
    clear_graph_cache()
    SdeSolarSystem.objects.create(system_id=9, region_id=10000001, name="Void",
                                  security=0.2, x=0.0, y=0.0, z=0.0)
    assert systems_within_jumps(9, 6.0, 3) is None


def test_max_jumps_zero_is_empty(chain):
    assert systems_within_jumps(1, 6.0, 0) == []


def test_view_multi_jump_annotates_rows(client, chain):
    resp = client.get(reverse("navigation:range_finder") + "?from=1&jumps=3&range=6")
    assert resp.status_code == 200
    assert resp.context["max_jumps"] == 3
    jumps_by_id = {r["system_id"]: r["jumps"] for r in resp.context["rows"]}
    assert jumps_by_id == {2: 1, 3: 2, 4: 3}


def test_range_override_is_clamped_no_dos(client, chain):
    # A hostile huge/inf range must be bounded, not O(n²)-collapse the graph build (H1).
    resp = client.get(reverse("navigation:range_finder") + "?from=1&jumps=5&range=1e400")
    assert resp.status_code == 200
    assert resp.context["range_ly"] <= MAX_JUMP_RANGE_LY


def test_get_graph_clamps_non_finite_range(chain):
    # inf resolves to the MAX-range graph (bounded), never a crash or complete graph.
    assert J._get_graph(math.inf)["count"] == J._get_graph(MAX_JUMP_RANGE_LY)["count"]


def test_graph_cache_is_bounded(chain):
    clear_graph_cache()
    for i in range(J._GRAPH_CACHE_MAX + 12):  # cycle many distinct range keys
        J._get_graph(1.0 + i * 0.01)
    assert len(J._graph_cache) <= J._GRAPH_CACHE_MAX
