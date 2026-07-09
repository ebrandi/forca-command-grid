"""Archive retrieval (P7, doc 10 §7): the classification gate (an officer never retrieves a
director-eyes-only passage), lexical ranking, and the never-empty fallback."""
from __future__ import annotations

import pytest
from django.core.cache import cache

from apps.command_intel import retrieval
from apps.command_intel.models import (
    Campaign,
    Classification,
    CourseOfAction,
    IntelligenceReport,
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
    user, _ = django_user_model.objects.get_or_create(username=f"ci-ret-{role}{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


@pytest.mark.django_db
def test_excludes_reports_above_clearance(django_user_model):
    officer = _user(django_user_model, rbac.ROLE_OFFICER)
    visible = IntelligenceReport.objects.create(
        classification=Classification.HIGH_COMMAND, status=IntelligenceReport.Status.READY,
        title="Visible", summary="logistics depth is tightening",
    )
    hidden = IntelligenceReport.objects.create(
        classification=Classification.DIRECTOR_EYES_ONLY, status=IntelligenceReport.Status.READY,
        title="Hidden", summary="logistics secret capital plan",
    )
    ids = [h["id"] for h in retrieval.retrieve("logistics", officer, k=10)]
    assert f"report:{visible.pk}" in ids
    assert f"report:{hidden.pk}" not in ids  # the classification gate holds in retrieval


@pytest.mark.django_db
def test_ranks_the_more_relevant_report_first(django_user_model):
    officer = _user(django_user_model, rbac.ROLE_OFFICER)
    IntelligenceReport.objects.create(
        classification=Classification.HIGH_COMMAND, status=IntelligenceReport.Status.READY,
        title="Finance", summary="isk runway and wallet burn",
    )
    match = IntelligenceReport.objects.create(
        classification=Classification.HIGH_COMMAND, status=IntelligenceReport.Status.READY,
        title="Logistics", summary="logistics pilots and staged hulls are short",
    )
    hits = retrieval.retrieve("logistics pilots staged hulls", officer, k=10)
    assert hits[0]["id"] == f"report:{match.pk}"


@pytest.mark.django_db
def test_report_less_coa_is_never_surfaced(django_user_model):
    # A COA orphaned from its (possibly classified) report has no clearance of its own —
    # it must never reach an officer's retrieval (the HIGH finding).
    officer = _user(django_user_model, rbac.ROLE_OFFICER)
    CourseOfAction.objects.create(
        slug="orphan/secret", objective="secret director-only decision", state="dismissed",
        report=None,
    )
    ids = [h["id"] for h in retrieval.retrieve("secret director decision", officer, k=10)]
    assert not any(i.startswith("coa:") for i in ids)


@pytest.mark.django_db
def test_campaign_from_a_hidden_report_is_not_surfaced(django_user_model):
    officer = _user(django_user_model, rbac.ROLE_OFFICER)  # not a director
    hidden = IntelligenceReport.objects.create(
        classification=Classification.DIRECTOR_EYES_ONLY, status=IntelligenceReport.Status.READY,
        title="Hidden", summary="x",
    )
    camp = Campaign.objects.create(
        objective="deploy capitals covertly", target_metric="readiness.overall",
        created_from_report=hidden,
    )
    ids = [h["id"] for h in retrieval.retrieve("deploy capitals", officer, k=10)]
    assert f"campaign:{camp.pk}" not in ids


@pytest.mark.django_db
def test_falls_back_to_recent_when_nothing_matches(django_user_model):
    officer = _user(django_user_model, rbac.ROLE_OFFICER)
    IntelligenceReport.objects.create(
        classification=Classification.HIGH_COMMAND, status=IntelligenceReport.Status.READY,
        title="Posture", summary="fleet readiness",
    )
    # A query with no lexical overlap still returns context (never an empty answer).
    hits = retrieval.retrieve("zzzznonsense qqqterm", officer, k=5)
    assert hits, "retrieval falls back to recent passages rather than returning nothing"
