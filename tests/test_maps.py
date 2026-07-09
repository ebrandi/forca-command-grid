"""Region maps: the top-down projection builder and the map views."""
from __future__ import annotations

import pytest

from apps.navigation.maps import VIEW, region_map, security_band, security_colour
from apps.sde.models import SdeConstellation, SdeRegion, SdeSolarSystem, SdeSystemJump


@pytest.fixture
def region(db):
    r = SdeRegion.objects.create(region_id=10000099, name="Testland")
    c = SdeConstellation.objects.create(constellation_id=20000099, region=r, name="TestConst")
    SdeSolarSystem.objects.create(system_id=30009001, region=r, constellation=c, name="Alpha",
                                  security=0.9, x=1e16, y=5e15, z=2e16)
    SdeSolarSystem.objects.create(system_id=30009002, region=r, constellation=c, name="Beta",
                                  security=-0.3, x=3e16, y=5e15, z=2e16)
    SdeSystemJump.objects.create(from_system_id=30009001, to_system_id=30009002)
    SdeSystemJump.objects.create(from_system_id=30009002, to_system_id=30009001)
    # A neighbouring region with a cross-region stargate from Alpha → Gamma. This makes
    # both regions "connected" (so the universe graph keeps them) and gives Alpha a
    # gateway out of Testland.
    r2 = SdeRegion.objects.create(region_id=10000098, name="Nextland")
    c2 = SdeConstellation.objects.create(constellation_id=20000098, region=r2, name="NextConst")
    SdeSolarSystem.objects.create(system_id=30009003, region=r2, constellation=c2, name="Gamma",
                                  security=0.5, x=9e16, y=5e15, z=2e16)
    SdeSystemJump.objects.create(from_system_id=30009001, to_system_id=30009003)
    SdeSystemJump.objects.create(from_system_id=30009003, to_system_id=30009001)
    return r


def test_security_helpers():
    assert security_band(0.9) == "highsec"
    assert security_band(0.3) == "lowsec"
    assert security_band(0.0) == "nullsec"
    assert security_colour(1.0).startswith("#")
    assert security_colour(-0.5) == "#8c1c1c"


@pytest.mark.django_db
def test_region_map_projection(region):
    m = region_map(region.region_id)
    assert m["region"]["name"] == "Testland"
    assert len(m["nodes"]) == 2
    assert len(m["edges"]) == 1  # both directions deduped to one undirected edge
    for n in m["nodes"]:
        assert 0 <= n["px"] <= VIEW and 0 <= n["py"] <= VIEW  # inside the viewBox
    alpha = next(n for n in m["nodes"] if n["name"] == "Alpha")
    assert alpha["band"] == "highsec" and alpha["colour"].startswith("#")


@pytest.mark.django_db
def test_region_map_unknown():
    assert region_map(99999999) is None


@pytest.mark.django_db
def test_map_index_view(client, region):
    resp = client.get("/tools/map/")
    assert resp.status_code == 200 and "Testland" in resp.content.decode()


@pytest.mark.django_db
def test_map_region_view(client, region):
    resp = client.get(f"/tools/map/{region.region_id}/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Testland" in html and 'id="region-map"' in html and "Alpha" in html
    assert client.get("/tools/map/Testland/").status_code == 200  # by name too


@pytest.mark.django_db
def test_map_region_not_found(client, db):
    resp = client.get("/tools/map/Nowhere/")
    assert resp.status_code == 404 and "not found" in resp.content.decode()


# --- Overlays + bridges + highlight -----------------------------------------
def test_heat_colour():
    from apps.navigation.maps import heat_colour
    assert heat_colour(0, 10) == "#39435a"          # no activity → dim
    assert heat_colour(10, 10).startswith("#")      # max → a bright colour


@pytest.mark.django_db
def test_overlay_security_default(region):
    m = region_map(region.region_id, overlay="security")
    assert m["overlay"] == "security"
    assert all(n["label"].startswith("sec ") for n in m["nodes"])


@pytest.mark.django_db
def test_overlay_our_kills(region):
    from django.utils import timezone

    from apps.killboard.models import Killmail
    Killmail.objects.create(killmail_id=1, killmail_hash="h", killmail_time=timezone.now(),
                            solar_system_id=30009001, victim_ship_type_id=587,
                            involves_home_corp=True, home_corp_role="attacker")
    m = region_map(region.region_id, overlay="kills")
    alpha = next(n for n in m["nodes"] if n["id"] == 30009001)
    assert "1 kills" in alpha["label"]


@pytest.mark.django_db
def test_overlay_incursions(region, monkeypatch):
    monkeypatch.setattr("apps.navigation.services.incursion_systems", lambda: {30009001})
    m = region_map(region.region_id, overlay="incursions")
    alpha = next(n for n in m["nodes"] if n["id"] == 30009001)
    assert alpha["label"] == "Incursion" and alpha["colour"] == "#f0533f"


@pytest.mark.django_db
def test_map_bridges_drawn(region):
    from apps.navigation.models import AnsiblexBridge
    AnsiblexBridge.objects.create(from_system_id=30009001, to_system_id=30009002, active=True)
    assert len(region_map(region.region_id)["bridges"]) == 1


def test_sovereignty_parser(monkeypatch):
    from apps.navigation import map_overlays
    monkeypatch.setattr(map_overlays, "_fetch", lambda p, k: {"solar_systems": [
        {"solar_system_id": 30009001, "claim": {"alliance": {"alliance_id": 99000001}}},
        {"solar_system_id": 30009002, "claim": {"faction": {"faction_id": 500007}}},
    ]})
    sov = map_overlays.sovereignty()
    assert sov[30009001]["alliance_id"] == 99000001
    assert sov[30009002]["faction_id"] == 500007


@pytest.mark.django_db
def test_overlay_sov_colours(region, monkeypatch):
    monkeypatch.setattr("core.esi.names.resolve_ids", lambda ids: 0)
    monkeypatch.setattr("apps.navigation.map_overlays.sovereignty",
                        lambda: {30009001: {"alliance_id": 99000001, "corporation_id": None, "faction_id": None}})
    m = region_map(region.region_id, overlay="sov")
    alpha = next(n for n in m["nodes"] if n["id"] == 30009001)
    beta = next(n for n in m["nodes"] if n["id"] == 30009002)
    assert alpha["colour"] != "#39435a"   # held → a distinct colour
    assert beta["label"] == "Unclaimed"   # not in the sov map


@pytest.mark.django_db
def test_map_region_overlay_and_highlight(client, region):
    resp = client.get(f"/tools/map/{region.region_id}/?overlay=kills&hl=30009001")
    assert resp.status_code == 200
    assert resp.context["overlay"] == "kills" and 30009001 in resp.context["highlight"]
    assert "Our activity" in resp.content.decode()  # switcher present


@pytest.mark.django_db
def test_overlay_fw(region, monkeypatch):
    monkeypatch.setattr("apps.navigation.map_overlays.faction_warfare", lambda: {
        30009001: {"owner": 500004, "occupier": 500004, "contested": "uncontested",
                   "vp": 0, "threshold": 75000},
        30009002: {"owner": 500004, "occupier": 500003, "contested": "contested",
                   "vp": 37500, "threshold": 75000},
    })
    m = region_map(region.region_id, overlay="fw")
    alpha = next(n for n in m["nodes"] if n["id"] == 30009001)
    beta = next(n for n in m["nodes"] if n["id"] == 30009002)
    assert alpha["colour"] == "#3a8f6a" and "Gallente" in alpha["label"]  # quiet, held
    assert beta["colour"] == "#f0533f" and "50%" in beta["label"]         # contested front


@pytest.mark.django_db
def test_overlay_constellation(region):
    m = region_map(region.region_id, overlay="constellation")
    alpha = next(n for n in m["nodes"] if n["id"] == 30009001)
    assert alpha["label"] == "TestConst" and alpha["colour"] != "#39435a"


# --- Universe map (region graph) --------------------------------------------
@pytest.mark.django_db
def test_universe_map(region):
    from apps.navigation.maps import universe_map
    u = universe_map()
    # Both regions are wired together by the Alpha→Gamma stargate, so both appear as
    # nodes with one connecting edge.
    names = {n["name"] for n in u["nodes"]}
    assert names == {"Testland", "Nextland"} and len(u["edges"]) == 1
    for n in u["nodes"]:
        assert 0 <= n["px"] <= u["view"] and n["r"] > 0 and n["colour"].startswith("#")


@pytest.mark.django_db
def test_universe_map_drops_isolated(region):
    """A region with no inter-region link (and not Pochven) is dropped from the graph."""
    from apps.navigation.maps import universe_map
    SdeRegion.objects.create(region_id=10000050, name="Lonely")
    SdeSolarSystem.objects.create(system_id=30009099, region_id=10000050, name="Hermit",
                                  security=0.0, x=2e17, y=0.0, z=2e17)
    assert "Lonely" not in {n["name"] for n in universe_map()["nodes"]}


@pytest.mark.django_db
def test_map_index_shows_universe(client, region):
    resp = client.get("/tools/map/")
    assert resp.status_code == 200 and 'id="universe-map"' in resp.content.decode()


# --- Inter-region gateways --------------------------------------------------
@pytest.mark.django_db
def test_region_map_gateways(region):
    m = region_map(region.region_id)
    alpha = next(n for n in m["nodes"] if n["id"] == 30009001)
    beta = next(n for n in m["nodes"] if n["id"] == 30009002)
    assert alpha.get("gateways") == ["Nextland"]   # Alpha gates out to Nextland
    assert "gateways" not in beta                  # Beta only links inside the region


# --- System detail page -----------------------------------------------------
@pytest.mark.django_db
def test_system_facts(region):
    from apps.navigation.system_info import system_facts
    f = system_facts(30009001)
    assert f["system"]["name"] == "Alpha" and f["system"]["band"] == "highsec"
    assert f["gateways"] == ["Nextland"]
    assert any(n["external"] and n["name"] == "Gamma" for n in f["neighbours"])
    assert f["ores"]  # high-sec ore reference present


@pytest.mark.django_db
def test_system_detail_view(client, region, monkeypatch):
    monkeypatch.setattr("apps.navigation.map_overlays.system_topology",
                        lambda sid: {"planets": 5, "moons": 12, "belts": 3,
                                     "station_ids": [], "constellation_id": None, "name": "Alpha"})
    monkeypatch.setattr("apps.navigation.map_overlays.system_jumps", lambda: {30009001: 42})
    monkeypatch.setattr("apps.navigation.map_overlays.system_kills",
                        lambda: {30009001: {"ship_kills": 1, "pod_kills": 0, "npc_kills": 80}})
    monkeypatch.setattr("apps.navigation.views.incursion_systems", lambda: set())
    monkeypatch.setattr("apps.navigation.map_overlays.sovereignty", lambda: {})
    # Loaded celestials show named planets/moons instead of the ESI counts.
    from apps.sde.models import SdeCelestial
    SdeCelestial.objects.create(item_id=701, system_id=30009001, kind=SdeCelestial.Kind.PLANET,
                                name="Alpha I", celestial_index=1)
    SdeCelestial.objects.create(item_id=702, system_id=30009001, kind=SdeCelestial.Kind.MOON,
                                name="Alpha I - Moon 1", parent_planet_id=701)

    resp = client.get("/tools/system/30009001/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Alpha" in html and "Nextland" in html and "Gamma" in html
    assert "Planets &amp; moons" in html and "Alpha I" in html  # named celestials rendered

    assert client.get("/tools/system/Nope/").status_code == 404


# --- Roaming targets --------------------------------------------------------
@pytest.mark.django_db
def test_roaming_targets(region, monkeypatch):
    monkeypatch.setattr("apps.navigation.map_overlays.system_kills", lambda: {
        30009002: {"ship_kills": 0, "pod_kills": 0, "npc_kills": 60},   # quiet ratting
        30009001: {"ship_kills": 5, "pod_kills": 1, "npc_kills": 60},   # contested
    })
    monkeypatch.setattr("apps.navigation.map_overlays.system_jumps", lambda: {})
    from apps.navigation.roaming import roaming_targets
    rows = roaming_targets(band="all", limit=10)
    # Both have 60 NPC kills, but Beta is undefended → higher score → ranked first.
    assert rows[0]["system_id"] == 30009002 and rows[0]["score"] > rows[1]["score"]
    assert rows[1]["contested"] is True


# --- Combined multi-region route map ----------------------------------------
@pytest.mark.django_db
def test_route_map_builder(region):
    from apps.navigation.maps import route_map
    m = route_map([30009001, 30009003])   # Alpha (Testland) → Gamma (Nextland)
    assert m["start"] == "Alpha" and m["end"] == "Gamma"
    assert m["jumps"] == 1 and m["region_count"] == 2
    assert {r["name"] for r in m["regions"]} == {"Testland", "Nextland"}
    route_nodes = {n["id"] for n in m["nodes"] if n["route"]}
    assert route_nodes == {30009001, 30009003}
    assert any(not n["route"] and n["id"] == 30009002 for n in m["nodes"])  # Beta = context
    assert m["route_points"].count(",") == 2  # two "x,y" points
    assert m["start_id"] == 30009001 and m["end_id"] == 30009003


@pytest.mark.django_db
def test_route_map_builder_empty():
    from apps.navigation.maps import route_map
    assert route_map([]) is None
    assert route_map([99999999]) is None  # unknown id


@pytest.mark.django_db
def test_route_map_view(client, region):
    resp = client.get("/tools/route-map/?sys=30009001,30009003")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'id="route-map"' in html and "Alpha" in html and "Gamma" in html
    assert "planned route" in html
    # No ids → a friendly empty state, still 200.
    assert "No route to draw" in client.get("/tools/route-map/").content.decode()


# --- Route drawn on the region map ------------------------------------------
@pytest.mark.django_db
def test_map_region_route(client, region):
    resp = client.get(f"/tools/map/{region.region_id}/?route=30009001,30009002")
    assert resp.status_code == 200
    assert len(resp.context["route_segments"]) == 1   # Alpha→Beta leg is in-region
    assert 30009001 in resp.context["highlight"]      # route systems highlighted
    assert "planned route" in resp.content.decode()


# --- Jump-range reach map ---------------------------------------------------
@pytest.mark.django_db
def test_range_map_builder(region):
    from apps.navigation.maps import range_map
    m = range_map(30009001, [30009002, 30009003])  # Alpha origin, Beta + Gamma in range
    assert m["origin"] == "Alpha" and m["origin_id"] == 30009001
    assert m["count"] == 2 and m["region_count"] == 2
    assert len(m["spokes"]) == 2                       # one spoke per in-range system
    origin = next(n for n in m["nodes"] if n["origin"])
    assert origin["id"] == 30009001
    assert {n["id"] for n in m["nodes"] if not n["origin"]} == {30009002, 30009003}
    for n in m["nodes"]:
        assert 0 <= n["px"] <= VIEW and 0 <= n["py"] <= VIEW
    # Spokes fan out from the origin's projected position.
    assert m["origin_x"] == origin["px"] and m["origin_y"] == origin["py"]


@pytest.mark.django_db
def test_range_map_builder_dedupes_and_ignores_origin_in_list(region):
    from apps.navigation.maps import range_map
    # Origin repeated in the id list must not become its own spoke/duplicate node.
    m = range_map(30009001, [30009001, 30009002, 30009002])
    assert m["count"] == 1 and len(m["spokes"]) == 1
    assert sum(1 for n in m["nodes"] if n["origin"]) == 1


@pytest.mark.django_db
def test_range_map_builder_bad_origin():
    from apps.navigation.maps import range_map
    assert range_map(99999999, [99999999]) is None    # unknown origin


@pytest.mark.django_db
def test_range_map_view(client, region):
    resp = client.get("/tools/range-map/?from=30009001&sys=30009002,30009003")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'id="range-map"' in html and "Alpha" in html and "Beta" in html
    assert "in range" in html
    # No origin → a friendly empty state, still 200.
    assert "No systems to draw" in client.get("/tools/range-map/").content.decode()
