"""Gap E — score-precise alert matching (score_below / score_above on findings).

Findings carry the score of the KPI they represent (stamped centrally in the pipeline),
so a rule can fire only below/above a numeric band — not just on structural presence.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.identity.models import RoleAssignment
from apps.readiness import config
from apps.readiness.alerts import _matches
from apps.sso.services import ensure_role
from core import rbac


def _finding(**kw):
    base = {"dimension_key": "financial", "kpi_key": "financial.runway_months",
            "kind": "gap", "score": None}
    base.update(kw)
    return SimpleNamespace(**base)


# --- _matches score conditions ----------------------------------------------
def test_score_below_matches_only_scored_findings_under_threshold():
    rule = {"match": {"score_below": 50}}
    assert _matches(rule, _finding(score=30)) is True
    assert _matches(rule, _finding(score=60)) is False
    assert _matches(rule, _finding(score=None)) is False  # unscored → no match


def test_score_above_matches_over_threshold():
    rule = {"match": {"score_above": 80}}
    assert _matches(rule, _finding(score=90)) is True
    assert _matches(rule, _finding(score=80)) is False
    assert _matches(rule, _finding(score=None)) is False


def test_score_condition_combines_with_structural():
    rule = {"match": {"dimension": "financial", "score_below": 50}}
    assert _matches(rule, _finding(score=30)) is True
    assert _matches(rule, _finding(dimension_key="srp", score=30)) is False  # wrong dim


def test_no_score_condition_is_structural_only():
    assert _matches({"match": {"dimension": "financial"}}, _finding(score=None)) is True


# --- persistence: upsert stores the score -----------------------------------
@pytest.mark.django_db
def test_upsert_persists_finding_score():
    from apps.readiness.engine.base import Finding
    from apps.readiness.findings import upsert_findings
    from apps.readiness.models import ReadinessFinding

    upsert_findings([Finding(kind="gap", dimension_key="srp", kpi_key="srp.pending_backlog",
                             label="Backlog", ref_type="srp", ref_id="backlog", score=22)])
    assert ReadinessFinding.objects.get(kpi_key="srp.pending_backlog").score == 22


# --- central population: a finding inherits its KPI's score ------------------
@pytest.mark.django_db
def test_pipeline_stamps_finding_score_from_kpi(sde, django_user_model):
    from apps.readiness.models import MandatoryShip, ReadinessFinding
    from apps.readiness.services import compute_readiness
    from apps.sso.models import EveCharacter

    # Enable the strategic dimension and give it a mandatory ship nobody owns → it emits
    # a finding tied to strategic.mandatory_ship_coverage with that KPI's (low) score.
    dims = config.get("dimensions")
    dims["strategic"]["enabled"] = True
    config.set("dimensions", dims)
    EveCharacter.objects.create(character_id=5001, name="P1", is_main=True, is_corp_member=True)
    MandatoryShip.objects.create(label="Rifter", ship_type_id=587, required_quantity=1)

    compute_readiness(persist=True, use_cache=False)
    f = ReadinessFinding.objects.get(kpi_key="strategic.mandatory_ship_coverage")
    assert f.score == 0  # 0 of 1 member owns the hull → KPI score 0, stamped on the finding


# --- editor exposes score_below ---------------------------------------------
@pytest.mark.django_db
def test_alert_editor_saves_score_below(client, django_user_model):
    director = django_user_model.objects.create(username="dir")
    RoleAssignment.objects.create(user=director, role=ensure_role(rbac.ROLE_DIRECTOR))
    client.force_login(director)
    client.post("/ops/admin/readiness/alerts/save/", {
        "key": "low-fin", "severity": "high", "dimension": "financial",
        "score_below": "40", "channel_discord": "on",
    })
    rule = config.get("alerts")["rules"][0]
    assert rule["match"]["score_below"] == 40
