"""P4 / WS4: contract matcher, payment reconcile, overdue sweep, reliability rollup."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.corporation.models import CorpWalletJournalEntry
from apps.logistics.models import CorpContract
from apps.procurement import contracts, metrics, payments
from apps.procurement.models import (
    PoReceipt,
    ProcurementConfig,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplyAgreement,
)

pytestmark = pytest.mark.django_db

S = PurchaseOrder.Status


def _cfg(**kw):
    cfg = ProcurementConfig.active()
    for k, v in kw.items():
        setattr(cfg, k, v)
    cfg.save()
    return cfg


def _supplier(kind=Supplier.Kind.PILOT, entity_id=1001):
    return Supplier.objects.create(kind=kind, entity_id=entity_id, display_name="S")


def _po(supplier, *, status=S.APPROVED, total="1000000000", qty=10, contract_id=None,
        received=0):
    po = PurchaseOrder.objects.create(
        supplier=supplier, status=status, expected_total_isk=Decimal(total),
        approved_at=timezone.now() - timedelta(hours=1), contract_id=contract_id,
    )
    PurchaseOrderLine.objects.create(
        po=po, type_id=587, quantity_ordered=qty, quantity_received=received,
        unit_price_isk=Decimal("100000000"),
    )
    return po


_CID = [70000]


def _contract(*, issuer_id=1001, issuer_corp=None, price="1000000000",
              status="outstanding", offset_h=1):
    _CID[0] += 1
    return CorpContract.objects.create(
        contract_id=_CID[0], type="item_exchange", status=status,
        issuer_id=issuer_id, issuer_corporation_id=issuer_corp, price=Decimal(price),
        date_issued=timezone.now() - timedelta(hours=offset_h),
    )


# --- matcher ------------------------------------------------------------------

def test_single_candidate_auto_matches_and_copies_evidence(monkeypatch):
    calls = {"n": 0}

    def fake_items(cid, *, corp_id=None, client=None):
        calls["n"] += 1
        return [{"type_id": 587, "quantity": 10}]

    monkeypatch.setattr(contracts, "fetch_contract_items", fake_items)
    _cfg(match_enabled=True)
    s = _supplier()
    po = _po(s)
    c = _contract(issuer_id=s.entity_id)
    result = contracts.match_contracts()
    assert result["matched"] == 1
    po.refresh_from_db()
    assert po.contract_id == c.contract_id
    assert po.status == S.CONTRACT_AVAILABLE
    assert po.contract_price == Decimal("1000000000")
    assert po.contract_matched_at is not None
    assert po.contract_items == [{"type_id": 587, "quantity": 10}]
    assert calls["n"] == 1  # items fetched exactly once


def test_two_candidates_are_offered_never_taken(monkeypatch):
    monkeypatch.setattr(contracts, "fetch_contract_items", lambda *a, **k: [])
    _cfg(match_enabled=True)
    s = _supplier()
    po = _po(s)
    _contract(issuer_id=s.entity_id)
    _contract(issuer_id=s.entity_id)
    result = contracts.match_contracts()
    assert result["matched"] == 0 and result["suggested"] == 1
    po.refresh_from_db()
    assert po.contract_id is None


def test_corp_supplier_matches_on_issuer_corporation_id(monkeypatch):
    monkeypatch.setattr(contracts, "fetch_contract_items", lambda *a, **k: [])
    _cfg(match_enabled=True)
    s = _supplier(kind=Supplier.Kind.CORP, entity_id=2002)
    po = _po(s)
    # Issued by some pilot but on behalf of corp 2002 → matches the corp supplier.
    good = _contract(issuer_id=555, issuer_corp=2002)
    # A pilot-issued contract with no corp affiliation must NOT match a corp supplier.
    _contract(issuer_id=2002, issuer_corp=None)
    result = contracts.match_contracts()
    assert result["matched"] == 1
    po.refresh_from_db()
    assert po.contract_id == good.contract_id


def test_matcher_is_inert_until_armed(monkeypatch):
    monkeypatch.setattr(contracts, "fetch_contract_items", lambda *a, **k: [])
    s = _supplier()
    po = _po(s)
    _contract(issuer_id=s.entity_id)
    assert contracts.match_contracts() == {"status": "disabled"}
    po.refresh_from_db()
    assert po.contract_id is None


def test_matched_evidence_survives_snapshot_rebuild(monkeypatch):
    monkeypatch.setattr(contracts, "fetch_contract_items", lambda *a, **k: [{"type_id": 587, "quantity": 10}])
    _cfg(match_enabled=True)
    s = _supplier()
    po = _po(s)
    c = _contract(issuer_id=s.entity_id)
    contracts.match_contracts()
    # The hourly snapshot is delete-all rebuilt and this contract has aged out.
    CorpContract.objects.all().delete()
    contracts.refresh_matched_contracts()
    po.refresh_from_db()
    assert po.contract_id == c.contract_id  # copied evidence kept
    assert po.contract_price == Decimal("1000000000")


# --- payment reconcile --------------------------------------------------------

def _journal(entry_id, *, context_id, amount="-1000000000", ref="contract_price_payment_corp",
             offset_min=1, second_party=None):
    return CorpWalletJournalEntry.objects.create(
        entry_id=entry_id, division=1, date=timezone.now() - timedelta(minutes=offset_min),
        ref_type=ref, amount=Decimal(amount), context_id=context_id,
        second_party_id=second_party,
    )


def test_reconcile_is_inert_until_armed():
    s = _supplier()
    po = _po(s, status=S.DELIVERED, received=10, contract_id=42)
    po.contract_matched_at = timezone.now() - timedelta(hours=2)
    po.save(update_fields=["contract_matched_at"])
    _journal(1, context_id=42)
    assert payments.reconcile_payments() == {"status": "disabled"}
    po.refresh_from_db()
    assert po.paid_entry_id is None


def test_exact_context_id_settles_and_reconciles():
    _cfg(reconcile_enabled=True)
    s = _supplier()
    po = _po(s, status=S.DELIVERED, received=10, contract_id=42)
    po.contract_matched_at = timezone.now() - timedelta(hours=2)
    po.save(update_fields=["contract_matched_at"])
    _journal(1, context_id=42)
    result = payments.reconcile_payments()
    assert result["settled"] == 1
    po.refresh_from_db()
    assert po.paid_entry_id == 1
    assert po.paid_amount_isk == Decimal("1000000000")
    assert po.status == S.RECONCILED
    # Re-run is idempotent.
    assert payments.reconcile_payments()["settled"] == 0


def test_wrong_context_id_never_matches():
    _cfg(reconcile_enabled=True)
    s = _supplier()
    po = _po(s, status=S.DELIVERED, received=10, contract_id=42)
    po.contract_matched_at = timezone.now() - timedelta(hours=2)
    po.save(update_fields=["contract_matched_at"])
    _journal(1, context_id=99)  # a similar payment for a different contract
    assert payments.reconcile_payments()["settled"] == 0
    po.refresh_from_db()
    assert po.paid_entry_id is None


def test_overdue_po_settles_and_keeps_overdue_since():
    _cfg(reconcile_enabled=True)
    s = _supplier()
    po = _po(s, status=S.OVERDUE, received=10, contract_id=42)
    po.overdue_since = timezone.now() - timedelta(days=3)
    po.contract_matched_at = timezone.now() - timedelta(hours=2)
    po.save(update_fields=["overdue_since", "contract_matched_at"])
    _journal(1, context_id=42)
    payments.reconcile_payments()
    po.refresh_from_db()
    assert po.paid_entry_id == 1
    assert po.status == S.RECONCILED
    assert po.overdue_since is not None


# --- overdue sweep + agreement expiry -----------------------------------------

def test_sweep_flags_overdue_once_and_is_stable():
    _cfg(overdue_sweep_enabled=True, overdue_grace_days=2)
    s = _supplier()
    po = _po(s, status=S.APPROVED)
    po.promised_by = timezone.now() - timedelta(days=5)
    po.save(update_fields=["promised_by"])
    payments.sweep_overdue()
    po.refresh_from_db()
    assert po.status == S.OVERDUE
    first = po.overdue_since
    assert first is not None
    # A second sweep neither re-stamps overdue_since nor changes anything.
    payments.sweep_overdue()
    po.refresh_from_db()
    assert po.overdue_since == first


def test_sweep_respects_the_grace_window():
    _cfg(overdue_sweep_enabled=True, overdue_grace_days=2)
    s = _supplier()
    po = _po(s, status=S.APPROVED)
    po.promised_by = timezone.now() - timedelta(hours=6)  # late, but inside grace
    po.save(update_fields=["promised_by"])
    payments.sweep_overdue()
    po.refresh_from_db()
    assert po.status == S.APPROVED


def test_sweep_expires_lapsed_agreements():
    _cfg(overdue_sweep_enabled=True)
    s = _supplier()
    ag = SupplyAgreement.objects.create(
        supplier=s, status=SupplyAgreement.Status.ACTIVE,
        term_end=timezone.now().date() - timedelta(days=1),
    )
    result = payments.sweep_overdue()
    assert result["expired"] == 1
    ag.refresh_from_db()
    assert ag.status == SupplyAgreement.Status.EXPIRED


# --- reliability rollup -------------------------------------------------------

def test_rollup_is_inert_until_armed():
    _supplier()
    assert metrics.rollup_reliability() == {"status": "disabled"}


def test_rollup_computes_and_is_re_run_stable():
    _cfg(reliability_rollup_enabled=True, reliability_window_weeks=8, overdue_grace_days=2)
    s = _supplier()
    po = _po(s, status=S.DELIVERED, received=10, contract_id=None)
    po.promised_by = timezone.now() - timedelta(days=10)  # a prior complete week
    po.save(update_fields=["promised_by"])
    line = po.lines.get()
    r = PoReceipt.objects.create(
        po=po, line=line, quantity=10, unit_jita_at_receipt=Decimal("80000000"),
    )
    # Backdate the receipt to before promise+grace so it counts as on-time.
    PoReceipt.objects.filter(pk=r.pk).update(created_at=po.promised_by - timedelta(days=1))

    metrics.rollup_reliability()
    s.refresh_from_db()
    assert s.reliability_sample == 1
    assert s.on_time_rate == Decimal("1.0000")
    assert s.fill_rate == Decimal("1.0000")
    # (unit_price 100M - jita 80M)/80M = 0.25
    assert s.price_variance_pct == Decimal("0.2500")
    first = (s.on_time_rate, s.fill_rate, s.price_variance_pct)
    # Re-run reads only frozen columns → identical result.
    metrics.rollup_reliability()
    s.refresh_from_db()
    assert (s.on_time_rate, s.fill_rate, s.price_variance_pct) == first
