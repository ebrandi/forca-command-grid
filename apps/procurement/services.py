"""P4 lifecycle services: agreement dual-control + the purchase-order state machine.

Every transition is ``@transaction.atomic``, locks the subject row first, and
writes an ``audit_log`` row. The app plans and evidences — it never moves ISK and
never creates in-game contracts/jobs — so everything right of APPROVED is either
an evidence-driven transition (matcher/receipts/reconcile, actor ``None``) or an
audited officer action.

Lock order (extends the documented global order, see ``apps/erp/services.py``):
BuildJob → IndustryProject → **PurchaseOrder** → StockpileItem (pk asc) →
StockReservation, and on the fit path the PO also precedes FitStock/StoreOrder.
No P4 path locks a PO after any stock/FitStock row.
"""
from __future__ import annotations

import math
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.market.pricing import price_for
from core import rbac
from core.audit import audit_log

from .models import (
    AgreementApproval,
    ProcurementConfig,
    PurchaseOrder,
    PurchaseOrderLine,
    SupplierItem,
    SupplyAgreement,
)

_CENT = Decimal("0.01")


class ProcurementError(Exception):
    """A domain rule blocked a transition (surfaced to the officer, translated)."""


# --- status groupings ---------------------------------------------------------

S = PurchaseOrder.Status
# Counted by MRP/shipyard incoming and scanned by the matcher/reconcile engines.
COUNTED_STATUSES = frozenset({
    S.APPROVED, S.CONTRACT_EXPECTED, S.CONTRACT_AVAILABLE, S.ACCEPTED, S.PARTIAL, S.OVERDUE,
})
# A physical receipt may only post from one of these.
RECEIVABLE_STATUSES = frozenset({S.CONTRACT_AVAILABLE, S.ACCEPTED, S.PARTIAL, S.OVERDUE})
TERMINAL_STATUSES = frozenset({S.RECONCILED, S.CANCELLED})
# Progress ladder rank — the derived-state helper never downgrades a PO along it.
_PROGRESS_RANK = {
    S.APPROVED: 0, S.CONTRACT_EXPECTED: 1, S.CONTRACT_AVAILABLE: 2,
    S.ACCEPTED: 3, S.PARTIAL: 4, S.DELIVERED: 5, S.RECONCILED: 6,
}


# --- pricing helpers ----------------------------------------------------------

def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


def _unit_price(price_model: str, fixed_price_isk, premium_pct, type_id: int) -> Decimal:
    """The frozen unit price for a line: a fixed price, or the live Jita signal
    marked up by the premium. Reads the process-local price snapshot."""
    if price_model == SupplierItem.PriceModel.FIXED and fixed_price_isk is not None:
        return _q(fixed_price_isk)
    base = price_for(type_id)
    return _q(base * (Decimal(1) + (premium_pct or Decimal(0))))


def _round_up_to_moq(quantity: int, moq: int) -> int:
    moq = max(1, int(moq or 1))
    return int(math.ceil(quantity / moq) * moq)


# --- agreement dual-control ---------------------------------------------------

def estimate_cycle_value(agreement: SupplyAgreement) -> Decimal:
    """Sum of every line's per-cycle quantity × its derived unit price — the
    threshold basis, frozen at submit."""
    total = Decimal(0)
    for line in agreement.lines.all():
        unit = _unit_price(line.price_model, line.fixed_price_isk, line.premium_pct, line.type_id)
        total += unit * line.quantity_per_cycle
    return _q(total)


@transaction.atomic
def submit_agreement(agreement: SupplyAgreement, actor) -> SupplyAgreement:
    """Freeze the estimated cycle value and either activate directly (below the
    Director threshold) or open a dual-control approval (at/above it)."""
    ag = SupplyAgreement.objects.select_for_update().get(pk=agreement.pk)
    if ag.status != SupplyAgreement.Status.DRAFT:
        raise ProcurementError(_("Only a draft agreement can be submitted."))

    value = estimate_cycle_value(ag)
    ag.estimated_cycle_value_isk = value
    cfg = ProcurementConfig.active()
    actor_id = str(getattr(actor, "id", "") or "")

    if value < cfg.agreement_approval_threshold_isk:
        ag.status = SupplyAgreement.Status.ACTIVE
        ag.save(update_fields=["status", "estimated_cycle_value_isk", "updated_at"])
        audit_log(actor, "procurement.agreement_activated", target_type="supply_agreement",
                  target_id=str(ag.pk), metadata={"value_isk": str(value), "auto": True})
        return ag

    ag.status = SupplyAgreement.Status.PENDING_APPROVAL
    ag.save(update_fields=["status", "estimated_cycle_value_isk", "updated_at"])
    try:
        AgreementApproval.objects.create(
            agreement=ag, requested_by=actor if actor_id else None,
            estimated_value_isk=value, status=AgreementApproval.Status.PENDING,
        )
    except IntegrityError:
        # The partial unique collapsed a double-submit — one open approval already exists.
        raise ProcurementError(_("An approval request for this agreement is already open.")) from None
    audit_log(actor, "procurement.agreement_submitted", target_type="supply_agreement",
              target_id=str(ag.pk), metadata={"value_isk": str(value)})
    _notify_agreement_pending(ag, actor)
    return ag


@transaction.atomic
def decide_agreement(approval_pk: int, actor, approve: bool) -> tuple[bool, str]:
    """A second Director approves or rejects a pending agreement approval.

    Separation of duties: the requester may NOT decide their own request — and
    unlike the role console, a superuser is not exempt (an ISK commitment follows
    the stricter buyback posture). The row is locked + re-checked PENDING inside
    the txn so two Directors deciding at once cannot double-apply."""
    try:
        approval = (
            AgreementApproval.objects.select_for_update()
            .select_related("agreement")
            .get(pk=approval_pk, status=AgreementApproval.Status.PENDING)
        )
    except AgreementApproval.DoesNotExist:
        return False, _("This approval is no longer pending.")

    actor_id = getattr(actor, "id", None)
    if approve and approval.requested_by_id and approval.requested_by_id == actor_id:
        return False, _("You can't approve your own agreement — another Director must.")

    ag = approval.agreement
    approval.decided_by = actor if actor_id else None
    approval.decided_at = timezone.now()
    if approve:
        approval.status = AgreementApproval.Status.APPROVED
        ag.status = SupplyAgreement.Status.ACTIVE
        outcome = "approved"
    else:
        approval.status = AgreementApproval.Status.REJECTED
        ag.status = SupplyAgreement.Status.REJECTED
        outcome = "rejected"
    approval.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    ag.save(update_fields=["status", "updated_at"])
    audit_log(actor, f"procurement.agreement_{outcome}", target_type="supply_agreement",
              target_id=str(ag.pk), metadata={"approval_id": approval.pk})
    return True, _("Agreement %(outcome)s.") % {"outcome": outcome}


def _agreement_covers_po(po: PurchaseOrder) -> bool:
    """Does an ACTIVE agreement pre-authorise this PO? Every line's type must be
    on the agreement, within its per-line max and the cycle's remaining volume,
    and now must fall inside the term. Any miss demotes the PO to standalone (the
    Director gate then applies) — so one officer can't route a whale PO around
    dual control by attaching it to a once-approved agreement.

    Price is not re-checked here: ``approve_po`` freezes covered lines FROM the
    agreement trio, so the committed ISK is the agreement's by construction."""
    ag = po.agreement
    if ag is None or ag.status != SupplyAgreement.Status.ACTIVE:
        return False
    today = timezone.now().date()
    if ag.term_start and today < ag.term_start:
        return False
    if ag.term_end and today > ag.term_end:
        return False

    ag_lines = {line.type_id: line for line in ag.lines.all()}
    # Volume already committed to this agreement, per type, excluding this PO and
    # terminal (cancelled/rejected) orders. The whole active term is treated as one
    # cycle (a conservative bound — a tighter weekly/monthly window would only
    # demote MORE POs to the Director gate, never fewer).
    committed: dict[int, int] = {}
    other_lines = (
        PurchaseOrderLine.objects
        .filter(po__agreement=ag)
        .exclude(po_id=po.pk)
        .exclude(po__status__in=(S.CANCELLED, S.DISPUTED))
        .values_list("type_id", "quantity_ordered")
    )
    for type_id, qty in other_lines:
        committed[type_id] = committed.get(type_id, 0) + qty

    for line in po.lines.all():
        agl = ag_lines.get(line.type_id)
        if agl is None:
            return False
        if agl.min_qty is not None and line.quantity_ordered < agl.min_qty:
            return False
        if agl.max_qty is not None and line.quantity_ordered > agl.max_qty:
            return False
        used = committed.get(line.type_id, 0)
        if used + line.quantity_ordered > agl.quantity_per_cycle:
            return False
    return True


def _notify_agreement_pending(agreement: SupplyAgreement, actor) -> None:
    """Best-effort ping to Directors that an approval is waiting. The localized
    pingboard scaffold is registered in WS8; this never breaks the submit flow."""
    try:
        from apps.pingboard import services as pingboard
        from apps.pingboard.models import AlertCategory

        actor_name = getattr(actor, "first_name", "") or getattr(actor, "username", "") or "An officer"
        supplier_name = agreement.supplier.display_name or f"Supplier #{agreement.supplier_id}"
        pingboard.emit_broadcast(
            category=AlertCategory.INDUSTRY_JOB,
            title="Supply agreement awaiting approval",
            body=(f"{actor_name} submitted a supply agreement with {supplier_name} that needs "
                  "a second Director's approval."),
            template="procurement.agreement_pending",
            context={"actor_name": actor_name, "supplier_name": supplier_name},
            source_service="procurement",
            source_object_id=f"agreement_pending:{agreement.pk}",
            idempotency_key=f"procurement:agreement_pending:{agreement.pk}",
        )
    except Exception:  # noqa: BLE001 — a notification must never break the flow
        import logging

        logging.getLogger("forca.procurement").exception(
            "agreement-pending notification failed (agreement %s)", agreement.pk
        )


# --- purchase-order lifecycle -------------------------------------------------

@transaction.atomic
def submit_po(po: PurchaseOrder, actor) -> PurchaseOrder:
    """DRAFT → SUBMITTED. Validate every line against the supplier catalogue and
    round each quantity up to its MOQ (disclosed on the form, never silent)."""
    locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if locked.status != S.DRAFT:
        raise ProcurementError(_("Only a draft purchase order can be submitted."))

    lines = list(locked.lines.all())
    if not lines:
        raise ProcurementError(_("A purchase order needs at least one line."))
    catalogue = {
        it.type_id: it for it in SupplierItem.objects.filter(
            supplier_id=locked.supplier_id, active=True,
            type_id__in=[line.type_id for line in lines],
        )
    }
    missing = [line.type_id for line in lines if line.type_id not in catalogue]
    if missing:
        raise ProcurementError(_("The supplier does not sell every item on this order."))

    for line in lines:
        rounded = _round_up_to_moq(line.quantity_ordered, catalogue[line.type_id].moq)
        if rounded != line.quantity_ordered:
            line.quantity_ordered = rounded
            line.save(update_fields=["quantity_ordered"])

    locked.status = S.SUBMITTED
    locked.save(update_fields=["status", "updated_at"])
    audit_log(actor, "procurement.po_submitted", target_type="purchase_order",
              target_id=str(locked.pk), metadata={"lines": len(lines)})
    return locked


@transaction.atomic
def approve_po(po: PurchaseOrder, actor, *, promised_by=None) -> PurchaseOrder:
    """SUBMITTED → APPROVED. Freeze per-line prices and the expected total, set
    the promised date, and gate the authority: any officer may approve, EXCEPT a
    standalone PO (not covered by an active agreement) at or above the Director
    threshold, which a Director must approve. The approver may never be the
    creator — superuser included."""
    locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if locked.status != S.SUBMITTED:
        raise ProcurementError(_("Only a submitted purchase order can be approved."))

    actor_id = getattr(actor, "id", None)
    if locked.created_by_id and actor_id and locked.created_by_id == actor_id:
        raise ProcurementError(_("You can't approve your own purchase order — another officer must."))

    covered = _agreement_covers_po(locked)
    ag_lines = {}
    if covered and locked.agreement_id:
        ag_lines = {line.type_id: line for line in locked.agreement.lines.all()}
    catalogue = {
        it.type_id: it for it in SupplierItem.objects.filter(
            supplier_id=locked.supplier_id,
            type_id__in=[line.type_id for line in locked.lines.all()],
        )
    }

    total = Decimal(0)
    max_lead = locked.supplier.lead_time_days
    for line in locked.lines.all():
        agl = ag_lines.get(line.type_id)
        item = catalogue.get(line.type_id)
        if agl is not None:
            unit = _unit_price(agl.price_model, agl.fixed_price_isk, agl.premium_pct, line.type_id)
        elif item is not None:
            unit = _unit_price(item.price_model, item.fixed_price_isk, item.premium_pct, line.type_id)
        else:
            unit = _unit_price(SupplierItem.PriceModel.JITA_INDEXED, None, Decimal(0), line.type_id)
        jita = price_for(line.type_id)
        line.unit_price_isk = unit
        line.unit_jita_at_order = _q(jita)
        line.save(update_fields=["unit_price_isk", "unit_jita_at_order"])
        total += unit * line.quantity_ordered
        if item is not None and item.lead_time_days is not None:
            max_lead = max(max_lead, item.lead_time_days)
    total = _q(total)

    cfg = ProcurementConfig.active()
    if (not covered and total >= cfg.po_director_threshold_isk
            and not rbac.has_role(actor, rbac.ROLE_DIRECTOR)):
        raise ProcurementError(_(
            "A standalone purchase order at or above the Director threshold needs "
            "Director approval."))

    now = timezone.now()
    locked.expected_total_isk = total
    locked.approved_by = actor if actor_id else None
    locked.approved_at = now
    locked.status = S.APPROVED
    if promised_by is not None:
        locked.promised_by = promised_by
    elif locked.promised_by is None:
        locked.promised_by = now + timedelta(days=max_lead)
    locked.save(update_fields=[
        "expected_total_isk", "approved_by", "approved_at", "status", "promised_by", "updated_at",
    ])
    audit_log(actor, "procurement.po_approved", target_type="purchase_order",
              target_id=str(locked.pk),
              metadata={"total_isk": str(total), "covered": covered})
    return locked


@transaction.atomic
def mark_contract_expected(po: PurchaseOrder, actor) -> PurchaseOrder:
    """APPROVED → CONTRACT_EXPECTED. An honest human step: the order has been
    communicated to the supplier in game (the app cannot message them)."""
    locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if locked.status != S.APPROVED:
        raise ProcurementError(_("Only an approved purchase order can be marked contract-expected."))
    locked.status = S.CONTRACT_EXPECTED
    locked.save(update_fields=["status", "updated_at"])
    audit_log(actor, "procurement.po_contract_expected", target_type="purchase_order",
              target_id=str(locked.pk))
    return locked


@transaction.atomic
def cancel_po(po: PurchaseOrder, actor) -> PurchaseOrder:
    """Cancel a PO. Officers may cancel up to CONTRACT_EXPECTED; from later states
    (goods or ISK may have moved) a Director must. Vehicle FKs release through the
    MRP/shipyard reconcile on the next run, never by touching those tables here."""
    locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if locked.status in TERMINAL_STATUSES:
        raise ProcurementError(_("This purchase order is already closed."))
    late = locked.status not in (S.DRAFT, S.SUBMITTED, S.APPROVED, S.CONTRACT_EXPECTED)
    if late and not rbac.has_role(actor, rbac.ROLE_DIRECTOR):
        raise ProcurementError(_("Only a Director can cancel a purchase order this far along."))
    locked.status = S.CANCELLED
    locked.save(update_fields=["status", "updated_at"])
    audit_log(actor, "procurement.po_cancelled", target_type="purchase_order",
              target_id=str(locked.pk))
    return locked


@transaction.atomic
def dispute_po(po: PurchaseOrder, actor, reason: str) -> PurchaseOrder:
    """Flag a post-approval PO as DISPUTED with a mandatory verbatim reason."""
    reason = (reason or "").strip()
    if not reason:
        raise ProcurementError(_("A dispute needs a reason."))
    locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if locked.status in (S.DRAFT, S.SUBMITTED) or locked.status in TERMINAL_STATUSES:
        raise ProcurementError(_("Only an in-flight purchase order can be disputed."))
    locked.status = S.DISPUTED
    locked.notes = (locked.notes + "\n" if locked.notes else "") + reason
    locked.save(update_fields=["status", "notes", "updated_at"])
    audit_log(actor, "procurement.po_disputed", target_type="purchase_order",
              target_id=str(locked.pk), metadata={"reason": reason[:200]})
    return locked


@transaction.atomic
def resolve_dispute(po: PurchaseOrder, actor, *, outcome: str, note: str = "") -> PurchaseOrder:
    """Resolve a DISPUTED PO. ``reconciled`` closes it (officer, note required);
    ``cancelled`` voids it (Director); ``resume`` returns it to its evidence-derived
    state so the remainder can still be received."""
    locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if locked.status != S.DISPUTED:
        raise ProcurementError(_("This purchase order is not under dispute."))
    if outcome == "reconciled":
        if not (note or "").strip():
            raise ProcurementError(_("Closing a dispute as reconciled needs a note."))
        locked.status = S.RECONCILED
    elif outcome == "cancelled":
        if not rbac.has_role(actor, rbac.ROLE_DIRECTOR):
            raise ProcurementError(_("Only a Director can cancel a disputed purchase order."))
        locked.status = S.CANCELLED
    elif outcome == "resume":
        locked.status = _derive_progress_state(locked)
    else:
        raise ProcurementError(_("Unknown dispute outcome."))
    locked.save(update_fields=["status", "updated_at"])
    audit_log(actor, "procurement.dispute_resolved", target_type="purchase_order",
              target_id=str(locked.pk), metadata={"outcome": outcome, "note": (note or "")[:200]})
    return locked


@transaction.atomic
def mark_reconciled(po: PurchaseOrder, actor, note: str) -> PurchaseOrder:
    """DELIVERED → RECONCILED — the manual closing step for prepaid/out-of-band
    payments, with a mandatory verbatim note. With the reconcile beat disarmed
    this is the only way a v1 PO leaves DELIVERED."""
    if not (note or "").strip():
        raise ProcurementError(_("Reconciling a purchase order needs a note."))
    locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if locked.status != S.DELIVERED:
        raise ProcurementError(_("Only a delivered purchase order can be reconciled."))
    locked.status = S.RECONCILED
    locked.save(update_fields=["status", "updated_at"])
    audit_log(actor, "procurement.po_reconciled", target_type="purchase_order",
              target_id=str(locked.pk), metadata={"note": note[:200]})
    return locked


# --- evidence-derived state (used by the matcher, receipts and reconcile) -----

def _derive_progress_state(po: PurchaseOrder) -> str:
    """The correct ladder state from the PO's own evidence — contract match,
    receipt completion and payment. Used to leave OVERDUE/DISPUTED deterministically
    and to advance on a receipt. Never returns OVERDUE/DISPUTED/CANCELLED (those
    are set explicitly)."""
    lines = list(po.lines.all())
    total_ordered = sum(line.quantity_ordered for line in lines)
    total_received = sum(line.quantity_received for line in lines)
    fully_received = total_ordered > 0 and total_received >= total_ordered

    if fully_received and po.paid_entry_id:
        return S.RECONCILED
    if fully_received:
        return S.DELIVERED
    if total_received > 0:
        return S.PARTIAL
    if po.contract_id:
        finished = (po.contract_status or "").lower() in ("finished", "finished_issuer", "accepted")
        return S.ACCEPTED if finished else S.CONTRACT_AVAILABLE
    if po.status == S.CONTRACT_EXPECTED:
        return S.CONTRACT_EXPECTED
    return S.APPROVED


# --- fan-out factory (used by the MRP + shipyard creators) --------------------

@transaction.atomic
def create_draft_po(*, supplier, location, actor, lines, note_key="", note_params=None):
    """Mint a DRAFT purchase order with MOQ-rounded lines and a catalogue-derived
    promised date. ``lines`` is a list of dicts: ``{type_id, quantity, doctrine_fit?}``.
    The MRP and shipyard creators call this; the officer then submits/approves it."""
    from apps.erp.messages import english_text

    note_params = note_params or {}
    max_lead = supplier.lead_time_days
    items = {
        it.type_id: it for it in SupplierItem.objects.filter(
            supplier=supplier, active=True,
            type_id__in=[line["type_id"] for line in lines],
        )
    }
    po = PurchaseOrder.objects.create(
        supplier=supplier, location=location, created_by=actor,
        note_key=note_key, note_params=note_params,
        system_note=english_text(note_key, note_params) if note_key else "",
    )
    for line in lines:
        item = items.get(line["type_id"])
        moq = item.moq if item else 1
        qty = _round_up_to_moq(max(1, int(line["quantity"])), moq)
        PurchaseOrderLine.objects.create(
            po=po, type_id=line["type_id"], quantity_ordered=qty,
            doctrine_fit=line.get("doctrine_fit"),
        )
        if item is not None and item.lead_time_days is not None:
            max_lead = max(max_lead, item.lead_time_days)
    po.promised_by = timezone.now() + timedelta(days=max_lead)
    po.save(update_fields=["promised_by"])
    audit_log(actor, "procurement.po_drafted", target_type="purchase_order",
              target_id=str(po.pk), metadata={"lines": len(lines)})
    return po


@transaction.atomic
def reconcile_mrp_po(po_id: int, target: int) -> str:
    """MRP reconcile for a NetRequirement-linked PO. DRAFT: refresh the single line
    to the MOQ-rounded target (audited) — returns "refreshed"/"ok". Committed
    (SUBMITTED+): a drift is flagged, never rewritten — returns "diverged"/"ok".
    Terminal (cancelled/reconciled): returns "released" so the FK can be nulled."""
    po = PurchaseOrder.objects.select_for_update().get(pk=po_id)
    if po.status in (S.CANCELLED, S.RECONCILED):
        return "released"
    line = po.lines.first()
    if line is None:
        return "ok"
    item = SupplierItem.objects.filter(supplier_id=po.supplier_id, type_id=line.type_id).first()
    moq = item.moq if item else 1
    rounded = _round_up_to_moq(max(1, int(target)), moq)
    if po.status == S.DRAFT:
        if target > 0 and rounded != line.quantity_ordered:
            line.quantity_ordered = rounded
            line.save(update_fields=["quantity_ordered"])
            audit_log(None, "procurement.mrp_po_refresh", target_type="purchase_order",
                      target_id=str(po.pk), metadata={"target": int(target), "quantity": rounded})
            return "refreshed"
        return "ok"
    # Committed PO: compare the MOQ-rounded target; drift → diverged flag only.
    return "diverged" if rounded != line.quantity_ordered else "ok"


def apply_derived_state(po: PurchaseOrder, *, actor=None) -> str:
    """Set the PO to its evidence-derived state, preserving ``overdue_since`` and
    never downgrading along the progress ladder. Returns the resulting status.
    Called by the evidence engines after they record contract/receipt/payment
    evidence (they hold the PO row lock)."""
    if po.status == S.DISPUTED:
        return po.status  # frozen until an officer resolves it
    derived = _derive_progress_state(po)
    current_rank = _PROGRESS_RANK.get(po.status, -1)
    # OVERDUE is off-ladder (rank -1) so any real evidence state supersedes it.
    if _PROGRESS_RANK.get(derived, -1) < current_rank:
        return po.status
    if derived != po.status:
        previous = po.status
        po.status = derived
        po.save(update_fields=["status", "updated_at"])
        audit_log(actor, "procurement.po_progressed", target_type="purchase_order",
                  target_id=str(po.pk), metadata={"from": previous, "to": derived})
    return po.status
