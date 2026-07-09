"""Regression tests for the 2026-07 security hardening round (audit fixes S-1/3/5/6/7/8)."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from core import rbac
from tests._raffle_utils import add_token, enrol_pilot, make_user

pytestmark = pytest.mark.django_db


# --- S-7 · combat-rank reward separation of duties ------------------------- #
def test_reward_self_approval_denied(django_user_model):
    from apps.killboard import rewards
    from apps.killboard.models import RankRewardEvent, RewardType

    beneficiary = make_user(django_user_model, "beneficiary", rbac.ROLE_DIRECTOR)
    approver = make_user(django_user_model, "approver", rbac.ROLE_OFFICER)
    ev = RankRewardEvent.objects.create(
        character_id=111, character_name="P", user=beneficiary, rank_name="Ace",
        rank_min_kills=1000, reward_type=RewardType.ISK, reward_amount=Decimal("1000000"),
        status=RankRewardEvent.Status.PENDING,
    )
    with pytest.raises(PermissionDenied):
        rewards.approve(ev, beneficiary)           # can't approve your own reward
    rewards.approve(ev, approver)                  # a different actor can
    assert ev.status == RankRewardEvent.Status.APPROVED
    with pytest.raises(PermissionDenied):
        rewards.mark_paid(ev, beneficiary)         # can't pay your own reward either


# --- S-8 · superseded tokens lose their decryptable ciphertext -------------- #
def test_login_prune_blanks_superseded_ciphertext(django_user_model):
    from apps.sso import services

    _user, char = enrol_pilot(django_user_model, 6001, with_token=False)
    old = add_token(char, scopes=["publicData"])
    assert old._refresh_token  # encrypted, non-empty
    tok = SimpleNamespace(token_type="Bearer", expires_in=1200,
                          refresh_token="new-rt", access_token="new-at")
    services.store_token(char, tok, ["publicData"])  # new token covers old's scopes → prune
    old.refresh_from_db()
    assert old.revoked_at is not None
    assert old._refresh_token == "" and old._access_token == ""


# --- S-3 · member can't self-credit on a hidden DRAFT op -------------------- #
def test_member_cannot_attend_draft_op(client, django_user_model):
    from apps.operations.models import Operation, OperationAttendance
    from apps.pilots.models import ContributionEvent

    user, _char = enrol_pilot(django_user_model, 6100)
    op = Operation.objects.create(name="Black op", status=Operation.Status.DRAFT)
    client.force_login(user)
    assert client.post(reverse("operations:attend", args=[op.pk])).status_code == 404
    assert not OperationAttendance.objects.filter(operation=op).exists()
    assert not ContributionEvent.objects.filter(kind=ContributionEvent.Kind.FLEET).exists()


# --- S-1 · officer can't act on an above-clearance COA ---------------------- #
def test_officer_cannot_accept_above_clearance_coa(client, django_user_model):
    from apps.command_intel.models import Classification, CourseOfAction, IntelligenceReport

    officer = make_user(django_user_model, "officer1", rbac.ROLE_OFFICER)
    dr = IntelligenceReport.objects.create(
        classification=Classification.DIRECTOR_EYES_ONLY,
        status=IntelligenceReport.Status.READY,
    )
    coa = CourseOfAction.objects.create(report=dr, objective="Director-only plan")
    client.force_login(officer)
    assert client.post(reverse("command_intel:coa_accept", args=[coa.pk])).status_code == 403
    coa.refresh_from_db()
    assert coa.state == CourseOfAction.State.PROPOSED  # untouched


# --- S-5 · LLM report generation is rate-limited ---------------------------- #
def test_report_generation_rate_limited(django_user_model, monkeypatch):
    from apps.command_intel import services
    from apps.command_intel.models import IntelligenceReport

    user = make_user(django_user_model, "spammer", rbac.ROLE_OFFICER)
    real_get = services.config.get

    def fake_get(domain):
        d = dict(real_get(domain))
        if domain == "provider":
            d["rate_limit_per_hour"] = 1
        return d

    monkeypatch.setattr(services.config, "get", fake_get)

    r1 = services.request_report(user=user)
    assert r1.status != IntelligenceReport.Status.FAILED
    # Take r1 out of the in-flight set so dedup doesn't short-circuit the next call.
    IntelligenceReport.objects.filter(pk=r1.pk).update(status=IntelligenceReport.Status.READY)
    r2 = services.request_report(user=user)        # 2nd within the hour, cap = 1 → refused
    assert r2.status == IntelligenceReport.Status.FAILED
    assert "Rate limit" in r2.error


# --- S-6 · buyback lot detail is object-scoped ------------------------------ #
def test_buyback_offer_detail_object_scoped(client, django_user_model):
    from apps.buyback.models import Audience, BuybackConfig, BuybackOffer
    from apps.buyback.services import invalidate_audience_cache

    BuybackConfig.objects.create(name="T", is_active=True, audience=Audience.PUBLIC)
    invalidate_audience_cache()
    seller = make_user(django_user_model, "bseller", rbac.ROLE_MEMBER)
    snoop = make_user(django_user_model, "bsnoop", rbac.ROLE_MEMBER)
    cancelled = BuybackOffer.objects.create(
        seller=seller, status=BuybackOffer.Status.CANCELLED, items=[],
        jita_total=Decimal("1"), offer_total=Decimal("1"),
    )
    open_lot = BuybackOffer.objects.create(
        seller=seller, status=BuybackOffer.Status.OPEN, items=[],
        jita_total=Decimal("1"), offer_total=Decimal("1"),
    )
    client.force_login(snoop)
    # can't enumerate another member's settled/cancelled lot…
    assert client.get(reverse("buyback:offer", args=[cancelled.pk])).status_code == 403
    # …but an OPEN lot is board-visible to members.
    assert client.get(reverse("buyback:offer", args=[open_lot.pk])).status_code == 200
    client.force_login(seller)
    assert client.get(reverse("buyback:offer", args=[cancelled.pk])).status_code == 200  # owner
