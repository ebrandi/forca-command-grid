"""MIN-1 — My Mining page: a pilot's self-scoped m³ / value / tickets / payout view."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.market.models import MarketPrice
from apps.mining.models import (
    MiningLedgerEntry,
    MiningObserver,
    MiningPayout,
    MiningPayoutLine,
)
from apps.mining.services import my_mining_summary, my_mining_tickets, my_payout_lines
from apps.sde.models import SdeCategory, SdeGroup, SdeType
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

DAY = dt.date(2026, 6, 20)


def _ore(type_id=18, name="Veldspar", volume=0.1, price="10"):
    cat, _ = SdeCategory.objects.get_or_create(category_id=25, defaults={"name": "Asteroid"})
    grp, _ = SdeGroup.objects.get_or_create(group_id=450, defaults={"category": cat, "name": "Veldspar"})
    SdeType.objects.get_or_create(type_id=type_id, defaults={"group": grp, "name": name, "volume": volume})
    MarketPrice.objects.create(type_id=type_id, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min=Decimal(price))


def _miner(django_user_model, uid, cids):
    user = django_user_model.objects.create(username=f"eve:{uid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    for cid in cids:
        EveCharacter.objects.create(character_id=cid, user=user, name=f"P{cid}",
                                    is_main=(cid == cids[0]), is_corp_member=True)
    return user


def _mine(cid, qty, type_id=18, day=DAY):
    obs, _ = MiningObserver.objects.get_or_create(observer_id=1)
    MiningLedgerEntry.objects.create(observer=obs, character_id=cid, character_name=f"P{cid}",
                                     type_id=type_id, quantity=qty, day=day)


# --- summary -----------------------------------------------------------------
def test_summary_aggregates_own_characters(django_user_model):
    _ore()
    _miner(django_user_model, 1, [1001, 1002])
    _mine(1001, 100)
    _mine(1002, 50)  # both the pilot's characters
    s = my_mining_summary([1001, 1002], DAY, DAY)
    assert s["total_quantity"] == 150
    assert s["total_m3"] == Decimal("15.00")     # 150 units × 0.1 m³
    assert s["total_value"] == Decimal("1500.00")  # 150 × 10 ISK
    assert len(s["rows"]) == 1 and s["rows"][0]["name"] == "Veldspar"


def test_summary_excludes_other_pilots(django_user_model):
    _ore()
    _mine(1001, 100)  # mine's
    _mine(9999, 999)  # someone else's
    s = my_mining_summary([1001], DAY, DAY)
    assert s["total_quantity"] == 100  # the other pilot's 999 is not counted


def test_summary_empty_for_no_characters():
    s = my_mining_summary([], DAY, DAY)
    assert s["rows"] == [] and s["total_value"] == Decimal("0")


# --- payout lines ------------------------------------------------------------
def test_payout_lines_owed_and_paid(django_user_model):
    user = _miner(django_user_model, 1, [1001])
    payout = MiningPayout.objects.create(name="Op", period_start=DAY, period_end=DAY)
    MiningPayoutLine.objects.create(payout=payout, character_id=1001, user=user,
                                    net=Decimal("500"), paid=False)
    MiningPayoutLine.objects.create(payout=payout, character_id=1001, user=user,
                                    net=Decimal("300"), paid=True)
    # a line belonging to someone else's character must not appear
    MiningPayoutLine.objects.create(payout=payout, character_id=7777, net=Decimal("999"), paid=False)
    result = my_payout_lines(user, [1001])
    assert result["owed"] == Decimal("500.00")
    assert result["paid"] == Decimal("300.00")
    assert len(result["lines"]) == 2


def test_payout_lines_match_by_character_even_without_user_link(django_user_model):
    user = _miner(django_user_model, 1, [1001])
    payout = MiningPayout.objects.create(name="Op", period_start=DAY, period_end=DAY)
    # line keyed by character only (user link absent at payout time) still belongs to them
    MiningPayoutLine.objects.create(payout=payout, character_id=1001, net=Decimal("200"), paid=False)
    result = my_payout_lines(user, [1001])
    assert result["owed"] == Decimal("200.00") and len(result["lines"]) == 1


def test_mining_tickets_zero_without_active_contest(django_user_model):
    user = _miner(django_user_model, 1, [1001])
    assert my_mining_tickets(user) == 0


# --- view --------------------------------------------------------------------
def test_my_mining_page_renders_own_data(client, django_user_model):
    _ore()
    user = _miner(django_user_model, 1, [1001])
    _mine(1001, 100)
    client.force_login(user)
    resp = client.get(reverse("mining:me"))
    assert resp.status_code == 200
    assert b"My mining" in resp.content
    assert b"Veldspar" in resp.content


def test_my_mining_is_self_scoped(client, django_user_model):
    _ore()
    _miner(django_user_model, 1, [1001])
    _miner(django_user_model, 2, [2002])
    _mine(2002, 500)  # the other pilot's mining
    client.force_login(django_user_model.objects.get(username="eve:1"))
    resp = client.get(reverse("mining:me"))
    assert resp.status_code == 200
    # the logged-in pilot has no mining of their own, and never sees the other's
    assert b"500" not in resp.content or b"No mining recorded for you" in resp.content


def test_my_mining_requires_login(client):
    resp = client.get(reverse("mining:me"))
    assert resp.status_code in (302, 403)
