"""PCC-3 (roadmap 3.11) — constraint-aware, future-only contribution nudge.

An in-page steer that matches the member's usual contribution kind to a live binding corp
constraint. Best-effort, self-scoped, no alert.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.pilots.services import contribution_nudge, record_contribution

pytestmark = pytest.mark.django_db

_OPEN = "apps.command_intel.pilot._open_constraints"
_SNAP = "apps.command_intel.snapshot.latest_snapshot"


def _contrib(user, kind, n=3):
    for i in range(n):
        record_contribution(user, kind=kind, magnitude=1, points=1, unit="x",
                            ref_type="t", ref_id=f"{kind}-{i}", occurred_at=timezone.now())


def _constraint(category, key="", label="Logistics backlog"):
    return SimpleNamespace(category=category, key=key, label=label)


def _mock(monkeypatch, constraints):
    # None is a valid snapshot everywhere (all consumers None-guard it); _open_constraints is
    # mocked to ignore its arg, so the nudge still resolves against `constraints`.
    monkeypatch.setattr(_SNAP, lambda: None)
    monkeypatch.setattr(_OPEN, lambda snap: constraints)


def test_nudge_matches_usual_kind_to_constraint(django_user_model, monkeypatch):
    user = django_user_model.objects.create(username="hauler")
    _contrib(user, "haul", n=4)
    _mock(monkeypatch, [_constraint("logistics", label="Logistics backlog")])
    nudge = contribution_nudge(user)
    assert nudge is not None
    assert nudge["kind"] == "haul" and nudge["count_90d"] == 4
    assert nudge["constraint_label"] == "Logistics backlog"


def test_no_nudge_when_no_constraints(django_user_model, monkeypatch):
    user = django_user_model.objects.create(username="h2")
    _contrib(user, "haul")
    _mock(monkeypatch, [])
    assert contribution_nudge(user) is None


def test_no_nudge_when_no_contributions(django_user_model, monkeypatch):
    user = django_user_model.objects.create(username="h3")
    _mock(monkeypatch, [_constraint("logistics")])
    assert contribution_nudge(user) is None


def test_no_nudge_when_kind_does_not_match(django_user_model, monkeypatch):
    user = django_user_model.objects.create(username="h4")
    _contrib(user, "mining")  # mining doesn't relieve a combat-readiness constraint
    _mock(monkeypatch, [_constraint("combat", label="Combat readiness")])
    assert contribution_nudge(user) is None


def test_doctrine_stock_key_matches_build(django_user_model, monkeypatch):
    user = django_user_model.objects.create(username="h5")
    _contrib(user, "build")
    _mock(monkeypatch, [_constraint("logistics", key="doctrine_stock.ferox", label="Ferox stock")])
    nudge = contribution_nudge(user)
    assert nudge and nudge["kind"] == "build"
