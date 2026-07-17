"""P4 contract matching: soft-link a PO to the delete-all-rebuilt CorpContract
snapshot by bare ``contract_id``, copy the evidence onto the PO at match time, and
fetch the matched contract's items exactly once (bounded, cached forever).

This is a sibling step chained after ``logistics.sync_corp_contracts`` — it reads
the snapshot the sync just wrote and makes NO second corp-contracts pull; the only
ESI call is the bounded per-contract items fetch. A conservative matcher never
guesses: exactly one candidate auto-matches, zero or several are offered on the PO
page for one-click officer confirmation.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from core.audit import audit_log

from .models import ProcurementConfig, PurchaseOrder
from .receipts import auto_receive_from_contract
from .services import ProcurementError, S, apply_derived_state

_PRICE_TOLERANCE = Decimal("0.02")  # ±2% (the courier verifier's discipline)
_ITEMS_FETCH_BUDGET = 10            # bound the per-run items fetches (and matches)
# OVERDUE is included deliberately: a late PO must keep generating evidence so it
# can leave OVERDUE via apply_derived_state.
_MATCHABLE = (S.APPROVED, S.CONTRACT_EXPECTED, S.OVERDUE)
_FINISHED = ("finished", "finished_issuer", "finished_contractor", "accepted")


def candidate_contracts(po: PurchaseOrder) -> list:
    """CorpContract rows that could be this PO's in-game contract: an item exchange
    from the supplier, issued on/after approval, priced within ±2% of the expected
    total, and not already claimed by another PO."""
    from apps.logistics.models import CorpContract

    supplier = po.supplier
    if supplier.entity_id is None or po.contract_id:
        return []
    qs = CorpContract.objects.filter(type="item_exchange")
    if supplier.kind == supplier.Kind.CORP:
        qs = qs.filter(issuer_corporation_id=supplier.entity_id)
    else:
        qs = qs.filter(issuer_id=supplier.entity_id)
    floor = po.approved_at or po.created_at
    if floor:
        qs = qs.filter(date_issued__gte=floor)

    claimed = set(
        PurchaseOrder.objects.exclude(pk=po.pk)
        .filter(contract_id__isnull=False).values_list("contract_id", flat=True)
    )
    expected = po.expected_total_isk or Decimal(0)
    out = []
    for c in qs:
        if c.contract_id in claimed:
            continue
        if expected > 0 and abs((c.price or Decimal(0)) - expected) > _PRICE_TOLERANCE * expected:
            continue
        out.append(c)
    return out


def fetch_contract_items(contract_id: int, *, corp_id=None, client=None):
    """The bounded, once-per-contract items fetch via the director contracts token.
    Returns a list of ``{"type_id", "quantity"}`` or ``None`` if no token / error."""
    from apps.logistics.contracts_esi import _director_contract_token
    from core.esi.client import ESIClient, ESIError

    corp_id = corp_id or getattr(settings, "FORCA_HOME_CORP_ID", 0)
    token = _director_contract_token(corp_id)
    if token is None:
        return None
    client = client or ESIClient()
    try:
        rows = client.get(
            f"/corporations/{corp_id}/contracts/{contract_id}/items/", token=token,
        ).data or []
    except ESIError:
        return None
    out = []
    for r in rows:
        tid, qty = r.get("type_id"), r.get("quantity")
        if tid and qty:
            out.append({"type_id": int(tid), "quantity": int(qty)})
    return out


@transaction.atomic
def apply_match(po: PurchaseOrder, contract, *, actor=None, corp_id=None, client=None) -> PurchaseOrder:
    """Copy the contract's evidence onto the PO, fetch its items once, and advance
    to CONTRACT_AVAILABLE (outstanding) or ACCEPTED (already finished)."""
    locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if locked.contract_id:  # a racing match already claimed it
        return locked
    locked.contract_id = contract.contract_id
    locked.contract_status = contract.status or ""
    locked.contract_price = contract.price
    locked.contract_date_issued = contract.date_issued
    locked.contract_date_completed = contract.date_completed
    locked.contract_matched_at = timezone.now()
    items = fetch_contract_items(contract.contract_id, corp_id=corp_id, client=client)
    if items is not None:
        locked.contract_items = items
    locked.save(update_fields=[
        "contract_id", "contract_status", "contract_price", "contract_date_issued",
        "contract_date_completed", "contract_matched_at", "contract_items", "updated_at",
    ])
    apply_derived_state(locked, actor=None)
    audit_log(actor, "procurement.contract_matched", target_type="purchase_order",
              target_id=str(locked.pk), metadata={"contract_id": contract.contract_id})
    return locked


def confirm_match(po: PurchaseOrder, contract_id: int, actor) -> PurchaseOrder:
    """Officer one-click confirmation of a candidate contract (the zero/several path)."""
    from apps.logistics.models import CorpContract

    contract = CorpContract.objects.filter(contract_id=contract_id).first()
    if contract is None:
        raise ProcurementError(_("That contract is no longer in the snapshot."))
    if contract_id not in {c.contract_id for c in candidate_contracts(po)}:
        raise ProcurementError(_("That contract is not a valid match for this order."))
    return apply_match(po, contract, actor=actor)


def refresh_matched_contracts() -> int:
    """Refresh matched POs' contract_status/date_completed from the fresh snapshot
    by bare-id lookup. A contract absent from the snapshot (aged out) keeps its
    copied evidence — the point of copy-on-match."""
    from apps.logistics.models import CorpContract

    refreshed = 0
    live = PurchaseOrder.objects.filter(contract_id__isnull=False).exclude(
        status__in=(S.RECONCILED, S.CANCELLED)
    )
    for po in live:
        contract = CorpContract.objects.filter(contract_id=po.contract_id).first()
        if contract is None:
            continue
        changed = False
        if contract.status and contract.status != po.contract_status:
            po.contract_status = contract.status
            changed = True
        if contract.date_completed and contract.date_completed != po.contract_date_completed:
            po.contract_date_completed = contract.date_completed
            changed = True
        if changed:
            with transaction.atomic():
                locked = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
                locked.contract_status = po.contract_status
                locked.contract_date_completed = po.contract_date_completed
                locked.save(update_fields=["contract_status", "contract_date_completed", "updated_at"])
                apply_derived_state(locked, actor=None)
            refreshed += 1
    return refreshed


def match_contracts() -> dict:
    """The matcher run (chained after the corp-contracts sync). No-op unless armed."""
    cfg = ProcurementConfig.active()
    if not cfg.match_enabled:
        return {"status": "disabled"}

    from core.esi.client import ESIClient

    corp_id = getattr(settings, "FORCA_HOME_CORP_ID", 0)
    client = ESIClient()
    refreshed = refresh_matched_contracts()
    matched = suggested = 0
    for po in PurchaseOrder.objects.filter(status__in=_MATCHABLE, contract_id__isnull=True):
        candidates = candidate_contracts(po)
        if len(candidates) == 1:
            if matched >= _ITEMS_FETCH_BUDGET:
                continue  # bounded per run; the rest match next run
            apply_match(po, candidates[0], corp_id=corp_id, client=client)
            matched += 1
            po.refresh_from_db()
            if cfg.auto_receipt_enabled and (po.contract_status or "").lower() in _FINISHED:
                auto_receive_from_contract(po, actor=None)
        elif candidates:
            suggested += 1  # zero or several → offered on the page, never auto-taken
    return {"status": "ok", "matched": matched, "suggested": suggested, "refreshed": refreshed}
