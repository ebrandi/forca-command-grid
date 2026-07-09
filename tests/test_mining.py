"""Mining ledger: ESI sync, participation valuation, tax, payouts, contributions."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apps.identity.models import RoleAssignment
from apps.market.models import MarketPrice
from apps.mining.models import (
    MiningLedgerEntry,
    MiningObserver,
    MiningPayout,
    MiningPayoutLine,
)
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

DAY = dt.date(2026, 6, 20)


class _Resp:
    def __init__(self, data):
        self.data = data


class _Client:
    def __init__(self, observers, ledgers):
        self.observers = observers
        self.ledgers = ledgers

    def get(self, path, token=None):
        if path.endswith("/mining/observers/"):
            return _Resp(self.observers)
        if "/mining/observers/" in path:
            oid = int(path.rstrip("/").split("/")[-1])
            return _Resp(self.ledgers.get(oid, []))
        return _Resp([])


def _officer(django_user_model, name="eve:m-off"):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


def _ore_price(price="10"):
    MarketPrice.objects.create(type_id=18, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min=Decimal(price))


# --- ESI sync ---------------------------------------------------------------
@pytest.mark.django_db
def test_sync_ledger(monkeypatch, django_user_model):
    from apps.mining import sync as S

    miner = django_user_model.objects.create(username="eve:miner1")
    EveCharacter.objects.create(character_id=1001, user=miner, name="Miner One", is_corp_member=True)
    observers = [{"observer_id": 700, "observer_type": "structure", "last_updated": "2026-06-20"}]
    ledgers = {700: [{"character_id": 1001, "type_id": 18, "quantity": 5000,
                      "last_updated": "2026-06-20"}]}
    monkeypatch.setattr(S, "_token_character", lambda corp_id: type("C", (), {"character_id": 9})())
    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda ch, sc: "tok")

    res = S.sync_mining_ledger(corp_id=1, client=_Client(observers, ledgers))
    assert res["status"] == "ok" and res["entries"] == 1
    e = MiningLedgerEntry.objects.get()
    assert e.character_id == 1001 and e.quantity == 5000 and e.character_name == "Miner One"
    assert MiningObserver.objects.count() == 1

    S.sync_mining_ledger(corp_id=1, client=_Client(observers, ledgers))  # idempotent
    assert MiningLedgerEntry.objects.count() == 1


# --- Participation valuation ------------------------------------------------
@pytest.mark.django_db
def test_participation_valuation():
    from apps.mining.services import participation

    obs = MiningObserver.objects.create(observer_id=1)
    MiningLedgerEntry.objects.create(observer=obs, character_id=1, character_name="A",
                                     type_id=18, quantity=100, day=DAY)
    MiningLedgerEntry.objects.create(observer=obs, character_id=2, character_name="B",
                                     type_id=18, quantity=50, day=DAY)
    _ore_price("10")
    rows = participation(DAY, DAY)
    assert rows[0]["name"] == "A" and rows[0]["value"] == Decimal("1000.00")
    assert rows[1]["value"] == Decimal("500.00")


# --- Payout split + tax -----------------------------------------------------
@pytest.mark.django_db
def test_build_payout_by_value_withholds_tax():
    from apps.mining.services import build_payout

    obs = MiningObserver.objects.create(observer_id=1)
    MiningLedgerEntry.objects.create(observer=obs, character_id=1, character_name="A",
                                     type_id=18, quantity=100, day=DAY)
    MiningLedgerEntry.objects.create(observer=obs, character_id=2, character_name="B",
                                     type_id=18, quantity=300, day=DAY)
    _ore_price("10")
    payout = MiningPayout.objects.create(name="Op", period_start=DAY, period_end=DAY,
                                         pool_isk=Decimal("1000000"), method="by_value",
                                         tax_rate=Decimal("0.10"))
    assert build_payout(payout) == 2
    lines = {line.character_id: line for line in payout.lines.all()}
    # A=1000 value, B=3000 → 25% / 75% of a 1,000,000 pool, 10% tax withheld.
    assert lines[1].share_pct == Decimal("25.00")
    assert lines[1].gross == Decimal("250000.00")
    assert lines[1].tax == Decimal("25000.00")
    assert lines[1].net == Decimal("225000.00")
    assert lines[2].gross == Decimal("750000.00")
    payout.refresh_from_db()
    assert payout.total_value == Decimal("4000.00")  # 1000 + 3000


# --- Mark paid credits the contribution ledger ------------------------------
@pytest.mark.django_db
def test_mark_line_paid_credits_contribution(client, django_user_model):
    from apps.pilots.models import ContributionEvent

    member = django_user_model.objects.create(username="eve:miner2")
    officer = _officer(django_user_model)
    payout = MiningPayout.objects.create(name="Op", period_start=DAY, period_end=DAY,
                                         pool_isk=0, tax_rate=Decimal("0.1"))
    line = MiningPayoutLine.objects.create(payout=payout, character_id=1, character_name="A",
                                           user=member, net=Decimal("225000"))
    client.force_login(officer)
    assert client.post(f"/mining/payouts/{payout.pk}/lines/{line.id}/paid/").status_code == 302
    line.refresh_from_db()
    assert line.paid
    assert ContributionEvent.objects.filter(kind=ContributionEvent.Kind.MINING, user=member).exists()


@pytest.mark.django_db
def test_unmarking_line_paid_reverses_contribution(client, django_user_model):
    from apps.pilots.models import ContributionEvent

    member = django_user_model.objects.create(username="eve:miner3")
    officer = _officer(django_user_model)
    payout = MiningPayout.objects.create(name="Op", period_start=DAY, period_end=DAY,
                                         pool_isk=0, tax_rate=Decimal("0.1"))
    line = MiningPayoutLine.objects.create(payout=payout, character_id=1, character_name="A",
                                           user=member, net=Decimal("225000"))
    client.force_login(officer)
    url = f"/mining/payouts/{payout.pk}/lines/{line.id}/paid/"
    client.post(url)  # mark paid → credited
    assert ContributionEvent.objects.filter(kind=ContributionEvent.Kind.MINING, user=member).exists()
    client.post(url)  # toggle back to un-paid → credit reversed
    line.refresh_from_db()
    assert line.paid is False
    assert not ContributionEvent.objects.filter(kind=ContributionEvent.Kind.MINING, user=member).exists()


# --- Views ------------------------------------------------------------------
@pytest.mark.django_db
def test_ledger_tax_and_payout_create(client, django_user_model):
    from apps.mining.services import active_tax_rate

    obs = MiningObserver.objects.create(observer_id=1)
    MiningLedgerEntry.objects.create(observer=obs, character_id=1, character_name="A",
                                     type_id=18, quantity=100, day=dt.date.today())
    _ore_price("10")
    client.force_login(_officer(django_user_model, "eve:m-off2"))

    assert client.get("/mining/").status_code == 200
    client.post("/mining/tax/", {"rate_pct": "15"})
    assert active_tax_rate() == Decimal("0.1500")

    today = dt.date.today().isoformat()
    resp = client.post("/mining/payouts/create/", {
        "name": "Test Op", "period_start": today, "period_end": today,
        "pool_isk": "1000000", "method": "by_value",
    })
    assert resp.status_code == 302
    assert MiningPayout.objects.filter(name="Test Op").exists()


@pytest.mark.django_db
def test_mining_money_mutations_are_audited(client, django_user_model):
    """set_tax / payout_create / payout_recompute / payout_finalise each write an
    audit-trail row (money-movement accountability — parity with SRP/store/logistics)."""
    from apps.admin_audit.models import AuditLog

    obs = MiningObserver.objects.create(observer_id=2)
    MiningLedgerEntry.objects.create(observer=obs, character_id=2, character_name="B",
                                     type_id=18, quantity=100, day=dt.date.today())
    _ore_price("10")
    client.force_login(_officer(django_user_model, "eve:m-audit"))

    today = dt.date.today().isoformat()
    client.post("/mining/tax/", {"rate_pct": "12"})
    client.post("/mining/payouts/create/", {
        "name": "Audited Op", "period_start": today, "period_end": today,
        "pool_isk": "500000", "method": "by_value",
    })
    payout = MiningPayout.objects.get(name="Audited Op")
    client.post(f"/mining/payouts/{payout.pk}/recompute/", {"pool_isk": "600000"})
    client.post(f"/mining/payouts/{payout.pk}/finalise/")

    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"mining.set_tax", "mining.payout_create",
            "mining.payout_recompute", "mining.payout_finalise"} <= actions
    # The tax value is recorded but no raw pool secrets leak (there are none here anyway).
    tax_row = AuditLog.objects.filter(action="mining.set_tax").first()
    assert tax_row is not None and tax_row.metadata.get("rate")
