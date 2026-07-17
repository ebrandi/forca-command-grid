"""Director console for cost & profitability (cross-cutting phase).

Margin by fulfilment method over the window, the flagged-quote-drift list with an inline
assumptions panel (frozen vs current, basis, source, as-of + the fee/index defaults), and
per-order settlement evidence. Director-only — the ``corporation:finance`` gate precedent.
Every mutation is a POST with ``audit_log`` + ``client_ip``; no frozen order column is ever
touched (drift/settlement live in their own tables).
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from .forms import MarginConfigForm
from .margin import BASIS_SOURCE_LABELS, acknowledge_drift, margin_summary, record_contract_settlement
from .models import MarginConfig, OrderBasisDrift, OrderSettlement, StoreOrder


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def margin_console(request: HttpRequest) -> HttpResponse:
    """Margin by method + the drift list + settlement evidence (Director-only)."""
    from apps.industry.models import IndustryEconomyConfig

    cfg = MarginConfig.active()
    summary = margin_summary(window_days=cfg.margin_window_days)
    drifts = list(
        OrderBasisDrift.objects.select_related("order", "acknowledged_by")
        .order_by("-flagged", "-checked_at")[:50]
    )
    for d in drifts:
        d.source_label = BASIS_SOURCE_LABELS.get(d.basis_source, d.basis_source)
    settlements = list(
        OrderSettlement.objects.select_related("order", "recorded_by")
        .order_by("-created_at")[:30]
    )
    return render(request, "store/margin.html", {
        "cfg": cfg,
        "form": MarginConfigForm(instance=cfg),
        "summary": summary,
        "drifts": drifts,
        "settlements": settlements,
        "econ": IndustryEconomyConfig.active(),
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def margin_config(request: HttpRequest) -> HttpResponse:
    """Update the margin & drift thresholds (audited)."""
    cfg = MarginConfig.active()
    form = MarginConfigForm(request.POST, instance=cfg)
    if form.is_valid():
        form.save()
        audit_log(request.user, "store.margin_config", target_type="margin_config",
                  target_id=str(cfg.pk),
                  metadata={"drift_check": cfg.drift_check_enabled,
                            "settlement_reconcile": cfg.settlement_reconcile_enabled},
                  ip=client_ip(request))
        messages.success(request, _("Margin settings updated."))
    else:
        for errors in form.errors.values():
            for err in errors:
                messages.error(request, err)
    return redirect("store:margin")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def drift_ack(request: HttpRequest, pk: int) -> HttpResponse:
    """Acknowledge a flagged quote drift — unflag + set the ack watermark (audited)."""
    drift = get_object_or_404(OrderBasisDrift.objects.select_related("order"), pk=pk)
    if acknowledge_drift(drift, actor=request.user):
        audit_log(request.user, "store.drift_ack", target_type="store_order",
                  target_id=str(drift.order_id),
                  metadata={"drift_pct": str(drift.drift_pct)}, ip=client_ip(request))
        messages.success(request, _("Drift acknowledged."))
    else:
        messages.info(request, _("That drift was already acknowledged."))
    return redirect("store:margin")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def settlement_record(request: HttpRequest) -> HttpResponse:
    """Record revenue evidence by hand: link an order to a completed contract id.

    The reconcile beat fills the amount/date once the contract completes — never a
    fabricated actual. Audited via the shared margin helper."""
    order_pk = request.POST.get("order_id", "")
    if not str(order_pk).isdigit():
        messages.error(request, _("Enter a valid order number."))
        return redirect("store:margin")
    order = get_object_or_404(StoreOrder, pk=int(order_pk))
    ok = record_contract_settlement(
        order, contract_id=request.POST.get("contract_id"),
        actor=request.user, note=request.POST.get("note", ""),
    )
    if ok:
        messages.success(request, _(
            "Settlement recorded — it fills once the contract completes."
        ))
    else:
        messages.error(request, _(
            "That contract id is invalid or already linked to another order."
        ))
    return redirect("store:margin")
