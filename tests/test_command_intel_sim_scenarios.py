"""4.18 — Saveable / comparable simulator scenarios (contingency planning).

Acceptance: an officer can save a named what-if (stressor + magnitude, validated), the
shared library re-runs each scenario LIVE against the latest snapshot, and a compare
view ranks them by how many constraints each pushes past their limit. Officer-gated.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.command_intel import simulation
from apps.command_intel.models import IntelligenceSnapshot, SavedSimScenario
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db


def _officer(django_user_model, name="off"):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_OFFICER))
    return u


def _snapshot(flyable=30, hulls=40, min_pilots=22):
    return IntelligenceSnapshot.objects.create(slices={"doctrine": {"doctrines": [
        {"name": "Ferox", "slug": "ferox", "primary": True,
         "flyable": flyable, "hulls_in_stock": hulls, "min_pilots": min_pilots}]}})


def test_validate_scenario_clamps_and_defaults():
    assert simulation.validate_scenario("pilot_attrition", 5) == ("pilot_attrition", 5)
    key, _mag = simulation.validate_scenario("bogus", 999)
    assert key == "pilot_attrition"                       # unknown → default scenario
    assert simulation.validate_scenario("fuel_shock", 9999) == ("fuel_shock", 10)  # clamp to max


def test_compare_ranks_by_worsened(monkeypatch):
    snap = _snapshot(flyable=30, min_pilots=22)  # headroom +8
    monkeypatch.setattr("apps.command_intel.simulation.latest_snapshot", lambda: snap)
    mild = SavedSimScenario(name="mild", scenario_key="pilot_attrition", magnitude=2)
    harsh = SavedSimScenario(name="harsh", scenario_key="pilot_attrition", magnitude=15)
    out = simulation.compare([mild, harsh])
    assert [c["name"] for c in out] == ["harsh", "mild"]  # most-dangerous first
    assert out[0]["worsened_count"] >= out[1]["worsened_count"]


def test_save_creates_scenario(client, django_user_model):
    client.force_login(_officer(django_user_model))
    resp = client.post(reverse("command_intel:simulator_save"),
                       {"name": "wallet hit", "scenario": "income_drop", "magnitude": "30"})
    assert resp.status_code == 302
    s = SavedSimScenario.objects.get()
    assert s.name == "wallet hit" and s.scenario_key == "income_drop" and s.magnitude == 30


def test_save_validates_bad_scenario(client, django_user_model):
    client.force_login(_officer(django_user_model))
    client.post(reverse("command_intel:simulator_save"),
                {"name": "x", "scenario": "evil", "magnitude": "99999"})
    s = SavedSimScenario.objects.get()
    assert s.scenario_key == "pilot_attrition" and s.magnitude <= 200  # normalised + clamped


def test_save_requires_name(client, django_user_model):
    client.force_login(_officer(django_user_model))
    resp = client.post(reverse("command_intel:simulator_save"),
                       {"name": "   ", "scenario": "fuel_shock", "magnitude": "2"})
    assert resp.status_code == 302 and SavedSimScenario.objects.count() == 0


def test_delete_removes(client, django_user_model):
    client.force_login(_officer(django_user_model))
    s = SavedSimScenario.objects.create(name="x", scenario_key="fuel_shock", magnitude=2)
    resp = client.post(reverse("command_intel:simulator_delete", args=[s.pk]))
    assert resp.status_code == 302 and SavedSimScenario.objects.count() == 0


def test_compare_view_renders(client, django_user_model, monkeypatch):
    snap = _snapshot()
    monkeypatch.setattr("apps.command_intel.simulation.latest_snapshot", lambda: snap)
    SavedSimScenario.objects.create(name="A", scenario_key="pilot_attrition", magnitude=10)
    SavedSimScenario.objects.create(name="B", scenario_key="fuel_shock", magnitude=3)
    client.force_login(_officer(django_user_model))
    resp = client.get(reverse("command_intel:simulator_compare"))
    assert resp.status_code == 200 and resp.context["count"] == 2
    assert b"Which contingency hurts most" in resp.content


def test_saved_library_shows_on_simulator(client, django_user_model, monkeypatch):
    monkeypatch.setattr("apps.command_intel.simulation.latest_snapshot", lambda: _snapshot())
    SavedSimScenario.objects.create(name="MyContingency", scenario_key="fuel_shock", magnitude=3)
    client.force_login(_officer(django_user_model))
    resp = client.get(reverse("command_intel:simulator"))
    assert resp.status_code == 200 and b"MyContingency" in resp.content


def test_compare_is_capped(client, django_user_model, monkeypatch):
    from apps.command_intel.views import MAX_COMPARE
    monkeypatch.setattr("apps.command_intel.simulation.latest_snapshot", lambda: _snapshot())
    SavedSimScenario.objects.bulk_create([
        SavedSimScenario(name=f"S{i}", scenario_key="fuel_shock", magnitude=2)
        for i in range(MAX_COMPARE + 3)
    ])
    client.force_login(_officer(django_user_model))
    resp = client.get(reverse("command_intel:simulator_compare"))
    assert resp.context["count"] == MAX_COMPARE and resp.context["truncated"] is True


def test_library_size_cap_blocks_overflow(client, django_user_model):
    from apps.command_intel.views import MAX_SAVED_SCENARIOS
    SavedSimScenario.objects.bulk_create([
        SavedSimScenario(name=f"S{i}", scenario_key="fuel_shock", magnitude=2)
        for i in range(MAX_SAVED_SCENARIOS)
    ])
    client.force_login(_officer(django_user_model))
    resp = client.post(reverse("command_intel:simulator_save"),
                       {"name": "one more", "scenario": "fuel_shock", "magnitude": "2"})
    assert resp.status_code == 302
    assert SavedSimScenario.objects.count() == MAX_SAVED_SCENARIOS  # rejected, not created


def test_sim_endpoints_are_officer_only(client, django_user_model):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get(reverse("command_intel:simulator_compare")).status_code == 403
    assert client.post(reverse("command_intel:simulator_save"), {"name": "x"}).status_code == 403
