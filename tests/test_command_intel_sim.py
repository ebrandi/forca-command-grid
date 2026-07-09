"""Simulation / Digital Twin + forecast (P6, doc 17 §1 / doc 18 P6): deterministic
perturb-and-recompute, a scenario that worsens a constraint, and least-squares breach
projection over constraint history."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.command_intel import forecast, simulation
from apps.command_intel.models import IntelligenceSnapshot, OperationalConstraint


def _snapshot(flyable=30, hulls=40, min_pilots=22, primary=False):
    return IntelligenceSnapshot.objects.create(slices={"doctrine": {"doctrines": [
        {"name": "Ferox", "slug": "ferox", "primary": primary,
         "flyable": flyable, "hulls_in_stock": hulls, "min_pilots": min_pilots},
    ]}})


def _row(rows, key):
    return next((r for r in rows if r["key"] == key), None)


@pytest.mark.django_db
def test_cold_state_without_a_snapshot(monkeypatch):
    monkeypatch.setattr("apps.command_intel.simulation.latest_snapshot", lambda: None)
    result = simulation.simulate("pilot_attrition", 5)
    assert result["available"] is False
    assert result["rows"] == []


@pytest.mark.django_db
def test_attrition_worsens_fleet_size(monkeypatch):
    snap = _snapshot(flyable=30, hulls=40, min_pilots=22)  # binding 30, headroom +8 ⇒ info
    monkeypatch.setattr("apps.command_intel.simulation.latest_snapshot", lambda: snap)
    result = simulation.simulate("pilot_attrition", 12)  # lose 12 pilots ⇒ 18, headroom −4
    assert result["available"] is True
    row = _row(result["rows"], "fleet_size.ferox")
    assert row is not None
    assert row["before"] == 30
    assert row["after"] == 18
    assert row["delta"] == -12
    assert row["worsened"] is True                 # info → high/critical
    assert result["worsened_count"] >= 1


@pytest.mark.django_db
def test_simulation_is_deterministic(monkeypatch):
    snap = _snapshot()
    monkeypatch.setattr("apps.command_intel.simulation.latest_snapshot", lambda: snap)
    a = simulation.simulate("pilot_attrition", 7)
    b = simulation.simulate("pilot_attrition", 7)
    assert a["rows"] == b["rows"]                   # identical arithmetic, no randomness


@pytest.mark.django_db
def test_magnitude_is_clamped(monkeypatch):
    snap = _snapshot()
    monkeypatch.setattr("apps.command_intel.simulation.latest_snapshot", lambda: snap)
    assert simulation.simulate("pilot_attrition", 99999)["magnitude"] == 200   # max
    assert simulation.simulate("pilot_attrition", "not-a-number")["magnitude"] == 5  # default


@pytest.mark.django_db
def test_live_snapshot_is_never_mutated(monkeypatch):
    snap = _snapshot(flyable=30)
    monkeypatch.setattr("apps.command_intel.simulation.latest_snapshot", lambda: snap)
    simulation.simulate("pilot_attrition", 10)
    snap.refresh_from_db()
    assert snap.slices["doctrine"]["doctrines"][0]["flyable"] == 30  # perturbation hit a copy


# --- forecast ---------------------------------------------------------------
def _history(headrooms, key="isk_runway"):
    base = timezone.now() - dt.timedelta(days=len(headrooms) - 1)
    for i, hr in enumerate(headrooms):
        snap = IntelligenceSnapshot.objects.create(slices={})
        IntelligenceSnapshot.objects.filter(pk=snap.pk).update(created_at=base + dt.timedelta(days=i))
        OperationalConstraint.objects.create(
            snapshot=snap, key=key, category="financial", label="ISK Runway",
            binding_metric=hr + 60, unit="days", headroom=hr, severity="watch", status="computed",
        )


@pytest.mark.django_db
def test_forecast_projects_a_declining_constraint_to_breach():
    _history([8, 6, 4, 2])  # slope −2/day, headroom 2 now ⇒ crosses 0 in ~1d
    findings = forecast.forecast_findings(window_days=21)
    f = next((x for x in findings if x["key"] == "isk_runway"), None)
    assert f is not None
    assert 0 < f["days_to_breach"] <= 21
    assert f["breach_at"] > timezone.now()


@pytest.mark.django_db
def test_forecast_is_silent_on_thin_history():
    _history([8, 6, 4])  # only 3 snapshots (< _MIN_POINTS)
    assert forecast.forecast_findings(window_days=21) == []


@pytest.mark.django_db
def test_forecast_ignores_an_improving_trend():
    _history([2, 4, 6, 8])  # headroom rising — no breach
    assert forecast.forecast_findings(window_days=21) == []
