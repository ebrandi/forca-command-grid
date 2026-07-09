"""Phase 3 — net-new financial & srp dimensions (providers + drill-down)."""
from __future__ import annotations

import copy
import datetime as dt
from decimal import Decimal as D

import pytest
from django.utils import timezone

from apps.corporation.models import CorpWalletDivision, CorpWalletJournalEntry
from apps.identity.models import RoleAssignment
from apps.readiness import config
from apps.readiness.services import compute_dimension, compute_readiness
from apps.sso.services import ensure_role
from core import rbac


def _officer(django_user_model, name="off"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _wallet(balance="10000000000", entries=()):
    CorpWalletDivision.objects.create(division=1, balance=D(balance))
    for i, (amount, days_ago) in enumerate(entries, start=1):
        CorpWalletJournalEntry.objects.create(
            entry_id=i, division=1, date=timezone.now() - dt.timedelta(days=days_ago),
            ref_type="x", amount=D(amount),
        )


def _srp_program():
    from apps.srp.models import SrpProgram

    SrpProgram.objects.create(is_active=True)


def _claim(django_user_model, cid, status, created_days_ago=0):
    from apps.killboard.models import Killmail
    from apps.srp.models import SrpClaim

    user = django_user_model.objects.create(username=f"eve:{cid}")
    km = Killmail.objects.create(
        killmail_id=cid, killmail_time=timezone.now(), solar_system_id=30000142,
        victim_character_id=cid, victim_ship_type_id=587, total_value=D("1"),
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
    )
    claim = SrpClaim.objects.create(killmail=km, claimant=user, status=status)
    if created_days_ago:
        SrpClaim.objects.filter(pk=claim.pk).update(
            created_at=timezone.now() - dt.timedelta(days=created_days_ago)
        )
        claim.refresh_from_db()
    return claim


# --- financial provider ------------------------------------------------------
@pytest.mark.django_db
def test_financial_unavailable_without_wallet():
    result = compute_dimension("financial")
    assert result is not None
    assert result.score is None and result.status == "unavailable"  # honest score


@pytest.mark.django_db
def test_financial_scores_from_wallet():
    # Healthy: well above min_wallet (5B), net income (no burn) → strong score.
    _wallet(balance="20000000000", entries=[("3000000000", 5), ("-500000000", 10)])
    result = compute_dimension("financial")
    assert result.score is not None and result.score >= 75
    keys = {k.key for k in result.kpis}
    assert keys == {
        "financial.wallet_vs_min", "financial.runway_months",
        "financial.reserve_cover", "financial.burn_vs_target",
    }


@pytest.mark.django_db
def test_financial_empty_journal_excludes_burn_kpis():
    # Divisions exist but the journal window is empty (sync lagging) → burn-derived
    # KPIs are unavailable (not optimistically 100), balance-derived ones still score.
    _wallet(balance="20000000000")  # no journal entries
    result = compute_dimension("financial")
    by_key = {k.key: k for k in result.kpis}
    assert by_key["financial.runway_months"].score is None
    assert by_key["financial.burn_vs_target"].score is None
    assert by_key["financial.wallet_vs_min"].score is not None  # balance still measured
    assert result.score is not None  # scored from the available KPIs


@pytest.mark.django_db
def test_financial_low_runway_emits_finding():
    # Small balance + heavy net burn this month → runway < 1 month → critical finding.
    _wallet(balance="500000000", entries=[("-4000000000", 5)])
    result = compute_dimension("financial")
    runway = next(k for k in result.kpis if k.key == "financial.runway_months")
    assert runway.value < 1
    assert any(f.kpi_key == "financial.runway_months" and f.severity == "critical"
               for f in result.findings)


# --- srp provider ------------------------------------------------------------
@pytest.mark.django_db
def test_srp_unavailable_without_active_program(sde):
    result = compute_dimension("srp")
    assert result.score is None and result.status == "unavailable"


@pytest.mark.django_db
def test_srp_scores_from_claims(django_user_model, sde):
    _srp_program()
    from apps.srp.models import SrpClaim

    _claim(django_user_model, 5001, SrpClaim.Status.SUBMITTED)
    _claim(django_user_model, 5002, SrpClaim.Status.PAID)
    result = compute_dimension("srp")
    assert result.score is not None
    backlog = next(k for k in result.kpis if k.key == "srp.pending_backlog")
    assert backlog.value == 1  # one SUBMITTED claim


@pytest.mark.django_db
def test_srp_old_claim_emits_finding(django_user_model, sde):
    _srp_program()
    from apps.srp.models import SrpClaim

    _claim(django_user_model, 5101, SrpClaim.Status.SUBMITTED, created_days_ago=20)  # > 14d max
    result = compute_dimension("srp")
    assert any(f.kpi_key == "srp.oldest_claim" for f in result.findings)


# --- index integration (disabled by default) ---------------------------------
@pytest.mark.django_db
def test_new_dims_disabled_by_default_excluded_from_index(django_user_model, sde):
    from apps.srp.models import SrpClaim

    _wallet(balance="20000000000")
    _srp_program()
    _claim(django_user_model, 5201, SrpClaim.Status.SUBMITTED)
    result = compute_readiness(use_cache=False)
    # financial/srp ship disabled → not scored, not in the index.
    assert "financial" not in result["dimensions"]
    assert "srp" not in result["dimensions"]


@pytest.mark.django_db
def test_stored_doc_predating_new_dims_keeps_them_disabled():
    # A dimensions doc written before financial/srp existed (only the original four).
    # The new dims must come from DEFAULTS (disabled) via the merge — never fall
    # through to the engine's "default on" path — so they stay out of the index.
    from apps.admin_audit.models import AppSetting

    legacy = {
        k: {"enabled": True, "weight": 1.0, "thresholds": {"amber": 60, "red": 40}}
        for k in ("doctrine", "skill", "stock", "logistics")
    }
    AppSetting.objects.create(key="readiness.dimensions", value=legacy)
    dims = config.get("dimensions")
    assert dims["financial"]["enabled"] is False
    assert dims["srp"]["enabled"] is False

    _wallet(balance="20000000000")
    result = compute_readiness(use_cache=False)
    assert "financial" not in result["dimensions"]
    assert "srp" not in result["dimensions"]


@pytest.mark.django_db
def test_enabling_financial_adds_it_to_index():
    _wallet(balance="20000000000")
    doc = copy.deepcopy(config.DEFAULTS["dimensions"])
    doc["financial"]["enabled"] = True
    config.set("dimensions", doc, user=None)
    result = compute_readiness(use_cache=False)
    assert "financial" in result["dimensions"]
    assert result["dimensions"]["financial"] is not None


# --- drill-down --------------------------------------------------------------
@pytest.mark.django_db
def test_drilldown_is_officer_only(client, django_user_model):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get("/readiness/d/financial/").status_code == 403


@pytest.mark.django_db
def test_drilldown_renders_disabled_dimension_preview(client, django_user_model):
    _wallet(balance="20000000000")
    client.force_login(_officer(django_user_model))
    html = client.get("/readiness/d/financial/").content.decode()
    assert "Financial Health" in html
    assert "financial.runway_months" in html
    assert "disabled" in html  # preview banner (not yet in the index)


@pytest.mark.django_db
def test_drilldown_unknown_dimension_404(client, django_user_model):
    client.force_login(_officer(django_user_model, "off2"))
    assert client.get("/readiness/d/nope/").status_code == 404
