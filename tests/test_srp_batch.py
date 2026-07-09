"""SRP-4 (roadmap 3.17) — batch review after a fleet wipe.

Approve every eligible submitted claim / settle every approved claim in one action, against a
single reference, with separation of duties preserved (the officer's own claims are skipped).
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.srp.models import SrpClaim, SrpProgram
from apps.srp.services import batch_approve, batch_pay
from apps.sso.services import ensure_role
from core import rbac
from tests._raffle_utils import HOME_CORP, home_kill

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _program():
    return SrpProgram.objects.create(name="Standard", is_active=True)


def _officer(django_user_model, name="officer"):
    u = django_user_model.objects.create_user(username=name, password="x")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_OFFICER))
    return u


def _member(django_user_model, name):
    return django_user_model.objects.create_user(username=name, password="x")


def _claim(claimant, km_id, *, status=SrpClaim.Status.SUBMITTED, payout="10000000"):
    km = home_kill(km_id, attackers=[(1, HOME_CORP, True)])
    return SrpClaim.objects.create(killmail=km, claimant=claimant, status=status,
                                   computed_payout=Decimal(payout))


def test_batch_approve_all_submitted(django_user_model):
    officer = _officer(django_user_model)
    m1, m2 = _member(django_user_model, "m1"), _member(django_user_model, "m2")
    _claim(m1, 1)
    _claim(m2, 2)
    _claim(m1, 3)
    r = batch_approve(officer)
    assert r["approved"] == 3 and r["skipped"] == 0 and len(r["claim_ids"]) == 3
    assert SrpClaim.objects.filter(status=SrpClaim.Status.APPROVED).count() == 3


def test_batch_approve_skips_officers_own_claim(django_user_model):
    officer = _officer(django_user_model)
    m1 = _member(django_user_model, "m1")
    _claim(m1, 1)
    _claim(officer, 2)  # the officer's own — SoD requires another officer
    r = batch_approve(officer)
    assert r["approved"] == 1 and r["skipped"] == 1
    assert SrpClaim.objects.get(claimant=officer).status == SrpClaim.Status.SUBMITTED


def test_batch_pay_all_approved_against_one_reference(django_user_model):
    officer = _officer(django_user_model)
    m1, m2 = _member(django_user_model, "m1"), _member(django_user_model, "m2")
    _claim(m1, 1, status=SrpClaim.Status.APPROVED)
    _claim(m2, 2, status=SrpClaim.Status.APPROVED)
    r = batch_pay(officer, reference="OP-WIPE-07")
    assert r["paid"] == 2 and r["skipped"] == 0 and len(r["claim_ids"]) == 2
    paid = SrpClaim.objects.filter(status=SrpClaim.Status.PAID)
    assert paid.count() == 2
    assert all(c.payment_reference == "OP-WIPE-07" for c in paid)


def test_batch_pay_skips_officers_own_claim(django_user_model):
    officer = _officer(django_user_model)
    m1 = _member(django_user_model, "m1")
    _claim(m1, 1, status=SrpClaim.Status.APPROVED)
    _claim(officer, 2, status=SrpClaim.Status.APPROVED)
    r = batch_pay(officer, reference="ref")
    assert r["paid"] == 1 and r["skipped"] == 1
    assert SrpClaim.objects.get(claimant=officer).status == SrpClaim.Status.APPROVED


def test_queue_shows_batch_button_and_batch_endpoint_works(client, django_user_model):
    officer = _officer(django_user_model)
    m1 = _member(django_user_model, "m1")
    for i in range(3):
        _claim(m1, 10 + i)
    client.force_login(officer)
    resp = client.get(reverse("srp:queue"))
    assert resp.status_code == 200 and b"Approve all eligible" in resp.content
    resp = client.post(reverse("srp:batch_approve"))
    assert resp.status_code == 302
    assert SrpClaim.objects.filter(status=SrpClaim.Status.APPROVED).count() == 3


def test_batch_endpoints_are_officer_only(client, django_user_model):
    m1 = _member(django_user_model, "m1")
    RoleAssignment.objects.create(user=m1, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(m1)
    assert client.post(reverse("srp:batch_approve")).status_code in (302, 403)
