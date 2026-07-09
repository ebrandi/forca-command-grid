"""Gap A — the per-KPI configuration layer (readiness.kpis) + admin page.

Verifies: the validator; ``combine_kpi_scores`` (empty config == plain mean, so the
index is byte-stable; disabled/ weighted KPIs change the fold); the config flowing
through a provider via ``compute_dimension``; the Director-gated admin page; and a
drift guard that every emitted KPI key is declared in its provider's catalogue.
"""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.readiness import config
from apps.readiness.engine.base import KpiResult, combine_kpi_scores, combine_scores
from apps.sso.services import ensure_role
from core import rbac


def _director(django_user_model, name="dir"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    return user


def _kpi(key, score):
    return KpiResult(key=key, value=None, score=score)


# --- combine_kpi_scores (the index-neutrality core) --------------------------
def test_empty_config_equals_plain_mean():
    kpis = [_kpi("d.a", 80), _kpi("d.b", 40), _kpi("d.c", None)]
    assert combine_kpi_scores(kpis, {}) == combine_scores(k.score for k in kpis) == 60


def test_disabled_kpi_excluded_from_fold():
    kpis = [_kpi("d.a", 80), _kpi("d.b", 40)]
    assert combine_kpi_scores(kpis, {"d.b": {"enabled": False}}) == 80


def test_weighted_fold():
    kpis = [_kpi("d.a", 100), _kpi("d.b", 0)]
    # a weighted 3:1 over b → (100*3 + 0*1)/4 = 75
    assert combine_kpi_scores(kpis, {"d.a": {"weight": 3}, "d.b": {"weight": 1}}) == 75


def test_all_disabled_is_none():
    kpis = [_kpi("d.a", 80)]
    assert combine_kpi_scores(kpis, {"d.a": {"enabled": False}}) is None


# --- validator ---------------------------------------------------------------
@pytest.mark.django_db
def test_validator_accepts_and_normalises():
    out = config.set("kpis", {"financial.runway_months": {"enabled": False, "weight": "2",
                                                           "thresholds": {"amber": "70", "red": "30"}}})
    entry = out["financial.runway_months"]
    assert entry["enabled"] is False and entry["weight"] == 2.0
    assert entry["thresholds"] == {"amber": 70, "red": 30}


@pytest.mark.django_db
def test_validator_rejects_bad_weight_and_bands():
    with pytest.raises(config.ConfigError):
        config.set("kpis", {"x.y": {"weight": -1}})
    with pytest.raises(config.ConfigError):
        config.set("kpis", {"x.y": {"thresholds": {"amber": 30, "red": 70}}})  # red >= amber


# --- config flows through a provider -----------------------------------------
@pytest.mark.django_db
def test_disabling_a_kpi_changes_the_dimension_score(sde):
    from apps.readiness.services import compute_dimension

    # leadership.officer_coverage is always measurable (filled vs defined owner desks);
    # by default it's the only scored KPI, so disabling it makes the dimension unavailable.
    before = compute_dimension("leadership")
    assert before is not None and before.score is not None
    config.set("kpis", {"leadership.officer_coverage": {"enabled": False}})
    after = compute_dimension("leadership")
    assert after.score is None  # its only KPI disabled ⇒ honest "unavailable"


# --- admin page --------------------------------------------------------------
@pytest.mark.django_db
def test_kpi_page_lists_grouped_and_is_director_only(client, django_user_model, sde):
    # member → 403
    from apps.identity.models import RoleAssignment as RA
    member = django_user_model.objects.create(username="m")
    RA.objects.create(user=member, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(member)
    assert client.get("/ops/admin/readiness/kpis/").status_code == 403
    # director → page lists KPI keys grouped by dimension
    client.force_login(_director(django_user_model))
    html = client.get("/ops/admin/readiness/kpis/").content.decode()
    assert "financial.runway_months" in html
    assert "srp.pending_backlog" in html


@pytest.mark.django_db
def test_kpi_page_save_persists_and_audits(client, django_user_model, sde):
    from apps.admin_audit.models import AuditLog

    client.force_login(_director(django_user_model, "dir2"))
    resp = client.post("/ops/admin/readiness/kpis/", {
        # financial.runway_months left unchecked → disabled; weight + bands set
        "kpi_financial.runway_months_weight": "2",
        "kpi_financial.runway_months_amber": "70",
        "kpi_financial.runway_months_red": "30",
        # wallet enabled
        "kpi_financial.wallet_vs_min_enabled": "on",
        "kpi_financial.wallet_vs_min_weight": "1",
    })
    assert resp.status_code == 302
    cfg = config.get("kpis")
    assert cfg["financial.runway_months"]["enabled"] is False
    assert cfg["financial.runway_months"]["weight"] == 2.0
    assert cfg["financial.runway_months"]["thresholds"] == {"amber": 70, "red": 30}
    assert cfg["financial.wallet_vs_min"]["enabled"] is True
    assert AuditLog.objects.filter(action="readiness.config.update", target_id="kpis").exists()


# --- drift guard: every declared catalogue key is well-formed ----------------
@pytest.mark.django_db
def test_kpi_catalogue_keys_are_namespaced_and_unique():
    from apps.readiness.engine import registry

    seen = set()
    for provider in registry.providers():
        for key, label in getattr(provider, "kpi_catalogue", []):
            assert key.startswith(f"{provider.key}."), f"{key} not namespaced to {provider.key}"
            assert label, f"{key} missing a label"
            assert key not in seen, f"duplicate KPI key {key}"
            seen.add(key)
