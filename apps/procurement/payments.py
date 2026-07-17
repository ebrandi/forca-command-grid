"""P4 payment reconcile + overdue sweep + agreement expiry.

Payment matching is evidence or nothing: the primary match is the wallet journal's
``context_id`` == the PO's ``contract_id`` (the strongest evidence). Null-context
legacy rows are never amount-guessed automatically — the console offers a
conservative suggestion for one-click officer confirmation. Ships inert behind
``ProcurementConfig`` flags.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from core.audit import audit_log

from .models import ProcurementConfig, PurchaseOrder, SupplyAgreement
from .services import ProcurementError, S, apply_derived_state

# A payment may land against any of these; OVERDUE included (late payment evidence).
_SETTLEABLE = (S.DELIVERED, S.PARTIAL, S.ACCEPTED, S.OVERDUE)
_SWEEPABLE = (S.APPROVED, S.CONTRACT_EXPECTED, S.CONTRACT_AVAILABLE, S.ACCEPTED, S.PARTIAL)


def _used_entry_ids() -> set:
    return set(
        PurchaseOrder.objects.exclude(paid_entry_id__isnull=True)
        .values_list("paid_entry_id", flat=True)
    )


def _settle(po: PurchaseOrder, entry, *, actor=None) -> bool:
    """Record the payment evidence on the PO under a row lock, then recompute state
    (fully delivered + paid → RECONCILED via apply_derived_state)."""
    with transaction.atomic():
        locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
        if locked.paid_entry_id:
            return False
        locked.paid_entry_id = entry.entry_id
        locked.paid_at = entry.date
        locked.paid_amount_isk = -entry.amount  # amount < 0 (money out) → store positive
        locked.save(update_fields=["paid_entry_id", "paid_at", "paid_amount_isk", "updated_at"])
        apply_derived_state(locked, actor=None)
    audit_log(actor, "procurement.payment_settled", target_type="purchase_order",
              target_id=str(po.pk), metadata={"entry_id": entry.entry_id})
    return True


def reconcile_payments() -> dict:
    """Auto-settle contract-linked POs whose payment lands with an exact
    ``context_id`` match. No-op unless armed."""
    cfg = ProcurementConfig.active()
    if not cfg.reconcile_enabled:
        return {"status": "disabled"}
    from apps.corporation.models import CorpWalletJournalEntry

    ref_types = cfg.reconcile_ref_types or []
    used = _used_entry_ids()
    settled = 0
    candidates = PurchaseOrder.objects.filter(
        status__in=_SETTLEABLE, paid_entry_id__isnull=True, contract_id__isnull=False,
    )
    for po in candidates:
        entry = (
            CorpWalletJournalEntry.objects.filter(
                ref_type__in=ref_types, context_id=po.contract_id, amount__lt=0,
                date__gte=po.contract_matched_at or po.created_at,
            ).order_by("date").first()
        )
        if entry is None or entry.entry_id in used:
            continue
        if _settle(po, entry):
            used.add(entry.entry_id)
            settled += 1
    return {"status": "ok", "settled": settled}


def payment_suggestions(po: PurchaseOrder, *, limit: int = 5) -> list:
    """Conservative suggestions for a PO whose payment carries no ``context_id``
    (legacy rows): the supplier as recipient, on/after the match, for at least the
    expected total, of a trusted ref_type — offered for officer confirmation, never
    auto-settled."""
    cfg = ProcurementConfig.active()
    if po.supplier.entity_id is None:
        return []
    from apps.corporation.models import CorpWalletJournalEntry

    used = _used_entry_ids()
    floor = po.contract_matched_at or po.created_at
    qs = CorpWalletJournalEntry.objects.filter(
        ref_type__in=(cfg.reconcile_ref_types or []),
        second_party_id=po.supplier.entity_id,
        context_id__isnull=True,
        amount__lte=-(po.expected_total_isk or 0),
    ).order_by("-date")
    if floor:
        qs = qs.filter(date__gte=floor)
    return [e for e in qs if e.entry_id not in used][:limit]


def confirm_payment(po: PurchaseOrder, entry_id: int, actor) -> PurchaseOrder:
    """Officer confirms a suggested payment row (the null-context path)."""
    from apps.corporation.models import CorpWalletJournalEntry

    entry = CorpWalletJournalEntry.objects.filter(entry_id=entry_id).first()
    if entry is None:
        raise ProcurementError(_("That wallet entry no longer exists."))
    if entry.entry_id in _used_entry_ids():
        raise ProcurementError(_("That payment is already matched to another order."))
    if not _settle(po, entry, actor=actor):
        raise ProcurementError(_("This order is already settled."))
    po.refresh_from_db()
    return po


def _ping_overdue(po: PurchaseOrder) -> None:
    """One officer ping when a PO first goes overdue (idempotency-keyed)."""
    try:
        from apps.pingboard import services as pingboard
        from apps.pingboard.models import AlertCategory

        supplier_name = po.supplier.display_name or f"Supplier #{po.supplier_id}"
        eta = po.promised_by.date().isoformat() if po.promised_by else ""
        pingboard.emit_broadcast(
            category=AlertCategory.INDUSTRY_JOB,
            title="Purchase order overdue",
            body=f"A purchase order from {supplier_name} is overdue (promised {eta}).",
            template="procurement.po_overdue",
            context={"supplier_name": supplier_name, "count": 1, "eta_date": eta},
            source_service="procurement",
            source_object_id=f"po_overdue:{po.pk}",
            idempotency_key=f"procurement:po_overdue:{po.pk}",
        )
    except Exception:  # noqa: BLE001 — a ping must never break the sweep
        import logging

        logging.getLogger("forca.procurement").exception("overdue ping failed (po %s)", po.pk)


def sweep_overdue() -> dict:
    """Flip past-promise POs to OVERDUE (first time: stamp ``overdue_since`` + ping)
    and expire ACTIVE agreements past their term. No-op unless armed. Compares only
    app-side facts — a missing director token stales the matcher, never fakes 'late'."""
    cfg = ProcurementConfig.active()
    if not cfg.overdue_sweep_enabled:
        return {"status": "disabled"}
    now = timezone.now()
    cutoff = now - timedelta(days=cfg.overdue_grace_days)
    overdue = expired = 0

    for po in PurchaseOrder.objects.filter(
        status__in=_SWEEPABLE, promised_by__isnull=False, promised_by__lt=cutoff,
    ):
        first_time = False
        with transaction.atomic():
            locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
            if (locked.status not in _SWEEPABLE or locked.promised_by is None
                    or locked.promised_by >= cutoff):
                continue
            first_time = locked.overdue_since is None
            locked.status = S.OVERDUE
            if first_time:
                locked.overdue_since = now
            locked.save(update_fields=["status", "overdue_since", "updated_at"])
            audit_log(None, "procurement.po_overdue", target_type="purchase_order",
                      target_id=str(locked.pk))
            po = locked
        if first_time:
            _ping_overdue(po)
        overdue += 1

    today = now.date()
    for ag in SupplyAgreement.objects.filter(
        status=SupplyAgreement.Status.ACTIVE, term_end__isnull=False, term_end__lt=today,
    ):
        with transaction.atomic():
            locked = SupplyAgreement.objects.select_for_update().get(pk=ag.pk)
            if (locked.status != SupplyAgreement.Status.ACTIVE
                    or locked.term_end is None or locked.term_end >= today):
                continue
            locked.status = SupplyAgreement.Status.EXPIRED
            locked.save(update_fields=["status", "updated_at"])
            audit_log(None, "procurement.agreement_expired", target_type="supply_agreement",
                      target_id=str(locked.pk))
        expired += 1

    return {"status": "ok", "overdue": overdue, "expired": expired}
