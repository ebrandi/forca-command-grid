"""Navigation tools: jump-plan math, gate route plan, and the planner views."""
from __future__ import annotations

import json

import pytest
import responses
from django.core.cache import cache

from apps.logistics.jumps import (
    LIGHT_YEAR_M,
    clear_graph_cache,
    effective_range,
    jump_plan,
    jump_route,
    systems_in_range,
)
from apps.logistics.routing import RouteUnavailable, route_plan
from apps.sde.models import SdeRegion, SdeSolarSystem

LY = LIGHT_YEAR_M


def _sys(sid, x_ly, *, sec=0.0, name=None, region_id=10000001):
    return SdeSolarSystem.objects.create(
        system_id=sid, region_id=region_id, name=name or f"Sys{sid}", security=sec,
        x=x_ly * LY, y=5.0 * LY, z=0.0,
    )


@pytest.fixture
def chain(db):
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "Catch"})
    clear_graph_cache()
    cache.clear()
    for sid, x in ((1, 0), (2, 6), (3, 12), (4, 18)):
        _sys(sid, x)
    return None


# --- Range + jump-plan math -------------------------------------------------
def test_effective_range():
    assert effective_range(5.0, 5) == 10.0
    assert effective_range(3.5, 0) == 3.5
    assert round(effective_range(3.5, 5), 2) == 7.0


@pytest.mark.django_db
def test_jump_plan_hops_and_fuel(chain):
    # JF at 8 ly: 3 hops of 6 ly. No skills → full fuel (3100/ly).
    plan = jump_plan(1, 4, range_ly=8.0, fuel_per_ly=3100, fatigue_factor=0.10,
                     uses_jf_skill=True, jfc=0, jf_skill=0)
    assert plan["jumps"] == 3 and len(plan["hops"]) == 3
    assert round(plan["total_ly"]) == 18
    assert plan["hops"][0]["fuel"] == 18600          # ceil(6.0 × 3100)
    assert plan["total_fuel"] == 55800               # 3 × 18600
    assert plan["final_fatigue_min"] > 0


@pytest.mark.django_db
def test_jump_plan_skills_cut_fuel(chain):
    # JFC V (×0.5) + Jump Freighter V (×0.5) = ×0.25 of base fuel.
    plan = jump_plan(1, 4, range_ly=8.0, fuel_per_ly=3100, uses_jf_skill=True, jfc=5, jf_skill=5)
    assert plan["hops"][0]["fuel"] == 4650           # ceil(6.0 × 3100 × 0.25)
    assert plan["total_fuel"] == 13950


@pytest.mark.django_db
def test_jump_plan_rejects_highsec_endpoint(chain):
    _sys(60, 6, sec=0.6)  # high-sec destination
    clear_graph_cache()
    # The planner is for capitals — a high-sec endpoint has no cyno route.
    assert jump_plan(1, 60, range_ly=10.0, fuel_per_ly=1000) is None


@pytest.mark.django_db
def test_jump_planner_view_explains_highsec(client, chain):
    _sys(60, 6, sec=0.6, name="Ebasez")
    clear_graph_cache()
    resp = client.get("/tools/jump/?from=1&to=60&ship=carrier&jdc=5")
    assert resp.status_code == 200
    assert resp.context["result"] is None
    assert "high-sec" in resp.context["error"] and "Ebasez" in resp.context["error"]


@pytest.mark.django_db
def test_jump_freighter_highsec_origin_is_mixed_mode_not_direct_jump(client, chain):
    # A jump freighter can't cyno OUT of high-sec: a high-sec origin resolves to a
    # gate-out-then-jump route, never a direct cyno jump from high-sec. Without a
    # local gate graph here it can't find a staging system, so it errors cleanly
    # instead of faking an illegal jump into/out of high-sec.
    _sys(70, 6, sec=0.6, name="Amarr")
    clear_graph_cache()
    resp = client.get("/tools/jump/?from=70&to=1&ship=jf&jdc=5")
    assert resp.status_code == 200
    assert resp.context["result"] is None
    assert "staging" in resp.context["error"] or "high-sec" in resp.context["error"]
    # …and a carrier still can't reach a high-sec endpoint at all.
    carrier = client.get("/tools/jump/?from=1&to=70&ship=carrier&jdc=5")
    assert carrier.context["result"] is None and "high-sec" in carrier.context["error"]


@pytest.mark.django_db
def test_jump_plan_none_when_unreachable(db):
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "C"})
    clear_graph_cache()
    _sys(1, 0)
    _sys(2, 100)
    assert jump_plan(1, 2, range_ly=8.0, fuel_per_ly=3100) is None


# --- Gate route plan --------------------------------------------------------
@responses.activate
@pytest.mark.django_db
def test_route_plan_detail(chain):
    responses.add(
        responses.POST, "https://esi.evetech.net/route/1/4/",
        body=json.dumps({"route": [1, 2, 3, 4]}), status=200, content_type="application/json",
    )
    cache.clear()
    r = route_plan(1, 4, "safer")
    assert r["jumps"] == 3
    assert [s["system_id"] for s in r["systems"]] == [1, 2, 3, 4]
    assert r["systems"][0]["band"] == "nullsec" and r["systems"][0]["region"] == "Catch"


@responses.activate
@pytest.mark.django_db
def test_route_plan_unavailable(chain):
    responses.add(
        responses.POST, "https://esi.evetech.net/route/1/4/",
        body=json.dumps({"route": []}), status=200, content_type="application/json",
    )
    cache.clear()
    with pytest.raises(RouteUnavailable):
        route_plan(1, 4, "shortest")


# --- Avoidance, stations, incursions, range finder --------------------------
@responses.activate
@pytest.mark.django_db
def test_incursion_systems(db):
    from apps.navigation.services import incursion_systems
    cache.clear()
    responses.add(
        responses.GET, "https://esi.evetech.net/incursions/",
        body=json.dumps([{"infested_solar_systems": [1, 2, 3]}]), status=200,
        content_type="application/json",
    )
    assert incursion_systems() == {1, 2, 3}


@pytest.mark.django_db
def test_resolve_avoidance(chain):
    from apps.navigation.services import resolve_avoidance
    avoid, unresolved = resolve_avoidance("Sys2, Nope", "Catch")
    assert 2 in avoid          # named system
    assert 1 in avoid          # Sys1 is in region Catch (expanded)
    assert "Nope" in unresolved


@pytest.mark.django_db
def test_jump_route_avoid(chain):
    # Avoiding S2 breaks the 8 ly chain (1—2—3—4) entirely.
    assert jump_route(1, 4, 8.0, use_cache=False, avoid={2}) is None
    # At 13 ly, S1 reaches S3 directly, so it routes around S2.
    r = jump_route(1, 4, 13.0, use_cache=False, avoid={2})
    assert r is not None and 2 not in r["path"]


@pytest.mark.django_db
def test_systems_in_range(chain):
    rows = systems_in_range(1, 13.0)  # S2 (6 ly) + S3 (12 ly); S4 (18) out of range
    assert {r["system_id"] for r in rows} == {2, 3}
    assert systems_in_range(1, 13.0, avoid={2})[0]["system_id"] == 3


@pytest.mark.django_db
def test_require_stations(chain):
    from apps.sde.models import SdeStation
    SdeStation.objects.create(station_id=60000001, name="St", system_id=2)
    clear_graph_cache()  # also clears the station cache
    rows = systems_in_range(1, 13.0, require_stations=True)
    assert {r["system_id"] for r in rows} == {2}  # only S2 is dockable


@responses.activate
@pytest.mark.django_db
def test_jump_planner_avoid_incursions(client, chain):
    cache.clear()
    responses.add(
        responses.GET, "https://esi.evetech.net/incursions/",
        body=json.dumps([{"infested_solar_systems": [2]}]), status=200,
        content_type="application/json",
    )
    # S2 under incursion → avoiding it breaks the 8 ly chain → no route.
    resp = client.get("/tools/jump/?from=1&to=4&ship=jf&jdc=3&avoid_incursions=1")
    assert resp.status_code == 200
    assert resp.context["result"] is None and resp.context["error"]


@pytest.mark.django_db
def test_range_finder_view(client, chain):
    resp = client.get("/tools/range/?from=1&ship=jf&jdc=5&range=13")
    assert resp.status_code == 200
    assert {r["system_id"] for r in resp.context["rows"]} == {2, 3}
    # Results offer a reach map linking the origin + every in-range system.
    link = resp.context["map_link"]
    assert link and "from=1" in link and "sys=2,3" in link
    assert "View range map" in resp.content.decode()


# --- Waypoints (multi-leg) --------------------------------------------------
@pytest.mark.django_db
def test_jump_plan_multi_forces_waypoint(chain):
    from apps.logistics.jumps import jump_plan_multi
    # At 13 ly S1→S4 could skip S2, but a waypoint forces the route through it.
    plan = jump_plan_multi([1, 2, 4], range_ly=13.0, fuel_per_ly=1000)
    assert plan is not None and 2 in plan["path"]


@responses.activate
@pytest.mark.django_db
def test_route_plan_multi_stitches_legs(chain):
    from apps.logistics.routing import route_plan_multi
    cache.clear()
    responses.add(responses.POST, "https://esi.evetech.net/route/1/2/",
                  body=json.dumps({"route": [1, 2]}), status=200, content_type="application/json")
    responses.add(responses.POST, "https://esi.evetech.net/route/2/4/",
                  body=json.dumps({"route": [2, 3, 4]}), status=200, content_type="application/json")
    r = route_plan_multi([1, 2, 4], "shortest")
    assert r["jumps"] == 3  # 1 + 2
    assert [s["system_id"] for s in r["systems"]] == [1, 2, 3, 4]  # boundary not duplicated


@pytest.mark.django_db
def test_jump_planner_view_waypoints(client, chain):
    resp = client.get("/tools/jump/?from=1&to=4&ship=jf&jdc=5&range=13&waypoints=Sys2")
    assert resp.status_code == 200
    assert 2 in resp.context["result"]["map_ids"]


# --- Ansiblex jump-bridge network -------------------------------------------
@pytest.mark.django_db
def test_ansiblex_connections(db):
    from apps.navigation.models import AnsiblexBridge
    from apps.navigation.services import ansiblex_connections
    AnsiblexBridge.objects.create(from_system_id=10, to_system_id=20, active=True)
    AnsiblexBridge.objects.create(from_system_id=30, to_system_id=40, active=False)
    conns = ansiblex_connections()
    assert {"from": 10, "to": 20} in conns and {"from": 20, "to": 10} in conns  # both ways
    assert {"from": 30, "to": 40} not in conns      # inactive excluded


@responses.activate
@pytest.mark.django_db
def test_route_plan_sends_connections(chain):
    from apps.logistics.routing import route_plan
    cache.clear()
    responses.add(responses.POST, "https://esi.evetech.net/route/1/4/",
                  body=json.dumps({"route": [1, 4]}), status=200, content_type="application/json")
    route_plan(1, 4, "shortest", connections=[{"from": 1, "to": 4}, {"from": 4, "to": 1}])
    body = json.loads(responses.calls[-1].request.body)
    assert body["connections"] == [{"from": 1, "to": 4}, {"from": 4, "to": 1}]


@pytest.mark.django_db
def test_beacons_management(client, django_user_model, chain):
    from apps.identity.models import RoleAssignment
    from apps.navigation.models import AnsiblexBridge
    from apps.sso.services import ensure_role
    from core import rbac

    member = django_user_model.objects.create(username="eve:nav1")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    r = client.get("/tools/beacons/")
    assert r.status_code == 200 and r.context["can_manage"] is False
    assert client.post("/tools/beacons/add/", {"from": "2", "to": "3"}).status_code == 403

    client.logout()
    officer = django_user_model.objects.create(username="eve:nav2")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    assert client.post("/tools/beacons/add/", {"from": "2", "to": "3"}).status_code == 302
    bridge = AnsiblexBridge.objects.get()
    assert {bridge.from_system_id, bridge.to_system_id} == {2, 3}
    assert client.post(f"/tools/beacons/{bridge.pk}/remove/").status_code == 302
    assert AnsiblexBridge.objects.count() == 0


# --- ESI sync (Ansiblex + cyno beacons) -------------------------------------
@pytest.mark.django_db
def test_parse_bridge_systems(sde):
    from apps.navigation.esi_sync import _parse_bridge_systems
    # Jita 30000142, Otitoh 30002053 (from the SDE sample).
    assert set(_parse_bridge_systems("BRAVE Jita » Otitoh - Ansiblex", 30000142)) == {30000142, 30002053}
    assert _parse_bridge_systems("nothing here", 30000142) is None


@responses.activate
@pytest.mark.django_db
def test_sync_jump_network(monkeypatch, settings, sde):
    from apps.corporation.models import EveCorporation
    from apps.navigation import esi_sync
    from apps.navigation.models import AnsiblexBridge, CynoBeacon
    from apps.sso.models import EveCharacter

    settings.FORCA_HOME_CORP_ID = 98000001
    EveCorporation.objects.create(corporation_id=98000001, name="Home")
    char = EveCharacter.objects.create(character_id=5000, name="Dir", corporation_id=98000001, is_corp_member=True)
    monkeypatch.setattr(esi_sync, "_token_character", lambda corp_id: char)
    monkeypatch.setattr(esi_sync, "get_valid_access_token", lambda c, s: "tok")
    responses.add(
        responses.GET, "https://esi.evetech.net/corporations/98000001/structures/",
        json=[
            {"structure_id": 1001, "type_id": 35841, "system_id": 30000142, "name": "Jita » Otitoh - Ansiblex"},
            {"structure_id": 1002, "type_id": 35840, "system_id": 30002053, "name": "Otitoh Beacon"},
            {"structure_id": 1003, "type_id": 99999, "system_id": 30000142, "name": "Citadel"},
        ],
        status=200, headers={"X-Pages": "1"},
    )
    res = esi_sync.sync_jump_network()
    assert res["status"] == "ok" and res["bridges"] == 1 and res["beacons"] == 1
    bridge = AnsiblexBridge.objects.get()
    assert {bridge.from_system_id, bridge.to_system_id} == {30000142, 30002053}
    assert bridge.source == "esi" and bridge.structure_id == 1001
    assert CynoBeacon.objects.get().system_id == 30002053


@pytest.mark.django_db
def test_beacon_sync_access(client, django_user_model, settings):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    settings.FORCA_HOME_CORP_ID = 98000001
    member = django_user_model.objects.create(username="eve:bs1")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.post("/tools/beacons/sync/").status_code == 403  # officer-only

    client.logout()
    officer = django_user_model.objects.create(username="eve:bs2")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    # No Director token configured → graceful no-scope, still redirects.
    assert client.post("/tools/beacons/sync/").status_code == 302


# --- Views ------------------------------------------------------------------
@pytest.mark.django_db
def test_system_search_view(client, chain):
    data = client.get("/tools/systems/?q=Sys").json()
    assert any(d["name"] == "Sys1" for d in data)
    assert all("type_id" in d and "name" in d for d in data)


@pytest.mark.django_db
def test_planner_views_render(client):
    route = client.get("/tools/route/")
    assert route.status_code == 200 and "Route planner" in route.content.decode()
    jump = client.get("/tools/jump/")
    assert jump.status_code == 200
    html = jump.content.decode()
    assert "Jump planner" in html and "Jump Freighter" in html  # ship preset present


@pytest.mark.django_db
def test_jump_planner_computes_route(client, chain):
    # JDC III → 8 ly range → 3 hops across the chain.
    resp = client.get("/tools/jump/?from=1&to=4&ship=jf&jdc=3")
    assert resp.status_code == 200
    assert resp.context["result"]["summary"]["cyno_jumps"] == 3
    assert resp.context["range_ly"] == 8.0


@responses.activate
@pytest.mark.django_db
def test_route_planner_computes_route(client, chain):
    responses.add(
        responses.POST, "https://esi.evetech.net/route/1/4/",
        body=json.dumps({"route": [1, 2, 3, 4]}), status=200, content_type="application/json",
    )
    cache.clear()
    resp = client.get("/tools/route/?from=1&to=4&pref=safer")
    assert resp.status_code == 200
    assert resp.context["result"]["jumps"] == 3
