"""Officer/Director procurement surfaces (P4, WS7).

Thin, audited web seams over the already-tested lifecycle engines: suppliers and
their catalogues, standing agreements (with the Director dual-control decision),
purchase orders (the full state machine, contract match, receipts, payment
confirmation and the hub-pickup haul button), and a Director-only board.

House idiom (mirrors ``apps.store.views_inventory``): every page is
``@login_required`` + ``@role_required``; direct ORM writes are audited here, while
lifecycle transitions are audited inside the services they call (never twice).
Domain rule violations raise ``ProcurementError`` and surface as an error message.
"""
from __future__ import annotations

import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from . import contracts, metrics, payments, receipts
from .forms import (
    PurchaseOrderForm,
    PurchaseOrderLineForm,
    SupplierForm,
    SupplierItemForm,
    SupplyAgreementForm,
    SupplyAgreementLineForm,
)
from .models import (
    AgreementApproval,
    PurchaseOrder,
    Supplier,
    SupplyAgreement,
)
from .services import (
    RECEIVABLE_STATUSES,
    TERMINAL_STATUSES,
    ProcurementError,
    approve_po,
    cancel_po,
    decide_agreement,
    dispute_po,
    estimate_cycle_value,
    mark_contract_expected,
    mark_reconciled,
    resolve_dispute,
    submit_agreement,
    submit_po,
)

_S = PurchaseOrder.Status
# A payment may be confirmed against a PO that has (partly) landed but is unpaid.
_SETTLEABLE = (_S.DELIVERED, _S.PARTIAL, _S.ACCEPTED, _S.OVERDUE)
# A contract candidate is only offered while an approved order is still unmatched.
_MATCHABLE = (_S.APPROVED, _S.CONTRACT_EXPECTED, _S.OVERDUE)


# --- helpers ------------------------------------------------------------------

def _paginate(request: HttpRequest, items, per_page: int = 50):
    page_obj = Paginator(items, per_page).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return page_obj, params.urlencode()


def _surface_errors(request: HttpRequest, form) -> None:
    """Bubble a form's validation errors up as flash messages (the officer stays on
    the redirect target rather than losing their place)."""
    for errors in form.errors.values():
        for err in errors:
            messages.error(request, err)


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --- suppliers ----------------------------------------------------------------

@login_required
@role_required(rbac.ROLE_OFFICER)
def suppliers(request: HttpRequest) -> HttpResponse:
    """The supplier register — filter by name, kind and status."""
    q = (request.GET.get("q") or "").strip()
    f_kind = (request.GET.get("kind") or "").strip()
    f_status = (request.GET.get("status") or "").strip()

    qs = Supplier.objects.all().order_by("display_name", "pk")
    if q:
        qs = qs.filter(display_name__icontains=q)
    if f_kind:
        qs = qs.filter(kind=f_kind)
    if f_status:
        qs = qs.filter(status=f_status)

    page_obj, base_qs = _paginate(request, qs)
    return render(request, "procurement/suppliers.html", {
        "rows": page_obj.object_list, "page_obj": page_obj, "base_qs": base_qs,
        "total": qs.count(), "q": q, "f_kind": f_kind, "f_status": f_status,
        "kinds": Supplier.Kind.choices, "statuses": Supplier.Status.choices,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def supplier_new(request: HttpRequest) -> HttpResponse:
    """Register a new supplier (business data only)."""
    if request.method == "POST":
        form = SupplierForm(request.POST)
        if form.is_valid():
            supplier = form.save()
            audit_log(request.user, "procurement.supplier_created", target_type="supplier",
                      target_id=str(supplier.pk), ip=client_ip(request))
            messages.success(request, _("Supplier registered."))
            return redirect("procurement:supplier_detail", pk=supplier.pk)
    else:
        form = SupplierForm()
    return render(request, "procurement/supplier_form.html", {"form": form, "creating": True})


@login_required
@role_required(rbac.ROLE_OFFICER)
def supplier_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Supplier profile + catalogue + matched-PO contract history + reliability."""
    supplier = get_object_or_404(Supplier.objects.select_related("default_location"), pk=pk)
    if request.method == "POST":
        form = SupplierForm(request.POST, instance=supplier)
        if form.is_valid():
            form.save()
            audit_log(request.user, "procurement.supplier_updated", target_type="supplier",
                      target_id=str(supplier.pk), ip=client_ip(request))
            messages.success(request, _("Supplier updated."))
            return redirect("procurement:supplier_detail", pk=supplier.pk)
    else:
        form = SupplierForm(instance=supplier)

    contract_history = list(
        supplier.purchase_orders.filter(contract_id__isnull=False)
        .order_by("-contract_matched_at")[:50]
    )
    return render(request, "procurement/supplier_detail.html", {
        "supplier": supplier, "form": form,
        "items": list(supplier.items.order_by("type_id")),
        "item_form": SupplierItemForm(),
        "contract_history": contract_history,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def supplier_item_add(request: HttpRequest, pk: int) -> HttpResponse:
    """Add one catalogue line to a supplier."""
    supplier = get_object_or_404(Supplier, pk=pk)
    form = SupplierItemForm(request.POST)
    if form.is_valid():
        item = form.save(commit=False)
        item.supplier = supplier
        try:
            item.save()
        except IntegrityError:
            messages.error(request, _("That item is already in this supplier's catalogue."))
            return redirect("procurement:supplier_detail", pk=supplier.pk)
        audit_log(request.user, "procurement.supplier_item_added", target_type="supplier",
                  target_id=str(supplier.pk), metadata={"type_id": item.type_id},
                  ip=client_ip(request))
        messages.success(request, _("Catalogue item added."))
    else:
        _surface_errors(request, form)
    return redirect("procurement:supplier_detail", pk=supplier.pk)


# --- agreements ---------------------------------------------------------------

@login_required
@role_required(rbac.ROLE_OFFICER)
def agreements(request: HttpRequest) -> HttpResponse:
    """Standing supply agreements — filter by status."""
    f_status = (request.GET.get("status") or "").strip()
    qs = SupplyAgreement.objects.select_related("supplier").order_by("-created_at")
    if f_status:
        qs = qs.filter(status=f_status)
    page_obj, base_qs = _paginate(request, qs)
    return render(request, "procurement/agreements.html", {
        "rows": page_obj.object_list, "page_obj": page_obj, "base_qs": base_qs,
        "total": qs.count(), "f_status": f_status, "statuses": SupplyAgreement.Status.choices,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def agreement_new(request: HttpRequest) -> HttpResponse:
    """Draft a new agreement; its lines are added on the detail page."""
    if request.method == "POST":
        form = SupplyAgreementForm(request.POST)
        if form.is_valid():
            agreement = form.save(commit=False)
            agreement.created_by = request.user
            agreement.save()
            audit_log(request.user, "procurement.agreement_created", target_type="supply_agreement",
                      target_id=str(agreement.pk), ip=client_ip(request))
            messages.success(request, _("Draft agreement created — add its lines next."))
            return redirect("procurement:agreement_detail", pk=agreement.pk)
    else:
        form = SupplyAgreementForm()
    return render(request, "procurement/agreement_form.html", {"form": form})


@login_required
@role_required(rbac.ROLE_OFFICER)
def agreement_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Agreement detail with its lines, dual-control state and lifecycle actions."""
    agreement = get_object_or_404(
        SupplyAgreement.objects.select_related("supplier", "location"), pk=pk
    )
    approval = agreement.approvals.select_related("requested_by", "decided_by").order_by("-created_at").first()
    return render(request, "procurement/agreement_detail.html", {
        "agreement": agreement,
        "lines": list(agreement.lines.order_by("type_id")),
        "line_form": SupplyAgreementLineForm(),
        "approval": approval,
        "estimate": estimate_cycle_value(agreement),
        "is_draft": agreement.status == SupplyAgreement.Status.DRAFT,
        "is_pending": agreement.status == SupplyAgreement.Status.PENDING_APPROVAL,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def agreement_line_add(request: HttpRequest, pk: int) -> HttpResponse:
    """Add one type line to a draft agreement."""
    agreement = get_object_or_404(SupplyAgreement, pk=pk)
    if agreement.status != SupplyAgreement.Status.DRAFT:
        messages.error(request, _("Lines can only be changed while the agreement is a draft."))
        return redirect("procurement:agreement_detail", pk=agreement.pk)
    form = SupplyAgreementLineForm(request.POST)
    if form.is_valid():
        line = form.save(commit=False)
        line.agreement = agreement
        try:
            line.save()
        except IntegrityError:
            messages.error(request, _("That type is already on this agreement."))
            return redirect("procurement:agreement_detail", pk=agreement.pk)
        audit_log(request.user, "procurement.agreement_line_added", target_type="supply_agreement",
                  target_id=str(agreement.pk), metadata={"type_id": line.type_id},
                  ip=client_ip(request))
        messages.success(request, _("Agreement line added."))
    else:
        _surface_errors(request, form)
    return redirect("procurement:agreement_detail", pk=agreement.pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def agreement_action(request: HttpRequest, pk: int) -> HttpResponse:
    """Agreement lifecycle: submit (officer) and approve/reject (Director-only)."""
    agreement = get_object_or_404(SupplyAgreement, pk=pk)
    action = request.POST.get("action", "")
    try:
        if action == "submit":
            result = submit_agreement(agreement, request.user)
            if result.status == SupplyAgreement.Status.ACTIVE:
                messages.success(request, _("Agreement submitted and activated."))
            else:
                messages.success(request, _("Agreement submitted for Director approval."))
        elif action in ("approve", "reject"):
            if not rbac.has_role(request.user, rbac.ROLE_DIRECTOR):
                raise PermissionDenied(_("Only a Director can decide an agreement approval."))
            pending = agreement.approvals.filter(
                status=AgreementApproval.Status.PENDING
            ).first()
            if pending is None:
                messages.error(request, _("There is no pending approval for this agreement."))
            else:
                ok, msg = decide_agreement(pending.pk, request.user, approve=(action == "approve"))
                (messages.success if ok else messages.error)(request, msg)
        else:
            messages.error(request, _("Unknown action."))
    except ProcurementError as exc:
        messages.error(request, str(exc))
    return redirect("procurement:agreement_detail", pk=agreement.pk)


# --- purchase orders ----------------------------------------------------------

@login_required
@role_required(rbac.ROLE_OFFICER)
def pos(request: HttpRequest) -> HttpResponse:
    """The purchase-order register — filter by status and supplier; CSV export."""
    f_status = (request.GET.get("status") or "").strip()
    f_supplier = (request.GET.get("supplier") or "").strip()
    qs = PurchaseOrder.objects.select_related("supplier", "location").order_by("-created_at")
    if f_status:
        qs = qs.filter(status=f_status)
    if f_supplier.isdigit():
        qs = qs.filter(supplier_id=int(f_supplier))

    if request.GET.get("format") == "csv":
        return _pos_csv(qs)

    page_obj, base_qs = _paginate(request, qs)
    return render(request, "procurement/pos.html", {
        "rows": page_obj.object_list, "page_obj": page_obj, "base_qs": base_qs,
        "total": qs.count(), "f_status": f_status, "f_supplier": f_supplier,
        "statuses": PurchaseOrder.Status.choices,
        "suppliers": Supplier.objects.order_by("display_name", "pk"),
    })


def _pos_csv(qs) -> HttpResponse:
    """Purchase orders as CSV. Column keys stay machine-stable English; ``status``
    and ``delivery_mode`` are the persisted machine codes, not translated labels."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="procurement-purchase-orders.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "po_id", "supplier", "supplier_id", "status", "delivery_mode", "location",
        "promised_by", "overdue_since", "expected_total_isk", "paid_amount_isk",
        "contract_id", "contract_status", "created_at",
    ])
    for po in qs:
        writer.writerow([
            po.pk, po.supplier.display_name or "", po.supplier_id, po.status,
            po.delivery_mode, str(po.location) if po.location else "",
            po.promised_by.isoformat() if po.promised_by else "",
            po.overdue_since.isoformat() if po.overdue_since else "",
            po.expected_total_isk, po.paid_amount_isk if po.paid_amount_isk is not None else "",
            po.contract_id or "", po.contract_status or "", po.created_at.isoformat(),
        ])
    return response


@login_required
@role_required(rbac.ROLE_OFFICER)
def po_new(request: HttpRequest) -> HttpResponse:
    """Draft a new purchase order; its lines are added on the detail page."""
    if request.method == "POST":
        form = PurchaseOrderForm(request.POST)
        if form.is_valid():
            po = form.save(commit=False)
            po.created_by = request.user
            po.save()
            audit_log(request.user, "procurement.po_created", target_type="purchase_order",
                      target_id=str(po.pk), ip=client_ip(request))
            messages.success(request, _("Draft purchase order created — add its lines next."))
            return redirect("procurement:po_detail", pk=po.pk)
    else:
        form = PurchaseOrderForm()
    return render(request, "procurement/po_form.html", {"form": form})


@login_required
@role_required(rbac.ROLE_OFFICER)
def po_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Purchase-order detail: lines, the full lifecycle, contract match, receipts,
    payment confirmation and the hub-pickup haul button."""
    po = get_object_or_404(
        PurchaseOrder.objects.select_related("supplier", "location", "agreement"), pk=pk
    )
    candidates = []
    if po.contract_id is None and po.status in _MATCHABLE:
        candidates = contracts.candidate_contracts(po)
    suggestions = []
    if po.paid_entry_id is None and po.status in _SETTLEABLE:
        suggestions = payments.payment_suggestions(po)

    return render(request, "procurement/po_detail.html", {
        "po": po,
        "lines": list(po.lines.select_related("doctrine_fit").order_by("pk")),
        "line_form": PurchaseOrderLineForm(),
        "receipts": list(po.receipts.select_related("line", "actor").order_by("-created_at")[:50]),
        "candidates": candidates,
        "suggestions": suggestions,
        "is_draft": po.status == _S.DRAFT,
        "is_submitted": po.status == _S.SUBMITTED,
        "is_approved": po.status == _S.APPROVED,
        "is_disputed": po.status == _S.DISPUTED,
        "is_delivered": po.status == _S.DELIVERED,
        "receivable": po.status in RECEIVABLE_STATUSES,
        "is_hub_pickup": po.delivery_mode == PurchaseOrder.DeliveryMode.HUB_PICKUP,
        "is_terminal": po.status in TERMINAL_STATUSES,
        "can_dispute": (
            po.status not in (_S.DRAFT, _S.SUBMITTED, _S.DISPUTED)
            and po.status not in TERMINAL_STATUSES
        ),
        "delivery_modes": PurchaseOrder.DeliveryMode.choices,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def po_line_add(request: HttpRequest, pk: int) -> HttpResponse:
    """Add one type line to a draft purchase order."""
    po = get_object_or_404(PurchaseOrder, pk=pk)
    if po.status != _S.DRAFT:
        messages.error(request, _("Lines can only be changed while the order is a draft."))
        return redirect("procurement:po_detail", pk=po.pk)
    form = PurchaseOrderLineForm(request.POST)
    if form.is_valid():
        line = form.save(commit=False)
        line.po = po
        line.save()
        audit_log(request.user, "procurement.po_line_added", target_type="purchase_order",
                  target_id=str(po.pk), metadata={"type_id": line.type_id,
                                                  "quantity": line.quantity_ordered},
                  ip=client_ip(request))
        messages.success(request, _("Order line added."))
    else:
        _surface_errors(request, form)
    return redirect("procurement:po_detail", pk=po.pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def po_action(request: HttpRequest, pk: int) -> HttpResponse:
    """The purchase-order lifecycle dispatcher. Each branch calls the audited
    service; a domain-rule violation surfaces as an error message."""
    po = get_object_or_404(PurchaseOrder, pk=pk)
    action = request.POST.get("action", "")
    try:
        if action == "submit":
            submit_po(po, request.user)
            messages.success(request, _("Purchase order submitted."))
        elif action == "approve":
            promised = parse_datetime(request.POST.get("promised_by") or "")
            approve_po(po, request.user, promised_by=promised)
            messages.success(request, _("Purchase order approved."))
        elif action == "contract_expected":
            mark_contract_expected(po, request.user)
            messages.success(request, _("Marked contract-expected."))
        elif action == "cancel":
            cancel_po(po, request.user)
            messages.success(request, _("Purchase order cancelled."))
        elif action == "dispute":
            dispute_po(po, request.user, request.POST.get("reason", ""))
            messages.success(request, _("Purchase order marked disputed."))
        elif action == "resolve":
            resolve_dispute(po, request.user, outcome=request.POST.get("outcome", ""),
                            note=request.POST.get("note", ""))
            messages.success(request, _("Dispute resolved."))
        elif action == "reconcile":
            mark_reconciled(po, request.user, request.POST.get("note", ""))
            messages.success(request, _("Purchase order reconciled."))
        elif action == "confirm_match":
            contracts.confirm_match(po, _int(request.POST.get("contract_id")), request.user)
            messages.success(request, _("Contract matched to this order."))
        elif action == "confirm_payment":
            payments.confirm_payment(po, _int(request.POST.get("entry_id")), request.user)
            messages.success(request, _("Payment confirmed against this order."))
        elif action == "receipt":
            line = po.lines.filter(pk=_int(request.POST.get("line_id"))).first()
            if line is None:
                messages.error(request, _("Choose a line to receive against."))
            else:
                receipts.receive_po_delivery(
                    po, line, _int(request.POST.get("quantity")), actor=request.user,
                )
                messages.success(request, _("Delivery recorded."))
        elif action == "haul":
            tasks = receipts.create_haul_for_po(po, request.user)
            messages.success(request, _("%(n)s hauling task(s) created.") % {"n": len(tasks)})
        else:
            messages.error(request, _("Unknown action."))
    except ProcurementError as exc:
        messages.error(request, str(exc))
    return redirect("procurement:po_detail", pk=po.pk)


# --- board (Director-only) ----------------------------------------------------

@login_required
@role_required(rbac.ROLE_DIRECTOR)
def board(request: HttpRequest) -> HttpResponse:
    """The Director procurement board: open obligations, due/late, agreement
    utilisation, supplier reliability and evidence-feed freshness chips."""
    return render(request, "procurement/board.html", {
        "obligations": metrics.open_obligations(),
        "due": metrics.due_and_late(),
        "utilisation": metrics.agreement_utilisation(),
        "reliability": metrics.reliability_table(),
        "freshness": metrics.board_freshness(),
    })
