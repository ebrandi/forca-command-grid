"""PCC-2 — private, non-competitive contribution trend + active-week streaks."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.pilots.services import personal_trend, record_contribution
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

NOW = dt.datetime(2026, 6, 18, 12, 0, tzinfo=dt.UTC)


def _member(django_user_model, name):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    return user


def _at(y, m, d):
    return dt.datetime(y, m, d, 12, 0, tzinfo=dt.UTC)


# --- sparkline ---------------------------------------------------------------
def test_sparkline_buckets_by_month_and_scales_to_own_peak(django_user_model):
    u = _member(django_user_model, "eve:t1")
    record_contribution(u, "build", 5, "ships", ref_type="j", ref_id="1", occurred_at=_at(2026, 4, 10))
    record_contribution(u, "build", 10, "ships", ref_type="j", ref_id="2", occurred_at=_at(2026, 6, 5))
    trend = personal_trend(u, months=6, now=NOW)
    assert trend["month_labels"] == ["Jan", "Feb", "Mar", "Apr", "May", "Jun"]
    build = next(k for k in trend["kinds"] if k["kind"] == "build")
    assert build["series"][3] == Decimal("5")     # April
    assert build["series"][5] == Decimal("10")     # June
    assert build["peak"] == Decimal("10")
    assert build["bars"][3] == 50 and build["bars"][5] == 100  # scaled to own peak


def test_trend_is_private_to_the_pilot(django_user_model):
    me = _member(django_user_model, "eve:me")
    other = _member(django_user_model, "eve:other")
    record_contribution(me, "haul", 3, "loads", ref_type="h", ref_id="1", occurred_at=_at(2026, 6, 1))
    record_contribution(other, "haul", 99, "loads", ref_type="h", ref_id="2", occurred_at=_at(2026, 6, 1))
    trend = personal_trend(me, months=6, now=NOW)
    haul = next(k for k in trend["kinds"] if k["kind"] == "haul")
    assert haul["total"] == Decimal("3")  # never includes the other pilot's 99


def test_empty_trend_has_no_kinds(django_user_model):
    u = _member(django_user_model, "eve:empty")
    trend = personal_trend(u, months=6, now=NOW)
    assert trend["kinds"] == []
    assert trend["longest_streak"] == 0 and trend["current_streak"] == 0


# --- streaks -----------------------------------------------------------------
def test_longest_and_current_streak(django_user_model):
    u = _member(django_user_model, "eve:streak")
    # three consecutive ISO weeks ending at 'now'
    for i, day in enumerate((3, 10, 17)):  # Wed 2026-06-03/10/17 → weeks 06-01/08/15
        record_contribution(u, "task", 1, "tasks", ref_type="t", ref_id=str(i), occurred_at=_at(2026, 6, day))
    trend = personal_trend(u, months=6, now=NOW)
    assert trend["longest_streak"] == 3
    assert trend["current_streak"] == 3  # last active week is this week


def test_current_streak_zero_when_stale(django_user_model):
    u = _member(django_user_model, "eve:stale")
    # two consecutive weeks, but months ago → not "current"
    record_contribution(u, "task", 1, "tasks", ref_type="t", ref_id="1", occurred_at=_at(2026, 2, 4))
    record_contribution(u, "task", 1, "tasks", ref_type="t", ref_id="2", occurred_at=_at(2026, 2, 11))
    trend = personal_trend(u, months=6, now=NOW)
    assert trend["longest_streak"] == 2
    assert trend["current_streak"] == 0


# --- view --------------------------------------------------------------------
def test_contributions_page_shows_trajectory(client, django_user_model):
    u = _member(django_user_model, "eve:view")
    record_contribution(u, "build", 4, "ships", ref_type="j", ref_id="1")
    client.force_login(u)
    resp = client.get(reverse("pilots:contributions"))
    assert resp.status_code == 200
    assert b"Your trajectory" in resp.content
