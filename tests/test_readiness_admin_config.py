"""Milestone 1 — admin config pages: finance, SRP, responsibilities, alerts editor.

These pages reuse the already-validated ``apps.readiness.config`` domains; the tests
verify the Director gating, the round-trip through ``config.set`` (validate → persist →
version bump), the audit trail, and the alert-rule upsert/delete semantics.
"""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.readiness import config
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _director(django_user_model, name="dir"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    return user


def _officer(django_user_model, name="off"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


# --- gating ------------------------------------------------------------------
@pytest.mark.django_db
def test_pages_are_director_only(client, django_user_model):
    client.force_login(_officer(django_user_model))
    for url in (
        "/ops/admin/readiness/finance/",
        "/ops/admin/readiness/srp/",
        "/ops/admin/readiness/responsibilities/",
        "/ops/admin/readiness/alerts/",
    ):
        assert client.get(url).status_code == 403, url


@pytest.mark.django_db
def test_pages_render_for_director(client, django_user_model):
    client.force_login(_director(django_user_model))
    assert "Financial configuration" in client.get("/ops/admin/readiness/finance/").content.decode()
    assert "SRP thresholds" in client.get("/ops/admin/readiness/srp/").content.decode()
    assert "Officer responsibilities" in client.get("/ops/admin/readiness/responsibilities/").content.decode()
    assert "Alert rules" in client.get("/ops/admin/readiness/alerts/").content.decode()


# --- finance -----------------------------------------------------------------
@pytest.mark.django_db
def test_finance_save_persists_and_audits(client, django_user_model):
    from apps.admin_audit.models import AuditLog

    client.force_login(_director(django_user_model, "dir2"))
    client.post("/ops/admin/readiness/finance/", {
        "min_wallet": "9000000000", "srp_budget": "1500000000", "wallet_division_scope": "3",
    })
    cfg = config.get("finance")
    assert cfg["min_wallet"] == 9_000_000_000
    assert cfg["srp_budget"] == 1_500_000_000
    assert cfg["wallet_division_scope"] == "3"
    assert AuditLog.objects.filter(action="readiness.config.update", target_id="finance").exists()


@pytest.mark.django_db
def test_finance_blank_field_keeps_current(client, django_user_model):
    client.force_login(_director(django_user_model, "dir3"))
    config.set("finance", {"min_wallet": 7_000_000_000})
    client.post("/ops/admin/readiness/finance/", {"min_wallet": "", "srp_budget": "2000000000"})
    cfg = config.get("finance")
    assert cfg["min_wallet"] == 7_000_000_000  # untouched
    assert cfg["srp_budget"] == 2_000_000_000


@pytest.mark.django_db
def test_finance_rejects_negative(client, django_user_model):
    client.force_login(_director(django_user_model, "dir4"))
    config.set("finance", {"min_wallet": 5_000_000_000})
    client.post("/ops/admin/readiness/finance/", {"min_wallet": "-1"})
    assert config.get("finance")["min_wallet"] == 5_000_000_000  # no partial write


# --- srp ---------------------------------------------------------------------
@pytest.mark.django_db
def test_srp_save_persists(client, django_user_model):
    client.force_login(_director(django_user_model, "dir5"))
    client.post("/ops/admin/readiness/srp/", {
        "max_pending_claims": "40", "max_avg_wait_hours": "48", "max_claim_age_days": "21",
    })
    cfg = config.get("srp")
    assert cfg["max_pending_claims"] == 40
    assert cfg["max_avg_wait_hours"] == 48
    assert cfg["max_claim_age_days"] == 21


# --- responsibilities --------------------------------------------------------
@pytest.mark.django_db
def test_responsibilities_assigns_users_and_maps_dimensions(client, django_user_model):
    director = _director(django_user_model, "dir6")
    # A corp member that can be assigned to a desk.
    pilot = django_user_model.objects.create(username="eve:42")
    EveCharacter.objects.create(character_id=42, name="Logi Pilot", is_main=True,
                                is_corp_member=True, user=pilot)
    client.force_login(director)
    client.post("/ops/admin/readiness/responsibilities/", {
        "tag_finance_officer_label": "Finance Desk",
        "tag_finance_officer_users": [str(pilot.id)],
        "dim_financial_owner": "finance_officer",
        "dim_doctrine_owner": "training_officer",
    })
    cfg = config.get("responsibilities")
    assert cfg["owner_tags"]["finance_officer"]["label"] == "Finance Desk"
    assert cfg["owner_tags"]["finance_officer"]["users"] == [pilot.id]
    assert cfg["dimension_owner"]["financial"] == "finance_officer"
    assert cfg["dimension_owner"]["doctrine"] == "training_officer"


@pytest.mark.django_db
def test_responsibilities_drops_unmapped_dimension(client, django_user_model):
    client.force_login(_director(django_user_model, "dir7"))
    # doctrine maps to training_officer by default; posting it blank clears the mapping.
    client.post("/ops/admin/readiness/responsibilities/", {"dim_doctrine_owner": ""})
    assert "doctrine" not in config.get("responsibilities")["dimension_owner"]


# --- alerts ------------------------------------------------------------------
@pytest.mark.django_db
def test_alert_rule_add_and_structure(client, django_user_model):
    from apps.admin_audit.models import AuditLog

    client.force_login(_director(django_user_model, "dir8"))
    client.post("/ops/admin/readiness/alerts/save/", {
        "key": "low-fuel", "severity": "high", "dimension": "infrastructure", "kind": "risk",
        "channel_discord": "on", "cooldown_hours": "12",
        "escalate_after_hours": "48", "escalate_discord": "on",
    })
    rules = config.get("alerts")["rules"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["key"] == "low-fuel" and rule["severity"] == "high"
    assert rule["match"] == {"dimension": "infrastructure", "kind": "risk"}
    assert rule["channels"] == ["discord"]
    assert rule["cooldown_hours"] == 12
    assert rule["escalate_after_hours"] == 48
    assert rule["escalate_channels"] == ["discord"]
    assert AuditLog.objects.filter(action="readiness.config.update", target_id="alerts").exists()


@pytest.mark.django_db
def test_alert_rule_upsert_by_key(client, django_user_model):
    client.force_login(_director(django_user_model, "dir9"))
    client.post("/ops/admin/readiness/alerts/save/", {"key": "r1", "severity": "warn", "channel_discord": "on"})
    # Re-save same key with a new severity → replaces, not duplicates.
    client.post("/ops/admin/readiness/alerts/save/", {"key": "r1", "severity": "critical", "channel_discord": "on"})
    rules = config.get("alerts")["rules"]
    assert len(rules) == 1 and rules[0]["severity"] == "critical"


@pytest.mark.django_db
def test_alert_rule_rename_key(client, django_user_model):
    client.force_login(_director(django_user_model, "dir10"))
    client.post("/ops/admin/readiness/alerts/save/", {"key": "old", "severity": "warn", "channel_discord": "on"})
    client.post("/ops/admin/readiness/alerts/save/", {
        "key": "new", "original_key": "old", "severity": "warn", "channel_discord": "on",
    })
    keys = {r["key"] for r in config.get("alerts")["rules"]}
    assert keys == {"new"}


@pytest.mark.django_db
def test_alert_rule_rejects_bad_key(client, django_user_model):
    client.force_login(_director(django_user_model, "dir11"))
    client.post("/ops/admin/readiness/alerts/save/", {"key": "Bad Key!", "severity": "warn"})
    assert config.get("alerts")["rules"] == []


@pytest.mark.django_db
def test_alert_rule_delete(client, django_user_model):
    client.force_login(_director(django_user_model, "dir12"))
    client.post("/ops/admin/readiness/alerts/save/", {"key": "doomed", "severity": "warn", "channel_discord": "on"})
    client.post("/ops/admin/readiness/alerts/doomed/delete/")
    assert config.get("alerts")["rules"] == []


@pytest.mark.django_db
def test_alert_rule_no_escalation_when_window_blank(client, django_user_model):
    client.force_login(_director(django_user_model, "dir13"))
    client.post("/ops/admin/readiness/alerts/save/", {
        "key": "simple", "severity": "warn", "channel_discord": "on", "escalate_after_hours": "",
    })
    rule = config.get("alerts")["rules"][0]
    assert "escalate_after_hours" not in rule
