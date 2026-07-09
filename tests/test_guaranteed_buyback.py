"""4.20 — Corp-funded guaranteed buyback with ESI settlement.

The highest-risk, financial feature. These tests pin the safety invariants: it is OFF by
default (double-gated), a member can't commit the corp above the per-lot cap, approval is
budget-capped + separation-of-duties, the app NEVER moves ISK (approval doesn't settle),
and settlement only happens on a PRECISE corp-wallet match (payout token + recipient +
amount) — never a coincidental donation.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.buyback import guaranteed as gb
from apps.buyback.models import Audience, GuaranteedBuybackConfig, GuaranteedBuyout
from apps.corporation.models import CorpWalletJournalEntry
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db


def _config(**kw):
    c = GuaranteedBuybackConfig.get_solo()
    c.enabled = True
    c.audience = Audience.CORP
    c.per_lot_cap = Decimal("100000000")
    c.daily_budget = Decimal("1000000000")
    c.require_esi_reconcile = True
    for k, v in kw.items():
        setattr(c, k, v)
    c.save()
    return c


def _member(django_user_model, name, *roles):
    u = django_user_model.objects.create(username=name)
    for r in roles or (rbac.ROLE_MEMBER,):
        RoleAssignment.objects.create(user=u, role=ensure_role(r))
    return u


def _req(user, quoted, *, char=9001):
    return gb.request_buyout(
        user, seller_character_id=char, items=[{"type_id": 34, "quantity": 1}],
        item_count=1, volume_m3=1.0, jita_value=Decimal(quoted), quoted_value=Decimal(quoted),
    )


def test_off_by_default(django_user_model):
    GuaranteedBuybackConfig.get_solo()  # defaults: enabled False, audience disabled
    u = _member(django_user_model, "m")
    assert not gb.can_request(u)
    assert _req(u, "1000000") is None
    assert gb.reconcile_settlements()["status"] == "disabled"


def test_double_gate_master_on_but_audience_disabled(django_user_model):
    _config(enabled=True, audience=Audience.DISABLED)
    assert not gb.can_request(_member(django_user_model, "m"))


def test_per_lot_cap_blocks_a_whale(django_user_model):
    _config(per_lot_cap=Decimal("50000000"))
    u = _member(django_user_model, "m")
    assert _req(u, "60000000") is None            # over the cap
    assert _req(u, "40000000") is not None         # under the cap


def test_officer_cannot_approve_own_request(django_user_model):
    _config()
    officer = _member(django_user_model, "o", rbac.ROLE_OFFICER, rbac.ROLE_MEMBER)
    b = _req(officer, "1000000")
    ok, msg = gb.approve_buyout(b.id, officer)
    assert not ok and "own" in msg.lower()
    b.refresh_from_db()
    assert b.status == GuaranteedBuyout.Status.REQUESTED


def test_approve_commits_but_moves_no_isk(django_user_model):
    _config()
    seller = _member(django_user_model, "s")
    officer = _member(django_user_model, "o", rbac.ROLE_OFFICER)
    b = _req(seller, "1000000")
    ok, _msg = gb.approve_buyout(b.id, officer)
    assert ok
    b.refresh_from_db()
    assert b.status == GuaranteedBuyout.Status.APPROVED
    assert b.settled_at is None and b.settlement_ref == ""  # NOT settled — the app moved no ISK


def test_daily_budget_cap_blocks(django_user_model):
    _config(daily_budget=Decimal("1500000"))
    seller = _member(django_user_model, "s")
    officer = _member(django_user_model, "o", rbac.ROLE_OFFICER)
    gb.approve_buyout(_req(seller, "1000000").id, officer)   # commits 1M
    b2 = _req(seller, "1000000")
    ok, msg = gb.approve_buyout(b2.id, officer)              # 1M + 1M > 1.5M
    assert not ok and "budget" in msg.lower()
    b2.refresh_from_db()
    assert b2.status == GuaranteedBuyout.Status.REQUESTED


def _approved(django_user_model, quoted="1000000", char=9001):
    _config()
    seller = _member(django_user_model, "s")
    officer = _member(django_user_model, "o", rbac.ROLE_OFFICER)
    b = _req(seller, quoted, char=char)
    gb.approve_buyout(b.id, officer)
    b.refresh_from_db()
    return b


def test_esi_reconcile_settles_on_precise_match(django_user_model):
    b = _approved(django_user_model)
    CorpWalletJournalEntry.objects.create(
        entry_id=1, division=1, date=timezone.now(), ref_type="player_donation",
        amount=Decimal("-1000000"), second_party_id=9001, reason=f"lot {b.payment_token}",
    )
    assert gb.reconcile_settlements()["settled"] == 1
    b.refresh_from_db()
    assert b.status == GuaranteedBuyout.Status.SETTLED and b.settlement_kind == "esi"
    assert b.settlement_ref == "1"


def test_esi_reconcile_ignores_untoken_and_wrong_recipient(django_user_model):
    b = _approved(django_user_model)
    # right recipient + amount but NO token → not our payment
    CorpWalletJournalEntry.objects.create(
        entry_id=2, division=1, date=timezone.now(), ref_type="player_donation",
        amount=Decimal("-1000000"), second_party_id=9001, reason="thanks mate",
    )
    # token but WRONG recipient
    CorpWalletJournalEntry.objects.create(
        entry_id=3, division=1, date=timezone.now(), ref_type="player_donation",
        amount=Decimal("-1000000"), second_party_id=9999, reason=b.payment_token,
    )
    assert gb.reconcile_settlements()["settled"] == 0
    b.refresh_from_db()
    assert b.status == GuaranteedBuyout.Status.APPROVED  # still awaiting a real payment


def test_esi_reconcile_ignores_underpayment(django_user_model):
    b = _approved(django_user_model, quoted="1000000")
    CorpWalletJournalEntry.objects.create(
        entry_id=4, division=1, date=timezone.now(), ref_type="player_donation",
        amount=Decimal("-500000"), second_party_id=9001, reason=b.payment_token,  # half
    )
    assert gb.reconcile_settlements()["settled"] == 0
    b.refresh_from_db()
    assert b.status == GuaranteedBuyout.Status.APPROVED


def test_esi_reconcile_rejects_prefix_collision_token(django_user_model):
    # CRITICAL fix: buyout token "GB-N-" must NOT match a longer buyout's "GB-N0-" payment.
    b = _approved(django_user_model)              # token e.g. "GB-7-"
    longer = b.payment_token[:-1] + "0-"          # "GB-70-" — a different (longer-pk) buyout
    CorpWalletJournalEntry.objects.create(
        entry_id=7, division=1, date=timezone.now(), ref_type="player_donation",
        amount=Decimal("-1000000"), second_party_id=9001, reason=f"paid {longer}",
    )
    assert gb.reconcile_settlements()["settled"] == 0
    b.refresh_from_db()
    assert b.status == GuaranteedBuyout.Status.APPROVED  # not falsely settled


def test_esi_reconcile_requires_full_quote(django_user_model):
    # MED fix: a 99% payment must NOT auto-settle (the corp guaranteed the full quote).
    b = _approved(django_user_model, quoted="1000000")
    CorpWalletJournalEntry.objects.create(
        entry_id=8, division=1, date=timezone.now(), ref_type="player_donation",
        amount=Decimal("-990000"), second_party_id=9001, reason=b.payment_token,  # 99%
    )
    assert gb.reconcile_settlements()["settled"] == 0
    b.refresh_from_db()
    assert b.status == GuaranteedBuyout.Status.APPROVED


def test_superuser_cannot_self_approve(django_user_model):
    _config()
    su = django_user_model.objects.create(username="su", is_superuser=True)
    RoleAssignment.objects.create(user=su, role=ensure_role(rbac.ROLE_MEMBER))
    b = _req(su, "1000000")
    ok, msg = gb.approve_buyout(b.id, su)
    assert not ok and "own" in msg.lower()  # no self-approval, superuser included
    b.refresh_from_db()
    assert b.status == GuaranteedBuyout.Status.REQUESTED


def test_manual_settle_only_when_esi_off_and_sod(django_user_model):
    b = _approved(django_user_model)
    officer = b.decided_by
    ok, _msg = gb.mark_settled_manual(b.id, officer, "ref")
    assert not ok  # ESI mode → manual settlement disabled
    _config(require_esi_reconcile=False)
    ok, _msg = gb.mark_settled_manual(b.id, officer, "wallet-123")
    assert ok
    b.refresh_from_db()
    assert b.status == GuaranteedBuyout.Status.SETTLED and b.settlement_kind == "manual"
