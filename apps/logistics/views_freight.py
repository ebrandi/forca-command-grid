"""The Freight Pipeline (P6) — officer views over freight batches + in-transit stock.

Officer-only and audited. Consolidate purchase/import lines per lane, assign to the
courier flow (or a member haul), track ETD/ETA, and receipt landed stock. The page
never re-prices or re-verifies — it renders what :mod:`apps.logistics.freight`
computes and drives its audited transitions. Alpine ``x-data``/``@click`` only (the
CSP gate bans inline ``on*=`` handlers).
"""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import client_ip
from core.rbac import role_required

from . import freight
from .models import FreightBatch, FreightBatchLine, FreightConfig, ShipClass

_INPUT = {"class": "input-field"}

# Machine code → translated label (the DECISION_LABELS discipline; codes never travel).
COST_SOURCE_LABELS = {
    "typed": _("typed"),
    "snapshot": _("from price snapshot"),
    "": "",
}


def cost_source_label(code: str) -> str:
    return COST_SOURCE_LABELS.get(code, code)


class FreightConfigForm(forms.ModelForm):
    class Meta:
        model = FreightConfig
        fields = [
            "default_ship_class", "default_dispatch_days", "default_transit_days",
            "eta_sweep_enabled", "late_grace_hours",
        ]
        widgets = {
            "default_ship_class": forms.Select(attrs=_INPUT),
            "default_dispatch_days": forms.NumberInput(attrs={**_INPUT, "min": "0"}),
            "default_transit_days": forms.NumberInput(attrs={**_INPUT, "min": "0"}),
            "late_grace_hours": forms.NumberInput(attrs={**_INPUT, "min": "0"}),
        }


def _lane_queryset():
    from apps.market.models import MarketLocation

    return MarketLocation.objects.filter(active=True).order_by("name")


class OpenLaneForm(forms.Form):
    origin = forms.ModelChoiceField(queryset=None, widget=forms.Select(attrs=_INPUT))
    destination = forms.ModelChoiceField(queryset=None, widget=forms.Select(attrs=_INPUT))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = _lane_queryset()
        self.fields["origin"].queryset = qs
        self.fields["destination"].queryset = qs

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("origin") and cleaned.get("origin") == cleaned.get("destination"):
            raise forms.ValidationError(_("Origin and destination must differ."))
        return cleaned


class AssignForm(forms.Form):
    ship_class = forms.ChoiceField(
        choices=ShipClass.choices, widget=forms.Select(attrs=_INPUT)
    )
    rush = forms.BooleanField(required=False)


# --------------------------------------------------------------------------- #
#  Read views
# --------------------------------------------------------------------------- #
def _type_names(type_ids) -> dict[int, str]:
    from apps.sde.models import SdeType

    return dict(
        SdeType.objects.filter(type_id__in=list(type_ids)).values_list("type_id", "name")
    )


@login_required
@role_required(rbac.ROLE_OFFICER)
def freight_board(request: HttpRequest) -> HttpResponse:
    """Freight Pipeline: batches by status + the derived in-transit bucket. GET only."""
    if request.GET.get("export") == "csv":
        return _export_csv()

    batches = list(
        FreightBatch.objects.select_related("origin", "destination", "courier_contract")
        .prefetch_related("lines").order_by("-created_at")[:200]
    )
    now = timezone.now()
    cfg = FreightConfig.active()
    batch_rows = []
    for b in batches:
        lines = list(b.lines.all())
        fit = freight.capacity_fit(b) if lines else None
        late = bool(
            b.eta_planned and not b.is_terminal
            and b.status == FreightBatch.Status.IN_TRANSIT and b.eta_planned < now
        )
        batch_rows.append({
            "batch": b, "fit": fit, "line_count": len(lines),
            "received": sum(int(ln.quantity_received) for ln in lines),
            "planned_units": sum(int(ln.quantity) for ln in lines),
            "late": late,
        })

    tab = "intransit" if request.GET.get("tab") == "intransit" else "batches"
    context = {
        "tab": tab, "batch_rows": batch_rows, "config": cfg,
        "config_form": FreightConfigForm(instance=cfg),
        "open_form": OpenLaneForm(),
    }
    if tab == "intransit":
        context["intransit"] = _intransit_context()
    return render(request, "logistics/freight.html", context)


def _intransit_context() -> list[dict]:
    """The derived (type, destination) in-transit bucket with covering-requirement links."""
    from apps.industry.models import NetRequirement
    from apps.market.models import MarketLocation

    lots = freight.in_transit()
    type_names = _type_names({lot.type_id for lot in lots})
    dest_names = dict(
        MarketLocation.objects.filter(pk__in={lot.destination_id for lot in lots})
        .values_list("pk", "name")
    )
    req_by_line = dict(
        NetRequirement.objects.filter(
            freight_line_id__in={lot.line_id for lot in lots},
            status__in=(NetRequirement.Status.OPEN, NetRequirement.Status.IN_PROGRESS),
        ).values_list("freight_line_id", "pk")
    )
    buckets: dict[tuple[int, int], dict] = {}
    for lot in lots:
        key = (lot.type_id, lot.destination_id)
        row = buckets.setdefault(key, {
            "type_id": lot.type_id, "type_name": type_names.get(lot.type_id, str(lot.type_id)),
            "destination": dest_names.get(lot.destination_id, str(lot.destination_id)),
            "in_transit": 0, "received_unsynced": 0, "lines": [],
        })
        row[lot.kind] = row.get(lot.kind, 0) + lot.remaining
        row["lines"].append({
            "line_id": lot.line_id, "remaining": lot.remaining, "eta": lot.eta,
            "kind": lot.kind, "requirement_id": req_by_line.get(lot.line_id),
        })
    return sorted(buckets.values(), key=lambda r: (r["destination"], r["type_name"]))


@login_required
@role_required(rbac.ROLE_OFFICER)
def freight_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """One batch: lines, capacity fit, action buttons, landed-vs-forecast."""
    batch = get_object_or_404(
        FreightBatch.objects.select_related("origin", "destination", "courier_contract"),
        pk=pk,
    )
    lines = list(batch.lines.prefetch_related("receipts").order_by("pk"))
    names = _type_names({ln.type_id for ln in lines})
    line_rows = []
    for ln in lines:
        landed = [r.unit_landed_cost for r in ln.receipts.all() if r.unit_landed_cost is not None]
        line_rows.append({
            "line": ln, "name": names.get(ln.type_id, str(ln.type_id)),
            "officer_qty": ln.officer_quantity, "remaining": ln.remaining,
            "cost_source_label": cost_source_label(ln.cost_source),
            "landed_unit": (sum(landed) / len(landed)).quantize(Decimal("0.01")) if landed else None,
        })
    return render(request, "logistics/freight_detail.html", {
        "batch": batch,
        "line_rows": line_rows,
        "fit": freight.capacity_fit(batch) if lines else None,
        "assign_form": AssignForm(initial={"ship_class": batch.ship_class}),
        "landed_rows": freight.landed_vs_forecast(batch),
        "ship_classes": ShipClass.choices,
        "S": FreightBatch.Status,
    })


def _export_csv() -> HttpResponse:
    """Every batch line as CSV (machine-stable English keys, the house convention)."""
    from apps.market.models import MarketLocation  # noqa: F401 (name access via FK)

    lines = list(
        FreightBatchLine.objects.select_related("batch", "batch__origin", "batch__destination")
        .order_by("batch_id", "pk")
    )
    names = _type_names({ln.type_id for ln in lines})
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="freight-pipeline.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "batch_id", "origin", "destination", "status", "ship_class",
        "type_id", "type_name", "quantity", "planned_quantity", "quantity_received",
        "unit_purchase_cost", "cost_source", "freight_share", "freight_cost",
        "etd_planned", "eta_planned",
    ])
    for ln in lines:
        b = ln.batch
        writer.writerow([
            b.pk, str(b.origin), str(b.destination), b.status, b.ship_class,
            ln.type_id, names.get(ln.type_id, ""), ln.quantity, ln.planned_quantity,
            ln.quantity_received,
            "" if ln.unit_purchase_cost is None else ln.unit_purchase_cost,
            ln.cost_source, ln.freight_share, b.freight_cost,
            b.etd_planned.isoformat() if b.etd_planned else "",
            b.eta_planned.isoformat() if b.eta_planned else "",
        ])
    return response


# --------------------------------------------------------------------------- #
#  POST handlers (officer, audited via the service)
# --------------------------------------------------------------------------- #
def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _decimal(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def freight_config(request: HttpRequest) -> HttpResponse:
    cfg = FreightConfig.active()
    form = FreightConfigForm(request.POST, instance=cfg)
    if form.is_valid():
        form.save()
        from core.audit import audit_log

        audit_log(request.user, "freight.config", target_type="freight_config",
                  target_id=str(cfg.pk), ip=client_ip(request))
        messages.success(request, _("Freight settings saved."))
    else:
        messages.error(request, _("Please correct the errors below."))
    return redirect("logistics:freight_board")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def freight_open(request: HttpRequest) -> HttpResponse:
    form = OpenLaneForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Pick two different locations."))
        return redirect("logistics:freight_board")
    batch = freight.open_batch_for_lane(
        form.cleaned_data["origin"], form.cleaned_data["destination"],
        actor=request.user, ip=client_ip(request),
    )
    return redirect("logistics:freight_detail", pk=batch.pk)


def _handle(request, fn, *args, success, **kwargs):
    """Run a freight service call, surfacing FreightError as a page message."""
    try:
        fn(*args, actor=request.user, ip=client_ip(request), **kwargs)
    except freight.FreightError as exc:
        messages.error(request, str(exc))
        return False
    messages.success(request, success)
    return True


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def freight_line_add(request: HttpRequest, pk: int) -> HttpResponse:
    batch = get_object_or_404(FreightBatch, pk=pk)
    type_id = _int(request.POST.get("type_id"))
    quantity = _int(request.POST.get("quantity"))
    if not type_id or not quantity:
        messages.error(request, _("Pick an item and a quantity."))
        return redirect("logistics:freight_detail", pk=pk)
    _handle(
        request, freight.add_line, batch,
        type_id=type_id, quantity=quantity,
        unit_purchase_cost=_decimal(request.POST.get("unit_purchase_cost")),
        purchase_ref=(request.POST.get("purchase_ref") or "").strip(),
        success=_("Line added."),
    )
    return redirect("logistics:freight_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def freight_line_edit(request: HttpRequest, line_pk: int) -> HttpResponse:
    line = get_object_or_404(FreightBatchLine, pk=line_pk)
    kwargs = {}
    if request.POST.get("quantity"):
        kwargs["quantity"] = _int(request.POST.get("quantity"))
    if "unit_purchase_cost" in request.POST:
        kwargs["unit_purchase_cost"] = _decimal(request.POST.get("unit_purchase_cost"))
    if "purchase_ref" in request.POST:
        kwargs["purchase_ref"] = (request.POST.get("purchase_ref") or "").strip()
    _handle(request, freight.edit_line, line, success=_("Line updated."), **kwargs)
    return redirect("logistics:freight_detail", pk=line.batch_id)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def freight_line_remove(request: HttpRequest, line_pk: int) -> HttpResponse:
    line = get_object_or_404(FreightBatchLine, pk=line_pk)
    batch_id = line.batch_id
    _handle(request, freight.remove_line, line, success=_("Line removed."))
    return redirect("logistics:freight_detail", pk=batch_id)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def freight_assign(request: HttpRequest, pk: int) -> HttpResponse:
    batch = get_object_or_404(FreightBatch, pk=pk)
    form = AssignForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Pick a ship class."))
        return redirect("logistics:freight_detail", pk=pk)
    _handle(
        request, freight.assign_batch, batch,
        ship_class=form.cleaned_data["ship_class"], rush=form.cleaned_data["rush"],
        success=_("Batch assigned to the courier flow."),
    )
    return redirect("logistics:freight_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def freight_assign_haul(request: HttpRequest, pk: int) -> HttpResponse:
    batch = get_object_or_404(FreightBatch, pk=pk)
    _handle(request, freight.assign_to_haul_board, batch,
            success=_("Batch posted to the member haul board."))
    return redirect("logistics:freight_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def freight_unassign(request: HttpRequest, pk: int) -> HttpResponse:
    batch = get_object_or_404(FreightBatch, pk=pk)
    _handle(request, freight.unassign_batch, batch, success=_("Batch unassigned."))
    return redirect("logistics:freight_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def freight_depart(request: HttpRequest, pk: int) -> HttpResponse:
    batch = get_object_or_404(FreightBatch, pk=pk)
    _handle(request, freight.mark_departed, batch, success=_("Batch marked departed."))
    return redirect("logistics:freight_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def freight_eta(request: HttpRequest, pk: int) -> HttpResponse:
    from django.utils.dateparse import parse_datetime

    batch = get_object_or_404(FreightBatch, pk=pk)
    raw = (request.POST.get("eta") or "").strip()
    eta = parse_datetime(raw) if raw else None
    if eta is None:
        messages.error(request, _("Enter a valid ETA."))
        return redirect("logistics:freight_detail", pk=pk)
    if timezone.is_naive(eta):
        eta = timezone.make_aware(eta)
    _handle(request, freight.update_eta, batch, eta=eta, success=_("ETA updated."))
    return redirect("logistics:freight_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def freight_arrive(request: HttpRequest, pk: int) -> HttpResponse:
    batch = get_object_or_404(FreightBatch, pk=pk)
    _handle(request, freight.mark_arrived, batch, success=_("Batch marked arrived."))
    return redirect("logistics:freight_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def freight_receive(request: HttpRequest, line_pk: int) -> HttpResponse:
    line = get_object_or_404(FreightBatchLine, pk=line_pk)
    quantity = _int(request.POST.get("quantity"))
    if not quantity:
        messages.error(request, _("Enter a quantity to receive."))
        return redirect("logistics:freight_detail", pk=line.batch_id)
    _handle(request, freight.receive_line, line, quantity, success=_("Stock received."))
    return redirect("logistics:freight_detail", pk=line.batch_id)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def freight_cancel(request: HttpRequest, pk: int) -> HttpResponse:
    batch = get_object_or_404(FreightBatch, pk=pk)
    _handle(request, freight.cancel_batch, batch, success=_("Batch cancelled."))
    return redirect("logistics:freight_board")
