"""Gap G — the standalone Weekly Executive Report page (doc 10 §5)."""
from __future__ import annotations

import datetime as dt

import pytest

from apps.identity.models import RoleAssignment
from apps.readiness.models import ExecutiveReport
from apps.sso.services import ensure_role
from core import rbac


def _officer(django_user_model, name="off"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


@pytest.mark.django_db
def test_report_page_is_officer_only(client, django_user_model):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get("/readiness/report/").status_code == 403


@pytest.mark.django_db
def test_report_page_empty_state(client, django_user_model):
    client.force_login(_officer(django_user_model))
    html = client.get("/readiness/report/").content.decode()
    assert "No report yet" in html


@pytest.mark.django_db
def test_report_page_renders_latest_body(client, django_user_model):
    ExecutiveReport.objects.create(
        period_start=dt.date(2026, 6, 15), period_end=dt.date(2026, 6, 21), index=72,
        body={"index": 72, "top_risks": [{"title": "Low fuel", "dimension": "infrastructure",
                                          "severity": "high", "owner": "logistics_director"}],
              "top_tasks": ["Refuel Keepstar"], "best": {"dimension": "doctrine", "delta": 5},
              "worst": None, "movers": []},
    )
    client.force_login(_officer(django_user_model, "off2"))
    html = client.get("/readiness/report/").content.decode()
    assert "Low fuel" in html
    assert "Refuel Keepstar" in html
    assert "72" in html


@pytest.mark.django_db
def test_report_page_selects_by_period(client, django_user_model):
    ExecutiveReport.objects.create(period_start=dt.date(2026, 6, 1), period_end=dt.date(2026, 6, 7),
                                   index=50, body={"index": 50, "top_risks": [], "top_tasks": []})
    ExecutiveReport.objects.create(period_start=dt.date(2026, 6, 8), period_end=dt.date(2026, 6, 14),
                                   index=88, body={"index": 88, "top_risks": [], "top_tasks": []})
    client.force_login(_officer(django_user_model, "off3"))
    # Default → latest (88). Explicit period → the older one (50).
    assert "88" in client.get("/readiness/report/").content.decode()
    html = client.get("/readiness/report/?period_start=2026-06-01").content.decode()
    assert "1 Jun" in html and "50" in html
