"""Merged Corp Finance hub: windows, categories, series, forecast, gating."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.corporation.models import CorpWalletDivision, CorpWalletJournalEntry
from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, name, role):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


def _seed(days=40):
    EveCharacter.objects.create(character_id=90001, name="Ratter", is_corp_member=True)
    CorpWalletDivision.objects.create(division=1, balance=Decimal("4000000000"))
    now = timezone.now()
    eid = 1
    for i in range(days):
        CorpWalletJournalEntry.objects.create(
            entry_id=eid, division=1, date=now - dt.timedelta(days=i), ref_type="bounty_prizes",
            amount=Decimal("10000000"), first_party_id=1000125, second_party_id=90001,
            tax=Decimal("500000"))
        eid += 1
    CorpWalletJournalEntry.objects.create(
        entry_id=eid, division=1, date=now - dt.timedelta(days=2), ref_type="brokers_fee",
        amount=Decimal("-5000000"), first_party_id=90001)


# --- pure helpers ------------------------------------------------------------
def test_categorize_maps_and_humanises():
    from apps.corporation.finance_analytics import categorize

    assert categorize("bounty_prizes") == "Ratting"
    assert categorize("player_donation") == "Donations"
    assert categorize("some_new_reftype") == "Some New Reftype"  # humanised fallback
    assert categorize(None) == "Other"


def test_window_defaults_to_30d():
    from apps.corporation.finance_analytics import DEFAULT_WINDOW, resolve_window

    now = timezone.now()
    start, end, gran = resolve_window("bogus", now)        # unknown → default 30d
    assert (now - start).days == 30 and gran == "day"
    assert DEFAULT_WINDOW == "30d"


def test_horizon_days_options():
    from apps.corporation.finance_analytics import horizon_days

    now = timezone.now()
    assert horizon_days("30d", now) == 30
    assert horizon_days("60d", now) == 60
    assert horizon_days("90d", now) == 90
    eom = horizon_days("eom", now)
    assert 1 <= eom <= 31  # days remaining in the current month


# --- the dashboard -----------------------------------------------------------
@pytest.mark.django_db
def test_dashboard_aggregates_and_credits_member():
    from apps.corporation.finance_analytics import finance_dashboard

    _seed()
    d = finance_dashboard(window="30d", division=None, horizon="30d")
    assert d["income_total"] == Decimal("300000000")   # 30 days in window × 10M
    assert d["expense_total"] == Decimal("-5000000")
    assert d["net_total"] == Decimal("295000000")
    assert d["tax_total"] == Decimal("15000000")        # 30 × 0.5M
    # Member credited (not the NPC payer 1000125).
    assert d["top_earners"][0]["name"] == "Ratter"
    assert not any(e["id"] == 1000125 for e in d["top_earners"])
    # Categories + a daily series + a reconstructed balance.
    assert d["income_by_category"][0]["category"] == "Ratting"
    assert len(d["series"]) == 30 and d["series"][-1]["balance"] > 0


@pytest.mark.django_db
def test_division_filter():
    from apps.corporation.finance_analytics import finance_dashboard

    _seed()
    CorpWalletDivision.objects.create(division=2, balance=Decimal("1000000000"))
    CorpWalletJournalEntry.objects.create(entry_id=9999, division=2, date=timezone.now(),
                                          ref_type="bounty_prizes", amount=Decimal("77000000"),
                                          first_party_id=1000125, second_party_id=90001)
    only2 = finance_dashboard(window="30d", division=2, horizon="30d")
    assert only2["income_total"] == Decimal("77000000")     # just division 2
    assert only2["current_balance"] == Decimal("1000000000")


@pytest.mark.django_db
def test_forecast_declines_without_enough_history():
    from apps.corporation.finance_analytics import finance_dashboard

    EveCharacter.objects.create(character_id=90002, name="New", is_corp_member=True)
    CorpWalletDivision.objects.create(division=1, balance=Decimal("100000000"))
    CorpWalletJournalEntry.objects.create(entry_id=1, division=1, date=timezone.now(),
                                          ref_type="bounty_prizes", amount=Decimal("1000000"),
                                          first_party_id=1000125, second_party_id=90002)
    assert finance_dashboard()["forecast"]["enough"] is False


@pytest.mark.django_db
def test_forecast_projects_with_history():
    from apps.corporation.finance_analytics import finance_dashboard

    _seed()  # 40 days of positive net
    d = finance_dashboard(window="30d", horizon="60d")
    fc = d["forecast"]
    assert fc["enough"] and fc["horizon_days"] == 60
    assert fc["projected_balance"] > float(d["current_balance"])  # cash-positive trend


# --- view --------------------------------------------------------------------
@pytest.mark.django_db
def test_finance_is_director_or_admin_only(client, django_user_model, sde):
    _seed()
    client.force_login(_user(django_user_model, "officer", rbac.ROLE_OFFICER))
    assert client.get("/roster/finance/").status_code == 403
    client.force_login(_user(django_user_model, "director", rbac.ROLE_DIRECTOR))
    html = client.get("/roster/finance/").content.decode()
    assert "Corp finance" in html and "fin-balance" in html  # chart canvas present
    assert "chart.umd.js" in html  # Chart.js actually loaded, or the charts stay blank


@pytest.mark.django_db
def test_income_url_redirects_to_finance(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "director", rbac.ROLE_DIRECTOR))
    resp = client.get("/roster/income/")
    assert resp.status_code == 302 and resp.url == "/roster/finance/"
