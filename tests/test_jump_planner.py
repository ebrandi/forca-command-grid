"""Jump Planner rework: ship capability model, route-mode resolver, high-sec exit
planner, mixed-mode routing, and the corrected per-hull fuel calculation.

The dev/test DB carries no solar-system coordinates or stargate graph, so these
tests build a small synthetic universe (systems with coordinates + a
``SdeSystemJump`` gate graph) and mock ESI's ``/route`` for the gate legs.
"""
from __future__ import annotations

import json
import math

import pytest
import responses
from django.core.cache import cache

from apps.identity.models import RoleAssignment
from apps.logistics.jumps import (
    LIGHT_YEAR_M,
    SHIPS_BY_KEY,
    _fuel_multiplier,
    clear_graph_cache,
    jump_plan,
    profile_for,
)
from apps.logistics.ships import (
    HELIUM,
    HYDROGEN,
    NITROGEN,
    OXYGEN,
    SHIP_PROFILES,
    ShipProfile,
    jde_fuel_multiplier,
)
from apps.navigation.highsec_exit import (
    clear_gate_cache,
    nearest_lowsec,
    pair_entry_exit,
    rank_exits,
)
from apps.navigation.jump_service import plan_jump
from apps.navigation.route_mode import RouteMode, resolve_route_mode
from apps.sde.models import SdeRegion, SdeSolarSystem, SdeStation, SdeSystemJump
from apps.sso.services import ensure_role
from core import rbac

LY = LIGHT_YEAR_M
ESI = "https://esi.evetech.net"


# --- 1. Ship capability model ----------------------------------------------
def test_isotope_types_per_hull():
    assert SHIP_PROFILES["ark"].isotope_type_id == HELIUM
    assert SHIP_PROFILES["rhea"].isotope_type_id == NITROGEN
    assert SHIP_PROFILES["anshar"].isotope_type_id == OXYGEN
    assert SHIP_PROFILES["nomad"].isotope_type_id == HYDROGEN
    assert SHIP_PROFILES["rorqual"].isotope_type_id == OXYGEN


def test_real_fuel_and_range_data():
    # The old table was wrong; these are the real SDE dogma values.
    assert SHIP_PROFILES["rhea"].base_fuel_per_ly == 10000.0
    assert SHIP_PROFILES["nomad"].base_fuel_per_ly == 8200.0
    assert SHIP_PROFILES["redeemer"].base_fuel_per_ly == 700.0
    assert SHIP_PROFILES["rorqual"].base_fuel_per_ly == 4000.0
    assert SHIP_PROFILES["archon"].base_fuel_per_ly == 3000.0
    # Range fixes: Titan is 3.0 ly (was wrongly 6.0), FAX 3.5 (was 3.75).
    assert SHIP_PROFILES["avatar"].base_range_ly == 3.0
    assert SHIP_PROFILES["apostle"].base_range_ly == 3.5


def test_capability_flags():
    rhea, archon, nyx = profile_for("rhea"), profile_for("archon"), profile_for("nyx")
    assert rhea.reaches_highsec is True and rhea.jf_skill is True and rhea.rig_eligible is True
    # True capitals can use low/null gates but not high-sec; supers no gates at all.
    assert archon.can_use_gates is True and archon.reaches_highsec is False
    assert nyx.can_use_gates is False and nyx.reaches_highsec is False
    # No hull can cyno into or start a jump from high-sec.
    assert rhea.can_cyno_into_highsec is False and rhea.can_start_from_highsec is False


def test_back_compat_dict_access():
    # store/forecast + freight still do SHIPS_BY_KEY["jf"]["range"].
    assert SHIPS_BY_KEY["jf"]["range"] == 5.0
    assert SHIPS_BY_KEY["carrier"].base_range_ly == 3.5


# --- 2. Fuel calculation (DOTLAN-validated fixtures) ------------------------
def test_fuel_fixture_rhea():
    # Rhea, 5 ly, JFC V, JF V -> 12,500 Nitrogen (5 * 10000 * 0.5 * 0.5).
    mult = _fuel_multiplier(5, True, 5)
    assert math.ceil(5.0 * 10000 * mult) == 12500


def test_fuel_fixture_redeemer():
    # Redeemer (Black Ops), 4 ly, JFC V -> 1,400 Helium. JF skill must NOT apply.
    red = profile_for("redeemer")
    mult = _fuel_multiplier(5, red.jf_skill, 5)
    assert red.jf_skill is False
    assert math.ceil(4.0 * red.base_fuel_per_ly * mult) == 1400


def test_fuel_fixture_archon():
    # Archon (Carrier), 3.5 ly, JFC IV -> 6,300 Helium.
    arc = profile_for("archon")
    mult = _fuel_multiplier(4, arc.jf_skill, 5)
    assert round(3.5 * arc.base_fuel_per_ly * mult) == 6300


def test_jdc_does_not_affect_fuel():
    # Fuel multiplier is identical regardless of JDC (JDC affects range only).
    assert _fuel_multiplier(5, True, 5) == _fuel_multiplier(5, True, 5)  # no JDC arg at all


def test_jde_rigs_reduce_fuel_only_stacking_penalised():
    assert jde_fuel_multiplier(0) == 1.0
    assert jde_fuel_multiplier(1) == pytest.approx(0.9)          # 10% rig, full value
    assert jde_fuel_multiplier(2) == pytest.approx(0.9 * (1 - 0.07 * 0.869))
    # More rigs never increase fuel.
    assert jde_fuel_multiplier(3) < jde_fuel_multiplier(2)


# --- 3. Route-mode resolver -------------------------------------------------
def _p(key):
    return profile_for(key)


def test_resolver_jf_matrix():
    assert resolve_route_mode(0.3, 0.2, _p("rhea")).mode == RouteMode.JUMP_ONLY
    assert resolve_route_mode(0.3, 0.9, _p("rhea")).mode == RouteMode.JUMP_TO_LOWSEC_EXIT_THEN_GATE
    assert resolve_route_mode(0.9, 0.2, _p("rhea")).mode == RouteMode.GATE_TO_JUMP_ENTRY_THEN_JUMP
    assert resolve_route_mode(0.9, 0.9, _p("rhea")).mode == RouteMode.MIXED_GATE_AND_JUMP


def test_resolver_black_ops_is_separate():
    # Black Ops pure jump resolves to its own mode, distinct from a JF.
    res = resolve_route_mode(0.3, 0.2, _p("redeemer"))
    assert res.mode == RouteMode.BLACK_OPS_JUMP
    assert resolve_route_mode(0.3, 0.2, _p("rhea")).mode == RouteMode.JUMP_ONLY
    # Black Ops can still reach a high-sec destination on gates (mixed mode).
    assert resolve_route_mode(0.3, 0.9, _p("redeemer")).mode == RouteMode.JUMP_TO_LOWSEC_EXIT_THEN_GATE


def test_resolver_capital_highsec_is_invalid_not_faked():
    # A carrier can't enter high-sec at all -> INVALID, never a fake exit route.
    res = resolve_route_mode(0.3, 0.9, _p("archon"))
    assert res.mode == RouteMode.INVALID_FOR_DESTINATION and res.can_plan is False
    # …but low->low is a normal jump.
    assert resolve_route_mode(0.3, 0.2, _p("archon")).mode == RouteMode.JUMP_ONLY


def test_resolver_gate_only_for_no_jump_drive():
    freighter = ShipProfile(
        key="freighter", label="Freighter", type_id=20185, ship_class="freighter",
        class_label="Freighter", isotope_type_id=HELIUM, base_fuel_per_ly=0.0,
        base_range_ly=0.0, fatigue_factor=1.0, cyno_type="normal",
        has_jump_drive=False, can_use_gates=True, can_gate_highsec=True,
    )
    assert resolve_route_mode(0.9, 0.9, freighter).mode == RouteMode.GATE_ONLY


# --- 4. Synthetic universe for exit-planner + service tests -----------------
def _sys(sid, x_ly, sec, name, region_id=10000001):
    return SdeSolarSystem.objects.create(
        system_id=sid, region_id=region_id, name=name, security=sec,
        x=x_ly * LY, y=5.0 * LY, z=0.0,
    )


def _gate(a, b):
    SdeSystemJump.objects.create(from_system_id=a, to_system_id=b)
    SdeSystemJump.objects.create(from_system_id=b, to_system_id=a)


@pytest.fixture
def universe(db):
    """O(low) --jump-- L1/L2(low exits) --gate-- H1(high) --gate-- HDEST(high)."""
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "TestRegion"})
    clear_graph_cache()
    clear_gate_cache()
    cache.clear()
    _sys(1, 0, 0.3, "Origin")       # jump origin (low)
    _sys(2, 6, 0.2, "ExitOne")      # low-sec exit, 6 ly from origin
    _sys(3, 7, 0.1, "ExitTwo")      # low-sec exit, 7 ly from origin
    _sys(10, 100, 0.5, "HubBorder")  # high-sec, gate-linked to exits
    _sys(11, 101, 0.9, "Hub")        # high-sec destination
    SdeStation.objects.create(station_id=60000001, name="ExitOne Station", system_id=2)
    # Gate graph: exits border the high-sec pocket.
    _gate(2, 10)
    _gate(3, 10)
    _gate(10, 11)
    return None


def test_nearest_lowsec_by_gate_distance(universe):
    near = nearest_lowsec(11)  # from the high-sec destination
    names = {c["name"]: c["gate_jumps"] for c in near}
    assert names["ExitOne"] == 2 and names["ExitTwo"] == 2  # HDEST->H1->exit
    # Only cyno-capable (low/null) systems appear — never the high-sec ones.
    assert "Hub" not in names and "HubBorder" not in names


def test_rank_exits_prefers_reachable_dockable(universe):
    exits = rank_exits(11, 1, range_ly=10.0, prefer_stations=True)
    assert exits, "should find a reachable exit"
    assert exits[0]["name"] == "ExitOne"  # dockable, reachable within 10 ly
    assert exits[0]["has_station"] is True
    assert exits[0]["jump_jumps"] >= 1


def test_rank_exits_respects_avoid(universe):
    exits = rank_exits(11, 1, range_ly=10.0, avoid={2})
    assert all(c["system_id"] != 2 for c in exits)  # ExitOne avoided
    assert any(c["system_id"] == 3 for c in exits)   # ExitTwo still offered


def test_rank_exits_none_when_out_of_range(universe):
    # A 3 ly range can't reach either exit (6/7 ly away) -> no candidates.
    assert rank_exits(11, 1, range_ly=3.0) == []


def test_pair_entry_exit_both_highsec(universe):
    # Add a second high-sec pocket near the origin with its own low-sec entry.
    _sys(20, 1, 0.6, "OriginHub")   # high-sec origin near system 1
    _gate(1, 20)                     # low entry (1) borders high-sec origin (20)
    clear_gate_cache()
    pair = pair_entry_exit(20, 11, range_ly=200.0)
    assert pair is not None
    assert pair["entry"]["system_id"] == 1
    assert pair["exit"]["system_id"] in (2, 3)


# --- 5. Service: mixed-mode JF high-sec destination -------------------------
@responses.activate
def test_service_jf_highsec_dest_is_mixed_mode(universe):
    responses.add(responses.POST, f"{ESI}/route/2/11/",
                  body=json.dumps({"route": [2, 10, 11]}), status=200,
                  content_type="application/json")
    origin = SdeSolarSystem.objects.get(system_id=1)
    dest = SdeSolarSystem.objects.get(system_id=11)  # high-sec
    plan = plan_jump(origin, dest, profile_for("rhea"), jdc=5, jfc=5, jf_skill=5,
                     price_fn=lambda tid: None)
    assert plan["can_plan"] is True
    assert plan["mode"] == RouteMode.JUMP_TO_LOWSEC_EXIT_THEN_GATE
    # Two segments: a jump then a gate leg.
    kinds = [s["kind"] for s in plan["segments"]]
    assert kinds == ["jump", "gate"]
    # The TRUE destination stays visible (not the low-sec exit).
    assert plan["dest"]["name"] == "Hub" and plan["dest"]["system_id"] == 11
    # The jump leg never touches a high-sec system.
    jump_wp = [w["band"] for w in plan["segments"][0]["waypoints"]]
    assert "highsec" not in jump_wp
    # The gate leg ends at the true high-sec destination.
    assert plan["segments"][1]["systems"][-1]["system_id"] == 11


@responses.activate
def test_service_fuel_only_on_jump_legs(universe):
    responses.add(responses.POST, f"{ESI}/route/2/11/",
                  body=json.dumps({"route": [2, 10, 11]}), status=200,
                  content_type="application/json")
    origin = SdeSolarSystem.objects.get(system_id=1)
    dest = SdeSolarSystem.objects.get(system_id=11)
    plan = plan_jump(origin, dest, profile_for("rhea"), jdc=5, jfc=5, jf_skill=5,
                     price_fn=lambda tid: None)
    jump_fuel = sum(s["fuel"] for s in plan["segments"] if s["kind"] == "jump")
    assert plan["summary"]["fuel_units"] == jump_fuel
    assert plan["summary"]["gate_jumps"] == 2  # gate leg counted, but burns no fuel
    assert plan["summary"]["isotope_name"] == "Nitrogen Isotopes"


@responses.activate
def test_service_fuel_isk_and_safety_margin(universe):
    responses.add(responses.POST, f"{ESI}/route/2/11/",
                  body=json.dumps({"route": [2, 11]}), status=200,
                  content_type="application/json")
    origin = SdeSolarSystem.objects.get(system_id=1)
    dest = SdeSolarSystem.objects.get(system_id=11)
    plan = plan_jump(origin, dest, profile_for("rhea"), jdc=5, jfc=5, jf_skill=5,
                     safety_margin_pct=10.0, price_fn=lambda tid: 1000)
    base = plan["summary"]["fuel_units"]
    assert plan["summary"]["fuel_with_margin"] == math.ceil(base * 1.1)
    assert plan["summary"]["fuel_isk"] == 1000 * plan["summary"]["fuel_with_margin"]


@responses.activate
@pytest.mark.django_db
def test_dockable_only_falls_back_to_reachable_exit(db):
    # Regression (adversarial-review finding): with 'dockable only', the nearest
    # exit by gate distance (ExitA) is reachable only via a stationless bridge, so
    # the station-only plan can't route to it. The planner must fall back to a
    # station-reachable exit (ExitB), not report the trip impossible.
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "R"})
    clear_graph_cache()
    clear_gate_cache()
    cache.clear()
    _sys(1, 0, 0.3, "Origin")
    _sys(3, 8, 0.1, "ExitB")     # dockable, directly reachable from Origin (8 ly)
    _sys(4, 9, 0.2, "Bridge")    # stationless; ExitA's ONLY in-range neighbour
    _sys(2, 19, 0.2, "ExitA")    # dockable, but 10 ly past Bridge and >10 ly from any station
    _sys(10, 100, 0.5, "HBorder")
    _sys(14, 102, 0.6, "HBorder2")
    _sys(11, 101, 0.9, "Hub")
    SdeStation.objects.create(station_id=71, name="A", system_id=2)
    SdeStation.objects.create(station_id=72, name="B", system_id=3)
    _gate(2, 10)
    _gate(10, 11)   # ExitA -> Hub = 2 gates
    _gate(3, 14)
    _gate(14, 10)   # ExitB -> Hub = 3 gates
    responses.add(responses.POST, f"{ESI}/route/3/11/",
                  body=json.dumps({"route": [3, 14, 10, 11]}), status=200,
                  content_type="application/json")
    origin = SdeSolarSystem.objects.get(system_id=1)
    dest = SdeSolarSystem.objects.get(system_id=11)
    plan = plan_jump(origin, dest, profile_for("rhea"), jdc=5, require_stations=True,
                     price_fn=lambda t: None)
    assert plan["can_plan"] is True
    assert plan["exit"]["system_id"] == 3   # ExitB — not the unreachable-under-stations ExitA


def test_service_capital_highsec_dest_errors_cleanly(universe):
    origin = SdeSolarSystem.objects.get(system_id=1)
    dest = SdeSolarSystem.objects.get(system_id=11)
    plan = plan_jump(origin, dest, profile_for("archon"))  # carrier can't reach high-sec
    assert plan["can_plan"] is False
    assert "can't enter high-sec" in plan["error"]
    assert plan["segments"] == []


def test_service_jf_lowsec_dest_is_pure_jump(universe):
    origin = SdeSolarSystem.objects.get(system_id=1)
    dest = SdeSolarSystem.objects.get(system_id=2)  # low-sec
    plan = plan_jump(origin, dest, profile_for("rhea"), jdc=5, price_fn=lambda tid: None)
    assert plan["mode"] == RouteMode.JUMP_ONLY
    assert [s["kind"] for s in plan["segments"]] == ["jump"]


def test_service_highsec_waypoint_rejected(universe):
    origin = SdeSolarSystem.objects.get(system_id=1)
    dest = SdeSolarSystem.objects.get(system_id=2)
    hs = SdeSolarSystem.objects.get(system_id=11)  # high-sec waypoint
    plan = plan_jump(origin, dest, profile_for("rhea"), waypoints=[hs])
    assert plan["can_plan"] is False and "cyno can't be lit" in plan["error"]


# --- 6. View + saved routes -------------------------------------------------
def _user(django_user_model, name, role=rbac.ROLE_MEMBER):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


@pytest.mark.django_db
def test_planner_view_renders_grouped_ships(client):
    html = client.get("/tools/jump/").content.decode()
    assert "Jump planner" in html
    assert "Jump Freighters" in html and "Black Ops" in html  # optgroups
    assert "Rhea" in html and "Redeemer" in html


@responses.activate
def test_planner_view_jf_highsec_dest(client, universe):
    responses.add(responses.POST, f"{ESI}/route/2/11/",
                  body=json.dumps({"route": [2, 10, 11]}), status=200,
                  content_type="application/json")
    resp = client.get("/tools/jump/?from=1&to=11&ship=rhea&jdc=5")
    assert resp.status_code == 200
    result = resp.context["result"]
    assert result is not None
    assert result["mode"] == RouteMode.JUMP_TO_LOWSEC_EXIT_THEN_GATE
    assert result["dest"]["name"] == "Hub"  # true destination preserved


@pytest.mark.django_db
def test_save_open_delete_route(client, django_user_model, universe):
    responses.reset()
    user = _user(django_user_model, "pilot")
    client.force_login(user)
    r = client.post("/tools/jump/routes/save/", {
        "from": "1", "from_q": "Origin", "to": "2", "to_q": "ExitOne",
        "ship": "rhea", "jdc": "5", "jfc": "5", "jf": "5", "pref": "safer",
        "name": "My run", "visibility": "private",
    })
    assert r.status_code == 302
    from apps.navigation.models import SavedJumpRoute
    route = SavedJumpRoute.objects.get()
    assert route.owner == user and route.origin_system_id == 1
    # Open rebuilds the planner query.
    opened = client.get(f"/tools/jump/routes/{route.pk}/open/")
    assert opened.status_code == 302 and "from=1" in opened.url and "ship=rhea" in opened.url
    # Owner can delete.
    assert client.post(f"/tools/jump/routes/{route.pk}/delete/").status_code == 302
    assert SavedJumpRoute.objects.count() == 0


@pytest.mark.django_db
def test_saved_route_permissions(client, django_user_model):
    owner = _user(django_user_model, "owner")
    other = _user(django_user_model, "other")
    officer = _user(django_user_model, "off", rbac.ROLE_OFFICER)
    from apps.navigation.models import SavedJumpRoute
    priv = SavedJumpRoute.objects.create(
        owner=owner, name="priv", origin_system_id=1, origin_name="A",
        dest_system_id=2, dest_name="B", ship_key="rhea", visibility="private")
    shared = SavedJumpRoute.objects.create(
        owner=owner, name="shared", origin_system_id=1, origin_name="A",
        dest_system_id=2, dest_name="B", ship_key="rhea", visibility="leadership")
    # A different member can't open a private route.
    client.force_login(other)
    assert client.get(f"/tools/jump/routes/{priv.pk}/open/").status_code == 302
    assert SavedJumpRoute.objects.filter(pk=priv.pk).exists()
    assert client.post(f"/tools/jump/routes/{priv.pk}/delete/").status_code == 302
    assert SavedJumpRoute.objects.filter(pk=priv.pk).exists()  # not deleted
    # An officer sees + can remove a leadership-shared route.
    client.force_login(officer)
    html = client.get("/tools/jump/routes/").content.decode()
    assert "shared" in html
    assert client.post(f"/tools/jump/routes/{shared.pk}/delete/").status_code == 302
    assert not SavedJumpRoute.objects.filter(pk=shared.pk).exists()


# --- 7. Admin settings ------------------------------------------------------
@pytest.mark.django_db
def test_admin_settings_officer_only(client, django_user_model):
    member = _user(django_user_model, "m1")
    client.force_login(member)
    assert client.get("/ops/admin/jump-planner/settings/").status_code == 403

    officer = _user(django_user_model, "o1", rbac.ROLE_OFFICER)
    client.force_login(officer)
    assert client.get("/ops/admin/jump-planner/settings/").status_code == 200
    resp = client.post("/ops/admin/jump-planner/settings/", {
        "enabled": "on", "default_jdc": "4", "default_jfc": "5", "default_jf_skill": "5",
        "default_preference": "safer", "fuel_safety_margin_pct": "5",
        "avoid_systems": "", "avoid_regions": "", "highsec_exit_warning": "Be careful.",
    })
    assert resp.status_code == 302
    from apps.navigation.models import JumpPlannerConfig
    cfg = JumpPlannerConfig.active()
    assert cfg.default_jdc == 4 and cfg.fuel_safety_margin_pct == 5.0


@pytest.mark.django_db
def test_admin_settings_margin_validation(client, django_user_model):
    officer = _user(django_user_model, "o2", rbac.ROLE_OFFICER)
    client.force_login(officer)
    resp = client.post("/ops/admin/jump-planner/settings/", {
        "enabled": "on", "default_jdc": "5", "default_jfc": "5", "default_jf_skill": "5",
        "default_preference": "safer", "fuel_safety_margin_pct": "250",
        "highsec_exit_warning": "x",
    })
    assert resp.status_code == 200  # re-rendered with the error, not saved
    assert b"between 0 and 100" in resp.content


# --- 8. Regression: existing jump math unchanged ----------------------------
@pytest.mark.django_db
def test_engine_fuel_still_computes(db):
    SdeRegion.objects.get_or_create(region_id=10000001, defaults={"name": "R"})
    clear_graph_cache()
    _sys(1, 0, 0.0, "S1")
    _sys(2, 5, 0.0, "S2")
    # Rhea, one 5 ly hop, JFC V + JF V -> 12,500 (the DOTLAN fixture, end to end).
    plan = jump_plan(1, 2, range_ly=10.0, fuel_per_ly=10000, uses_jf_skill=True, jfc=5, jf_skill=5)
    assert plan["hops"][0]["fuel"] == 12500
