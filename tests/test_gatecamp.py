"""Gate-camp intel: scoring, chokepoint detection, watch list, route check."""
from __future__ import annotations

import pytest

from apps.navigation.gatecamp import (
    assess,
    camp_watch,
    chokepoint_flags,
    route_camp_check,
)
from apps.sde.models import SdeRegion, SdeSolarSystem, SdeSystemJump


@pytest.fixture
def space(db):
    # A lowsec pipe (Choke, 2 gates, borders null) hangs off a well-connected highsec
    # hub (4 gates). Choke is the textbook camp spot; the hub is not.
    r = SdeRegion.objects.create(region_id=10000077, name="Warzone")
    SdeSolarSystem.objects.create(system_id=40001, region=r, name="HubHigh", security=0.7)
    SdeSolarSystem.objects.create(system_id=40002, region=r, name="Choke", security=0.3)
    SdeSolarSystem.objects.create(system_id=40003, region=r, name="NullEnd", security=-0.2)
    SdeSolarSystem.objects.create(system_id=40004, region=r, name="HighA", security=0.8)
    SdeSolarSystem.objects.create(system_id=40005, region=r, name="HighB", security=0.8)
    SdeSolarSystem.objects.create(system_id=40006, region=r, name="HighC", security=0.9)
    for a, b in [(40001, 40002), (40002, 40003),
                 (40001, 40004), (40001, 40005), (40001, 40006)]:
        SdeSystemJump.objects.create(from_system_id=a, to_system_id=b)
        SdeSystemJump.objects.create(from_system_id=b, to_system_id=a)
    return r


def _make_member(django_user_model, username="eve:camp1"):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    m = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=m, role=ensure_role(rbac.ROLE_MEMBER))
    return m


# --- Scoring ----------------------------------------------------------------
def test_assess_clear():
    a = assess({"ship_kills": 0, "pod_kills": 0, "npc_kills": 0}, 50)
    assert a["level"] == "clear" and a["score"] == 0


def test_assess_caution_on_a_pod():
    a = assess({"ship_kills": 1, "pod_kills": 1, "npc_kills": 0}, 30)
    assert a["level"] == "caution"
    assert any("pod" in r for r in a["reasons"])


def test_assess_danger_on_multiple_pods():
    a = assess({"ship_kills": 4, "pod_kills": 3, "npc_kills": 0}, 20)
    assert a["level"] == "danger"


def test_assess_ratting_is_not_a_camp():
    # Lots of NPC kills, a little PvP → ratting hub, damped to elevated.
    a = assess({"ship_kills": 5, "pod_kills": 2, "npc_kills": 200}, 40)
    assert a["level"] == "elevated"
    assert any("ratting" in r for r in a["reasons"])


def test_assess_chokepoint_escalates():
    base = {"ship_kills": 2, "pod_kills": 1, "npc_kills": 0}
    assert assess(base, 10, chokepoint=False)["level"] == "caution"
    assert assess(base, 10, chokepoint=True)["level"] == "danger"


def test_assess_low_traffic_ambush_reason():
    a = assess({"ship_kills": 2, "pod_kills": 0, "npc_kills": 0}, 1)
    assert any("ambush" in r for r in a["reasons"])


# --- Chokepoint detection ---------------------------------------------------
@pytest.mark.django_db
def test_chokepoint_flags(space):
    flags = chokepoint_flags([40001, 40002])
    assert flags[40002] is True    # lowsec, 2 gates, borders null → chokepoint
    assert flags[40001] is False   # highsec hub with 4 gates → not a pipe


# --- Watch list -------------------------------------------------------------
@pytest.mark.django_db
def test_camp_watch_ranks_worst_first(space, monkeypatch):
    monkeypatch.setattr("apps.navigation.map_overlays.system_kills", lambda: {
        40002: {"ship_kills": 5, "pod_kills": 4, "npc_kills": 0},   # camp on the pipe
        40001: {"ship_kills": 1, "pod_kills": 0, "npc_kills": 0},   # a stray kill
    })
    monkeypatch.setattr("apps.navigation.map_overlays.system_jumps", lambda: {40002: 8})
    rows = camp_watch(band="all", limit=10)
    assert rows[0]["system_id"] == 40002 and rows[0]["level"] == "danger"
    assert rows[0]["chokepoint"] is True
    assert all(r["level"] in ("caution", "danger") for r in rows)


@pytest.mark.django_db
def test_camp_watch_band_filter(space, monkeypatch):
    monkeypatch.setattr("apps.navigation.map_overlays.system_kills", lambda: {
        40002: {"ship_kills": 5, "pod_kills": 4, "npc_kills": 0},
    })
    monkeypatch.setattr("apps.navigation.map_overlays.system_jumps", lambda: {})
    assert camp_watch(band="highsec") == []        # the camp is lowsec
    assert camp_watch(band="lowsec")[0]["system_id"] == 40002


# --- Route check ------------------------------------------------------------
@pytest.mark.django_db
def test_route_camp_check(space, monkeypatch):
    monkeypatch.setattr("apps.navigation.map_overlays.system_kills", lambda: {
        40002: {"ship_kills": 6, "pod_kills": 5, "npc_kills": 0},
    })
    monkeypatch.setattr("apps.navigation.map_overlays.system_jumps", lambda: {40002: 5})
    summary = route_camp_check([40001, 40002, 40003])
    assert summary["worst"] == "danger" and summary["danger"] == 1
    assert summary["by_id"][40002]["level"] == "danger"
    assert summary["by_id"][40001]["level"] == "clear"
    assert summary["flagged"][0]["name"] == "Choke"


# --- View -------------------------------------------------------------------
@pytest.mark.django_db
def test_gatecamp_view_requires_login(client, space):
    resp = client.get("/tools/gatecamp/")
    assert resp.status_code == 302  # redirected to login


@pytest.mark.django_db
def test_gatecamp_view_member(client, django_user_model, space, monkeypatch):
    monkeypatch.setattr("apps.navigation.map_overlays.system_kills", lambda: {
        40002: {"ship_kills": 5, "pod_kills": 4, "npc_kills": 0},
    })
    monkeypatch.setattr("apps.navigation.map_overlays.system_jumps", lambda: {40002: 8})
    client.force_login(_make_member(django_user_model))
    resp = client.get("/tools/gatecamp/?band=lowsec")
    assert resp.status_code == 200
    assert "Choke" in resp.content.decode() and "DANGER" in resp.content.decode().upper()
