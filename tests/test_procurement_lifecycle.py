"""P4 / WS3: agreement dual-control + purchase-order state machine."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.procurement import services
from apps.procurement.models import (
    AgreementApproval,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplierItem,
    SupplyAgreement,
    SupplyAgreementLine,
)
from apps.procurement.services import ProcurementError
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

S = PurchaseOrder.Status


def _user(django_user_model, name, *roles, **kw):
    u = django_user_model.objects.create(username=name, **kw)
    for r in roles:
        RoleAssignment.objects.create(user=u, role=ensure_role(r))
    return u


def _supplier(**kw):
    kw.setdefault("kind", Supplier.Kind.PILOT)
    kw.setdefault("display_name", "Test Supplier")
    kw.setdefault("lead_time_days", 5)
    return Supplier.objects.create(**kw)


def _item(supplier, type_id, price, *, moq=1):
    return SupplierItem.objects.create(
        supplier=supplier, type_id=type_id, price_model=SupplierItem.PriceModel.FIXED,
        fixed_price_isk=Decimal(price), moq=moq, active=True,
    )


def _po(supplier, type_id, qty, price, *, created_by=None, moq=1, agreement=None):
    _item(supplier, type_id, price, moq=moq)
    po = PurchaseOrder.objects.create(supplier=supplier, created_by=created_by, agreement=agreement)
    PurchaseOrderLine.objects.create(po=po, type_id=type_id, quantity_ordered=qty)
    return po


# --- agreement dual-control ---------------------------------------------------

def test_agreement_below_threshold_activates_directly(django_user_model):
    officer = _user(django_user_model, "off", rbac.ROLE_OFFICER)
    s = _supplier()
    ag = SupplyAgreement.objects.create(supplier=s)
    SupplyAgreementLine.objects.create(
        agreement=ag, type_id=587, quantity_per_cycle=1,
        price_model=SupplierItem.PriceModel.FIXED, fixed_price_isk=Decimal("1000000000"),
    )
    services.submit_agreement(ag, officer)
    ag.refresh_from_db()
    assert ag.status == SupplyAgreement.Status.ACTIVE
    assert ag.estimated_cycle_value_isk == Decimal("1000000000.00")
    assert not ag.approvals.exists()


def test_agreement_above_threshold_opens_one_approval(django_user_model):
    officer = _user(django_user_model, "off", rbac.ROLE_OFFICER)
    s = _supplier()
    ag = SupplyAgreement.objects.create(supplier=s)
    SupplyAgreementLine.objects.create(
        agreement=ag, type_id=587, quantity_per_cycle=1,
        price_model=SupplierItem.PriceModel.FIXED, fixed_price_isk=Decimal("6000000000"),
    )
    services.submit_agreement(ag, officer)
    ag.refresh_from_db()
    assert ag.status == SupplyAgreement.Status.PENDING_APPROVAL
    assert ag.approvals.filter(status=AgreementApproval.Status.PENDING).count() == 1
    # A second submit collapses on the partial unique (no double request).
    with pytest.raises(ProcurementError):
        services.submit_agreement(SupplyAgreement.objects.get(pk=ag.pk), officer)


def test_threshold_uses_the_frozen_value(django_user_model):
    officer = _user(django_user_model, "off", rbac.ROLE_OFFICER)
    s = _supplier()
    ag = SupplyAgreement.objects.create(supplier=s)
    SupplyAgreementLine.objects.create(
        agreement=ag, type_id=587, quantity_per_cycle=1,
        price_model=SupplierItem.PriceModel.FIXED, fixed_price_isk=Decimal("6000000000"),
    )
    services.submit_agreement(ag, officer)
    ag.refresh_from_db()
    # The estimate is frozen on the row — later config/price moves don't rewrite it.
    assert ag.estimated_cycle_value_isk == Decimal("6000000000.00")


def test_requester_cannot_decide_even_superuser(django_user_model):
    su = _user(django_user_model, "su", rbac.ROLE_DIRECTOR, is_superuser=True)
    s = _supplier()
    ag = SupplyAgreement.objects.create(supplier=s)
    SupplyAgreementLine.objects.create(
        agreement=ag, type_id=587, quantity_per_cycle=1,
        price_model=SupplierItem.PriceModel.FIXED, fixed_price_isk=Decimal("6000000000"),
    )
    services.submit_agreement(ag, su)
    approval = ag.approvals.get()
    ok, msg = services.decide_agreement(approval.pk, su, approve=True)
    assert not ok and "own" in msg.lower()
    ag.refresh_from_db()
    assert ag.status == SupplyAgreement.Status.PENDING_APPROVAL


def test_second_director_approves_then_second_decide_is_noop(django_user_model):
    requester = _user(django_user_model, "d1", rbac.ROLE_DIRECTOR)
    decider = _user(django_user_model, "d2", rbac.ROLE_DIRECTOR)
    s = _supplier()
    ag = SupplyAgreement.objects.create(supplier=s)
    SupplyAgreementLine.objects.create(
        agreement=ag, type_id=587, quantity_per_cycle=1,
        price_model=SupplierItem.PriceModel.FIXED, fixed_price_isk=Decimal("6000000000"),
    )
    services.submit_agreement(ag, requester)
    approval = ag.approvals.get()
    ok, _msg = services.decide_agreement(approval.pk, decider, approve=True)
    assert ok
    ag.refresh_from_db()
    assert ag.status == SupplyAgreement.Status.ACTIVE
    # Re-deciding the now-decided approval is a clean no-op.
    ok2, msg2 = services.decide_agreement(approval.pk, decider, approve=False)
    assert not ok2 and "pending" in msg2.lower()


# --- PO submit / approve ------------------------------------------------------

def test_submit_rounds_quantity_up_to_moq(django_user_model):
    officer = _user(django_user_model, "off", rbac.ROLE_OFFICER)
    s = _supplier()
    po = _po(s, 587, 7, "1000000", created_by=officer, moq=10)
    services.submit_po(po, officer)
    line = po.lines.get()
    line.refresh_from_db()
    assert line.quantity_ordered == 10
    po.refresh_from_db()
    assert po.status == S.SUBMITTED


def test_approve_freezes_price_total_and_promise(django_user_model):
    creator = _user(django_user_model, "c", rbac.ROLE_OFFICER)
    approver = _user(django_user_model, "a", rbac.ROLE_OFFICER)
    s = _supplier(lead_time_days=4)
    # 2 × 0.9bn = 1.8bn — under the 2bn Director threshold, so a plain officer approves.
    po = _po(s, 587, 2, "900000000", created_by=creator)
    services.submit_po(po, creator)
    before = timezone.now()
    services.approve_po(po, approver)
    po.refresh_from_db()
    line = po.lines.get()
    assert po.status == S.APPROVED
    assert line.unit_price_isk == Decimal("900000000.00")
    assert po.expected_total_isk == Decimal("1800000000.00")
    assert po.approved_by_id == approver.id
    assert po.promised_by >= before + timedelta(days=3)


def test_approver_cannot_be_creator(django_user_model):
    creator = _user(django_user_model, "c", rbac.ROLE_OFFICER)
    s = _supplier()
    po = _po(s, 587, 1, "1000000", created_by=creator)
    services.submit_po(po, creator)
    with pytest.raises(ProcurementError):
        services.approve_po(po, creator)


def test_standalone_whale_needs_a_director(django_user_model):
    creator = _user(django_user_model, "c", rbac.ROLE_OFFICER)
    officer = _user(django_user_model, "o", rbac.ROLE_OFFICER)
    director = _user(django_user_model, "d", rbac.ROLE_DIRECTOR)
    s = _supplier()
    # 3bn > the 2bn director threshold, no agreement → standalone whale.
    po = _po(s, 587, 1, "3000000000", created_by=creator)
    services.submit_po(po, creator)
    with pytest.raises(ProcurementError):
        services.approve_po(po, officer)
    # A Director may approve it.
    services.approve_po(po, director)
    po.refresh_from_db()
    assert po.status == S.APPROVED


# --- agreement coverage -------------------------------------------------------

def _active_agreement(supplier, type_id, *, qty_per_cycle=10, max_qty=10,
                      price="3000000000", term_days=30):
    ag = SupplyAgreement.objects.create(
        supplier=supplier, status=SupplyAgreement.Status.ACTIVE,
        term_start=timezone.now().date() - timedelta(days=1),
        term_end=timezone.now().date() + timedelta(days=term_days),
    )
    SupplyAgreementLine.objects.create(
        agreement=ag, type_id=type_id, quantity_per_cycle=qty_per_cycle, max_qty=max_qty,
        price_model=SupplierItem.PriceModel.FIXED, fixed_price_isk=Decimal(price),
    )
    return ag


def test_covered_whale_needs_no_director(django_user_model):
    creator = _user(django_user_model, "c", rbac.ROLE_OFFICER)
    officer = _user(django_user_model, "o", rbac.ROLE_OFFICER)
    s = _supplier()
    ag = _active_agreement(s, 587)
    po = _po(s, 587, 1, "3000000000", created_by=creator, agreement=ag)
    services.submit_po(po, creator)
    # Covered by an active agreement → a plain officer may approve a 3bn PO.
    services.approve_po(po, officer)
    po.refresh_from_db()
    assert po.status == S.APPROVED


@pytest.mark.parametrize("mutate", ["out_of_catalogue", "over_max", "over_cycle", "expired"])
def test_coverage_misses_demote_to_standalone(mutate):
    s = _supplier()
    ag = _active_agreement(s, 587, qty_per_cycle=10, max_qty=5)
    po = PurchaseOrder.objects.create(supplier=s, agreement=ag)
    if mutate == "out_of_catalogue":
        PurchaseOrderLine.objects.create(po=po, type_id=999, quantity_ordered=1)
    elif mutate == "over_max":
        PurchaseOrderLine.objects.create(po=po, type_id=587, quantity_ordered=6)  # > max_qty 5
    elif mutate == "over_cycle":
        # Another live PO already committed the full cycle for this type.
        other = PurchaseOrder.objects.create(supplier=s, agreement=ag, status=S.APPROVED)
        PurchaseOrderLine.objects.create(po=other, type_id=587, quantity_ordered=10)
        PurchaseOrderLine.objects.create(po=po, type_id=587, quantity_ordered=1)
    elif mutate == "expired":
        ag.status = SupplyAgreement.Status.ACTIVE
        ag.term_end = timezone.now().date() - timedelta(days=1)
        ag.save(update_fields=["term_end"])
        PurchaseOrderLine.objects.create(po=po, type_id=587, quantity_ordered=1)
    assert services._agreement_covers_po(po) is False


# --- OVERDUE re-entrancy + terminal steps -------------------------------------

def test_overdue_is_re_entrant_on_receipt_evidence(django_user_model):
    s = _supplier()
    po = _po(s, 587, 10, "1000000")
    line = po.lines.get()
    po.status = S.OVERDUE
    po.overdue_since = timezone.now() - timedelta(days=1)
    po.save(update_fields=["status", "overdue_since"])
    # Half received → derived PARTIAL, overdue_since preserved.
    line.quantity_received = 5
    line.save(update_fields=["quantity_received"])
    services.apply_derived_state(po)
    po.refresh_from_db()
    assert po.status == S.PARTIAL
    assert po.overdue_since is not None
    # Fully received → DELIVERED.
    line.quantity_received = 10
    line.save(update_fields=["quantity_received"])
    services.apply_derived_state(po)
    po.refresh_from_db()
    assert po.status == S.DELIVERED
    assert po.overdue_since is not None


def test_mark_reconciled_requires_delivered_and_a_note(django_user_model):
    officer = _user(django_user_model, "o", rbac.ROLE_OFFICER)
    s = _supplier()
    po = _po(s, 587, 1, "1000000")
    po.status = S.DELIVERED
    po.save(update_fields=["status"])
    with pytest.raises(ProcurementError):
        services.mark_reconciled(po, officer, "")
    services.mark_reconciled(po, officer, "paid out of band")
    po.refresh_from_db()
    assert po.status == S.RECONCILED


def test_resolve_dispute_resume_returns_to_derived_state(django_user_model):
    officer = _user(django_user_model, "o", rbac.ROLE_OFFICER)
    s = _supplier()
    po = _po(s, 587, 10, "1000000")
    line = po.lines.get()
    line.quantity_received = 4
    line.save(update_fields=["quantity_received"])
    po.status = S.DISPUTED
    po.save(update_fields=["status"])
    services.resolve_dispute(po, officer, outcome="resume")
    po.refresh_from_db()
    assert po.status == S.PARTIAL
