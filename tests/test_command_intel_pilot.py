"""Pilot Intelligence (P5, doc 16 §7): constraint-relief ranking, honest fallback, and the
self-only IDOR guard on the directive actions."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.core.cache import cache

from apps.command_intel import pilot
from apps.command_intel.models import (
    IntelligenceSnapshot,
    OperationalConstraint,
    PilotDirective,
)
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _member(django_user_model, suffix=""):
    user, _ = django_user_model.objects.get_or_create(username=f"ci-pilot{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    return user


def _snapshot_with_fleet_constraint(severity="high", limiting="pilots_qualified"):
    snap = IntelligenceSnapshot.objects.create(slices={"doctrine": {"doctrines": [
        {"name": "Ferox", "slug": "ferox", "primary": True,
         "flyable": 18, "hulls_in_stock": 12, "min_pilots": 22},
    ]}})
    OperationalConstraint.objects.create(
        snapshot=snap, key="fleet_size.ferox", category="combat", label="Ferox fleet size",
        binding_metric=18, unit="pilots", limiting_factor=limiting, headroom=-4,
        severity=severity, status="computed",
    )
    return snap


@pytest.mark.django_db
def test_relief_directive_ties_to_the_binding_constraint(django_user_model, monkeypatch):
    snap = _snapshot_with_fleet_constraint(severity="high")
    monkeypatch.setattr("apps.command_intel.pilot.latest_snapshot", lambda: snap)
    monkeypatch.setattr(
        "apps.skills.services.closest_doctrines",
        lambda ch, limit=8: [{"doctrine": "Ferox", "doctrine_id": 1, "seconds": 600_000}],
    )
    user = _member(django_user_model, "-relief")
    payload = pilot.compute_directives(user, SimpleNamespace(character_id=1001), persist=True)

    top = payload["directives"][0]
    assert top["constraint_key"] == "fleet_size.ferox"
    assert top["category"] == PilotDirective.Category.SKILL
    assert "Ferox" in top["title"]
    assert top["leverage"] == 75  # high (70) + the training bonus (5)
    assert PilotDirective.objects.filter(user=user, constraint_key="fleet_size.ferox").exists()


@pytest.mark.django_db
def test_falls_back_to_doctrine_training_when_no_relief(django_user_model, monkeypatch):
    # A snapshot with no binding constraint the pilot can relieve ⇒ honest training seed.
    snap = IntelligenceSnapshot.objects.create(slices={"doctrine": {"doctrines": []}})
    monkeypatch.setattr("apps.command_intel.pilot.latest_snapshot", lambda: snap)
    monkeypatch.setattr(
        "apps.skills.services.closest_doctrines",
        lambda ch, limit=8: [{"doctrine": "Muninn", "doctrine_id": 2, "seconds": 300_000}],
    )
    user = _member(django_user_model, "-fallback")
    payload = pilot.compute_directives(user, SimpleNamespace(character_id=1002), persist=True)

    assert payload["directives"], "quest log is never empty"
    assert all(d["constraint_key"] == "" for d in payload["directives"])  # no fabricated corp-impact claim
    assert payload["ci_grounded"] is False


@pytest.mark.django_db
def test_stale_open_directive_is_dropped_on_regeneration(django_user_model, monkeypatch):
    snap = _snapshot_with_fleet_constraint()
    monkeypatch.setattr("apps.command_intel.pilot.latest_snapshot", lambda: snap)
    monkeypatch.setattr(
        "apps.skills.services.closest_doctrines",
        lambda ch, limit=8: [{"doctrine": "Ferox", "doctrine_id": 1, "seconds": 600_000}],
    )
    user = _member(django_user_model, "-stale")
    # A leftover OPEN directive that this run won't regenerate.
    PilotDirective.objects.create(user=user, slug="gone/stale", title="Old", category="skill")
    pilot.compute_directives(user, SimpleNamespace(character_id=1003), persist=True)
    assert not PilotDirective.objects.filter(user=user, slug="gone/stale").exists()


@pytest.mark.django_db
def test_done_state_survives_regeneration(django_user_model, monkeypatch):
    snap = _snapshot_with_fleet_constraint()
    monkeypatch.setattr("apps.command_intel.pilot.latest_snapshot", lambda: snap)
    monkeypatch.setattr(
        "apps.skills.services.closest_doctrines",
        lambda ch, limit=8: [{"doctrine": "Ferox", "doctrine_id": 1, "seconds": 600_000}],
    )
    user = _member(django_user_model, "-done")
    PilotDirective.objects.create(
        user=user, slug="fleet_size.ferox/train", title="Train into Ferox",
        category="skill", state=PilotDirective.State.DONE,
    )
    pilot.compute_directives(user, SimpleNamespace(character_id=1004), persist=True)
    kept = PilotDirective.objects.get(user=user, slug="fleet_size.ferox/train")
    assert kept.state == PilotDirective.State.DONE  # a done directive is not reopened


@pytest.mark.django_db
def test_directive_action_is_self_only(client, django_user_model):
    owner = _member(django_user_model, "-owner")
    intruder = _member(django_user_model, "-intruder")
    d = PilotDirective.objects.create(user=owner, slug="x/y", title="Stage a hull", category="ship")

    client.force_login(intruder)
    resp = client.post(f"/command/me/directive/{d.pk}/", {"action": "done"})
    assert resp.status_code == 404  # get_object_or_404(..., user=request.user)
    d.refresh_from_db()
    assert d.state == PilotDirective.State.OPEN  # intruder could not touch it

    client.force_login(owner)
    resp = client.post(f"/command/me/directive/{d.pk}/", {"action": "done"})
    assert resp.status_code == 302
    d.refresh_from_db()
    assert d.state == PilotDirective.State.DONE


@pytest.mark.django_db
def test_me_renders_link_character_state_without_a_character(client, django_user_model):
    # /command/me/ is a redirect into the Command Center now; following it must
    # land on the link-a-character empty state.
    member = _member(django_user_model, "-nochar")
    client.force_login(member)
    resp = client.get("/command/me/", follow=True)
    assert resp.status_code == 200
    assert resp.redirect_chain[-1][0] == "/dashboard/"
    assert b"Link an EVE character" in resp.content
