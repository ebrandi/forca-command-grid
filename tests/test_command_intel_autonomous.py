"""Guard-railed autonomous proposal (P7, doc 17 §5): the kill switch (provably inert when
off), the calibration trust gate, and the propose→audit path (proposals land as PROPOSED,
never accepted)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.command_intel import autonomous, config
from apps.command_intel.engine.base import Constraint
from apps.command_intel.models import (
    ActionOutcome,
    CourseOfAction,
    IntelligenceSnapshot,
)
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _user(django_user_model, role, suffix=""):
    user, _ = django_user_model.objects.get_or_create(username=f"ci-auto-{role}{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


def _seed_calibration(family="fleet_size.ferox", errors=(1, -1, 0)):
    coa = CourseOfAction.objects.create(slug=f"{family}/seed", objective="o", state="completed")
    for e in errors:
        ActionOutcome.objects.create(
            coa=coa, metric_key=family, predicted_delta=4, measured_delta=4 + e, error=e,
        )


@pytest.mark.django_db
def test_kill_switch_off_proposes_nothing():
    # Default config: enabled=False. The provable gate.
    assert config.DEFAULTS["autonomous"]["enabled"] is False
    assert autonomous.run_autonomous_proposals() == {"status": "disabled", "proposed": 0}
    assert not CourseOfAction.objects.exists()


@pytest.mark.django_db
def test_armed_but_no_calibration_proposes_nothing():
    config.set("autonomous", {"enabled": True})
    result = autonomous.run_autonomous_proposals()
    assert result["proposed"] == 0  # no measured-outcome history ⇒ no trusted family
    assert result["status"] in ("no_trusted_families", "ok")
    assert not CourseOfAction.objects.filter(state=CourseOfAction.State.PROPOSED).exists()


@pytest.mark.django_db
def test_calibration_gate_selects_only_trusted_families():
    config.set("autonomous", {"enabled": True, "min_calibration_samples": 3, "max_calibration_spread": 5.0})
    _seed_calibration("fleet_size.ferox", errors=(1, -1, 0))  # tight, enough samples
    constraints = [SimpleNamespace(key="fleet_size.ferox"), SimpleNamespace(key="isk_runway")]
    trusted = autonomous._trusted_families(constraints)
    assert "fleet_size" in trusted
    assert "isk_runway" not in trusted  # no history for this family


@pytest.mark.django_db
def test_armed_proposes_for_a_trusted_family_and_audits(monkeypatch):
    from apps.admin_audit.models import AuditLog

    config.set("autonomous", {"enabled": True, "min_calibration_samples": 3, "max_calibration_spread": 100.0})
    _seed_calibration("fleet_size.ferox", errors=(1, -1, 0))

    snap = IntelligenceSnapshot.objects.create(slices={"doctrine": {"doctrines": [
        {"name": "Ferox", "slug": "ferox", "primary": True,
         "flyable": 18, "hulls_in_stock": 12, "min_pilots": 22},
    ]}})
    con = Constraint(
        key="fleet_size.ferox", category="combat", label="Ferox fleet size",
        binding_metric=18, unit="pilots", limiting_factor="pilots_qualified",
        headroom=-4, severity="high", status="computed",
    )
    monkeypatch.setattr("apps.command_intel.autonomous.snapshot_mod.build_snapshot", lambda **k: snap)
    monkeypatch.setattr("apps.command_intel.engine.pipeline.compute_constraints", lambda *a, **k: [con])
    monkeypatch.setattr("apps.command_intel.autonomous.impact.candidate_impacts", lambda *a, **k: [])

    result = autonomous.run_autonomous_proposals()
    assert result["status"] == "ok"
    assert result["proposed"] >= 1
    assert "fleet_size" in result["families"]

    proposed = CourseOfAction.objects.filter(state=CourseOfAction.State.PROPOSED)
    assert proposed.exists()
    # Proposed only — autonomy never accepts or creates a task.
    assert not CourseOfAction.objects.filter(state=CourseOfAction.State.IN_PROGRESS).exists()
    assert AuditLog.objects.filter(action="command_intel.autonomous.propose").exists()


@pytest.mark.django_db
def test_min_calibration_samples_must_be_at_least_one():
    # 0 samples would make every family "trusted" with no track record — refused.
    with pytest.raises(config.ConfigError):
        config._validate_autonomous({"min_calibration_samples": 0})
    config._validate_autonomous({"min_calibration_samples": 1})  # the floor is allowed


@pytest.mark.django_db
def test_autonomous_console_toggles_the_kill_switch(client, django_user_model, sde):
    director = _user(django_user_model, rbac.ROLE_DIRECTOR)
    client.force_login(director)
    url = reverse("admin_audit:command_intel_autonomous")
    assert client.get(url).status_code == 200
    resp = client.post(url, {"enabled": "on", "min_calibration_samples": "4",
                             "max_calibration_spread": "2.5", "max_proposals_per_run": "3"})
    assert resp.status_code == 302
    cfg = config.get("autonomous")
    assert cfg["enabled"] is True
    assert cfg["min_calibration_samples"] == 4
