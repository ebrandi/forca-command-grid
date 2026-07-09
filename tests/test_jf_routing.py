"""Jump-freighter routing: range, proximity-graph BFS, route facts, pricing."""
from __future__ import annotations

import pytest

from apps.logistics.jumps import (
    LIGHT_YEAR_M,
    clear_graph_cache,
    jump_range_ly,
    jump_route,
)
from apps.logistics.models import RateCard
from apps.logistics.pricing import quote
from apps.logistics.routing import RouteUnavailable, jf_route_facts
from apps.sde.models import SdeRegion, SdeSolarSystem

LY = LIGHT_YEAR_M


def _sys(sid, x_ly, *, sec=0.0, region_id=10000001):
    # A constant non-zero y keeps systems off the galactic origin (0,0,0 means
    # "no coordinates"); distances along x are unaffected since y is shared.
    return SdeSolarSystem.objects.create(
        system_id=sid, region_id=region_id, name=f"S{sid}", security=sec,
        x=x_ly * LY, y=5.0 * LY, z=0.0,
    )


@pytest.fixture
def chain(db):
    # Four null-sec systems on a line, 6 ly apart: S1—S2—S3—S4.
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "R"})
    clear_graph_cache()
    for sid, x in ((1, 0), (2, 6), (3, 12), (4, 18)):
        _sys(sid, x)
    return None


# --- Range ------------------------------------------------------------------
def test_jump_range_ly():
    assert jump_range_ly(0) == 5.0
    assert jump_range_ly(3) == 8.0   # JDC III
    assert jump_range_ly(4) == 9.0   # JDC IV
    assert jump_range_ly(5) == 10.0  # JDC V
    assert jump_range_ly(9) == 10.0  # capped at V


# --- BFS hop counting -------------------------------------------------------
@pytest.mark.django_db
def test_bfs_fewest_hops(chain):
    r = jump_route(1, 4, 8.0, use_cache=False)
    assert r["jumps"] == 3 and r["path"] == [1, 2, 3, 4]
    assert round(r["ly"]) == 18


@pytest.mark.django_db
def test_longer_range_means_fewer_hops(chain):
    # At 13 ly, S1 reaches S3 directly (12 ly) → 2 hops instead of 3.
    r = jump_route(1, 4, 13.0, use_cache=False)
    assert r["jumps"] == 2


@pytest.mark.django_db
def test_same_system_is_zero_hops(chain):
    assert jump_route(2, 2, 10.0, use_cache=False)["jumps"] == 0


@pytest.mark.django_db
def test_no_path_when_out_of_range(db):
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "R"})
    clear_graph_cache()
    _sys(1, 0)
    _sys(2, 100)  # 100 ly away, nothing between
    assert jump_route(1, 2, 8.0, use_cache=False) is None


@pytest.mark.django_db
def test_highsec_not_used_as_intermediate(db):
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "R"})
    clear_graph_cache()
    _sys(10, 0)                 # null endpoint
    _sys(11, 16)                # null endpoint, 16 ly away
    _sys(12, 8, sec=0.9)        # high-sec bridge in the middle — must be ignored
    # The only midpoint is high-sec, so no cyno route exists at 9 ly range.
    assert jump_route(10, 11, 9.0, use_cache=False) is None


@pytest.mark.django_db
def test_display_highsec_excluded_as_intermediate(db):
    # True-sec 0.46 displays as "0.5" and is HIGH-SEC — no cyno, must not be a hop.
    # (The Ebasez bug: it was wrongly included by a < 0.5 filter.)
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "R"})
    clear_graph_cache()
    _sys(30, 0)
    _sys(31, 16)
    _sys(32, 8, sec=0.46)  # bridge that looks like 0.5 — high-sec, excluded
    assert jump_route(30, 31, 9.0, use_cache=False) is None


@pytest.mark.django_db
def test_lowsec_044_used_as_intermediate(db):
    # True-sec 0.44 displays as "0.4" = low-sec → cyno OK → it bridges.
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "R"})
    clear_graph_cache()
    _sys(40, 0)
    _sys(41, 16)
    _sys(42, 8, sec=0.44)
    r = jump_route(40, 41, 9.0, use_cache=False)
    assert r is not None and r["jumps"] == 2


@pytest.mark.django_db
def test_capital_rejects_highsec_endpoint(chain):
    _sys(50, 6, sec=0.6)  # high-sec destination 6 ly from S1
    clear_graph_cache()
    # True capital: can't jump into high-sec → no route.
    assert jump_route(1, 50, 10.0, use_cache=False, allow_highsec_endpoints=False) is None
    # Jump freighter: reaches a high-sec endpoint (gates the last leg).
    assert jump_route(1, 50, 10.0, use_cache=False, allow_highsec_endpoints=True) is not None


@pytest.mark.django_db
def test_pochven_not_used_as_intermediate(db):
    # Pochven (region 10000070) is jump-isolated — never a cyno hop (the Angymonne bug).
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "R"})
    SdeRegion.objects.get_or_create(region_id=10000070, defaults={"name": "Pochven"})
    clear_graph_cache()
    _sys(90, 0)
    _sys(91, 16)
    _sys(92, 8, region_id=10000070)  # Pochven bridge — excluded
    assert jump_route(90, 91, 9.0, use_cache=False) is None


@pytest.mark.django_db
def test_pochven_endpoint_rejected(chain):
    SdeRegion.objects.get_or_create(region_id=10000070, defaults={"name": "Pochven"})
    _sys(93, 6, region_id=10000070)  # Pochven destination 6 ly from S1
    clear_graph_cache()
    # Can't jump into Pochven, even for a JF allowed high-sec endpoints.
    assert jump_route(1, 93, 10.0, use_cache=False, allow_highsec_endpoints=True) is None


@pytest.mark.django_db
def test_wormhole_not_used_as_intermediate(db):
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "R"})
    SdeRegion.objects.get_or_create(region_id=11000001, defaults={"name": "J"})  # W-space
    clear_graph_cache()
    _sys(20, 0)
    _sys(21, 16)
    _sys(22, 8, sec=-0.9, region_id=11000001)  # wormhole bridge — excluded
    assert jump_route(20, 21, 9.0, use_cache=False) is None


# --- Route facts + pricing --------------------------------------------------
@pytest.mark.django_db
def test_jf_route_facts(chain):
    facts = jf_route_facts(1, 4, 8.0)
    assert facts["jumps"] == 3
    assert facts["sec_band"] == "nullsec"
    assert facts["range_ly"] == 8.0


@pytest.mark.django_db
def test_jf_route_facts_unavailable(db):
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "R"})
    clear_graph_cache()
    _sys(1, 0)
    _sys(2, 100)
    with pytest.raises(RouteUnavailable):
        jf_route_facts(1, 2, 8.0)


@pytest.mark.django_db
def test_jf_pricing_uses_cyno_hops(chain):
    facts = jf_route_facts(1, 4, 8.0)  # 3 hops
    card = RateCard()  # defaults: jf_base 200M, jf_per_jump 100M, discount 0.80
    q = quote(card, ship_class="jf", jumps=facts["jumps"], jump_hops=facts["jumps"],
              volume_m3=300_000, collateral=0, sec_band="nullsec")
    # 200M + 3×100M = 500M; ×0.80 = 400M
    assert int(q.reward) == 400_000_000
    assert q.breakdown["jump_hops"] == 3
    assert any("Cyno jumps × 3" in ln["label"] for ln in q.breakdown["lines"])
