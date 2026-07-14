"""Mining ledger views: participation + tax (officer), and profit-split payouts."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

from core import rbac
from core.rbac import role_required

from . import services
from .models import MiningPayout, MiningPayoutLine, MiningTaxConfig


def _window(request, default: int = 30) -> tuple[dt.date, dt.date, int]:
    try:
        days = max(1, min(int(request.GET.get("days", default)), 365))
    except (TypeError, ValueError):
        days = default
    today = timezone.now().date()
    return today - dt.timedelta(days=days - 1), today, days


# A sane ceiling: well within the column width (max_digits=24) and far above any real
# payout, so a crafted huge/negative pool can't corrupt lines or overflow the field.
_MAX_POOL = Decimal("1e18")


def _clean_pool(raw, default):
    """Parse a pool-ISK value in [0, 1e18); ``None`` if it's invalid/out of range."""
    if raw in (None, ""):
        return default
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError):
        return None
    if value < 0 or value >= _MAX_POOL:
        return None
    return value.quantize(Decimal("0.01"))


@login_required
def my_mining(request: HttpRequest) -> HttpResponse:
    """A pilot's own mining: their m³, Jita value, tickets and payout owed/paid.

    Member-facing and strictly self-scoped — reads only the requesting account's own
    characters, so a pilot can never see another's ledger. Caveats that only
    refinery-observer mining is recorded.
    """
    from apps.sso.models import EveCharacter

    character_ids = list(
        EveCharacter.objects.filter(user=request.user).values_list("character_id", flat=True)
    )
    start, end, days = _window(request, default=90)
    return render(request, "mining/my_mining.html", {
        "days": days,
        "summary": services.my_mining_summary(character_ids, start, end),
        "payouts": services.my_payout_lines(request.user, character_ids),
        "tickets": services.my_mining_tickets(request.user),
        "milestones": services.mining_milestones(character_ids),  # MIN-4 (3.10)
        "rate_pct": (services.active_tax_rate() * 100).quantize(Decimal("0.01")),
        "has_characters": bool(character_ids),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def ledger(request: HttpRequest) -> HttpResponse:
    """Participation board: who mined how much, its value, and the tax owed."""
    start, end, days = _window(request)
    rate = services.active_tax_rate()
    rows = services.participation(start, end)
    for r in rows:
        r["tax"] = (r["value"] * rate).quantize(Decimal("0.01"))
    total_value = sum((r["value"] for r in rows), start=Decimal("0"))
    total_tax = sum((r["tax"] for r in rows), start=Decimal("0"))
    return render(request, "mining/ledger.html", {
        "rows": rows, "days": days, "rate": rate,
        "total_value": total_value, "total_tax": total_tax,
        "rate_pct": (rate * 100).quantize(Decimal("0.01")),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def sync_ledger(request: HttpRequest) -> HttpResponse:
    from core.audit import audit_log, client_ip

    from .sync import sync_mining_ledger

    result = sync_mining_ledger()
    audit_log(request.user, "mining.ledger_sync", target_type="corp", target_id="mining",
              metadata={"status": result["status"]}, ip=client_ip(request))
    if result["status"] == "ok":
        messages.success(
            request,
            _("Ledger synced — %(count)s entries.") % {"count": result["entries"]},
        )
    elif result["status"] == "no_token":
        messages.warning(request, _("No character has granted the corp-mining scope yet."))
    else:
        messages.error(request, _("Ledger sync failed; try again later."))
    return redirect("mining:ledger")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def set_tax(request: HttpRequest) -> HttpResponse:
    try:
        pct = Decimal(request.POST.get("rate_pct") or "0")
    except (InvalidOperation, TypeError):
        messages.error(request, _("Invalid tax rate."))
        return redirect("mining:ledger")
    rate = max(Decimal("0"), min(pct / 100, Decimal("1")))
    MiningTaxConfig.objects.update(is_active=False)
    MiningTaxConfig.objects.create(rate=rate, is_active=True)
    from core.audit import audit_log, client_ip
    audit_log(request.user, "mining.set_tax", target_type="mining_tax_config",
              target_id="active", metadata={"rate": str(rate)}, ip=client_ip(request))
    messages.success(request, _("Mining tax set to %(rate)s.") % {"rate": f"{rate:.2%}"})
    return redirect("mining:ledger")


@login_required
@role_required(rbac.ROLE_OFFICER)
def payouts(request: HttpRequest) -> HttpResponse:
    return render(request, "mining/payouts.html", {
        "payouts": MiningPayout.objects.all()[:50],
        "methods": MiningPayout.Method.choices,
        "today": timezone.now().date().isoformat(),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def payout_create(request: HttpRequest) -> HttpResponse:
    name = (request.POST.get("name") or "").strip()
    start = parse_date(request.POST.get("period_start") or "")
    end = parse_date(request.POST.get("period_end") or "")
    if not name or start is None or end is None or start > end:
        messages.error(request, _("A payout needs a name and a valid date range."))
        return redirect("mining:payouts")
    pool = _clean_pool(request.POST.get("pool_isk"), Decimal("0"))
    if pool is None:
        messages.error(request, _("Pool ISK must be a number between 0 and 1e18."))
        return redirect("mining:payouts")
    method = request.POST.get("method")
    if method not in MiningPayout.Method.values:
        method = MiningPayout.Method.BY_VALUE
    payout = MiningPayout.objects.create(
        name=name, period_start=start, period_end=end, pool_isk=pool, method=method,
        tax_rate=services.active_tax_rate(), created_by=request.user,
    )
    n = services.build_payout(payout)
    from core.audit import audit_log, client_ip
    audit_log(request.user, "mining.payout_create", target_type="mining_payout",
              target_id=str(payout.pk),
              metadata={"pool_isk": str(pool), "method": method, "participants": n},
              ip=client_ip(request))
    messages.success(request, ngettext(
        "Payout created with %(n)s participant.",
        "Payout created with %(n)s participants.",
        n,
    ) % {"n": n})
    return redirect("mining:payout", pk=payout.pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
def payout_detail(request: HttpRequest, pk: int) -> HttpResponse:
    payout = get_object_or_404(MiningPayout, pk=pk)
    lines = list(payout.lines.all())
    return render(request, "mining/payout.html", {
        "payout": payout, "lines": lines,
        "total_gross": sum((line.gross for line in lines), start=Decimal("0")),
        "total_tax": sum((line.tax for line in lines), start=Decimal("0")),
        "total_net": sum((line.net for line in lines), start=Decimal("0")),
        "paid_count": sum(1 for line in lines if line.paid),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def payout_recompute(request: HttpRequest, pk: int) -> HttpResponse:
    payout = get_object_or_404(MiningPayout, pk=pk)
    if payout.status == MiningPayout.Status.FINAL:
        messages.error(request, _("A finalised payout can't be recomputed."))
        return redirect("mining:payout", pk=pk)
    pool = _clean_pool(request.POST.get("pool_isk"), payout.pool_isk)
    if pool is None:
        messages.error(request, _("Pool ISK must be a number between 0 and 1e18."))
        return redirect("mining:payout", pk=pk)
    payout.pool_isk = pool
    payout.tax_rate = services.active_tax_rate()
    payout.save(update_fields=["pool_isk", "tax_rate", "updated_at"])
    services.build_payout(payout)
    from core.audit import audit_log, client_ip
    audit_log(request.user, "mining.payout_recompute", target_type="mining_payout",
              target_id=str(pk), metadata={"pool_isk": str(pool)}, ip=client_ip(request))
    messages.success(request, _("Payout recomputed."))
    return redirect("mining:payout", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def line_paid(request: HttpRequest, pk: int, line_id: int) -> HttpResponse:
    from django.db import transaction

    from core.audit import audit_log, client_ip

    # Lock the line for the read-modify-write so two concurrent toggles can't both
    # flip it (and both fire the contribution credit); scope it to its parent
    # payout to keep the lookup IDOR-safe.
    with transaction.atomic():
        line = get_object_or_404(
            MiningPayoutLine.objects.select_for_update().select_related("payout"),
            pk=line_id, payout_id=pk,
        )
        # A finalised payout is frozen — its paid flags are the corp's record of
        # who was paid, so they can't be flipped after the fact (mirrors the
        # recompute guard). Pay lines first, then finalise to lock.
        if line.payout.status == MiningPayout.Status.FINAL:
            messages.error(request, _("A finalised payout is locked; paid status can't be changed."))
            return redirect("mining:payout", pk=pk)
        line.paid = not line.paid
        line.save(update_fields=["paid"])
        credited = False
        from apps.pilots.models import ContributionEvent
        if line.paid and line.user_id:
            from apps.pilots.services import record_contribution
            record_contribution(
                line.user, ContributionEvent.Kind.MINING, line.net, "isk",
                # The payout's own name — the "Mining payout:" prefix restated the kind.
                description=line.payout.name,
                ref_type="mining_payout_line", ref_id=str(line.id),
            )
            credited = True
        elif not line.paid:
            # Un-marking a line as paid reverses its mining credit, so the ledger
            # never keeps a contribution for a payment that was undone (mirrors the
            # fleet un-credit on un-attend).
            ContributionEvent.objects.filter(
                kind=ContributionEvent.Kind.MINING,
                ref_type="mining_payout_line", ref_id=str(line.id),
            ).delete()
    audit_log(request.user, "mining.line_paid", target_type="mining_payout_line",
              target_id=str(line.id),
              metadata={"payout": pk, "paid": line.paid, "credited": credited},
              ip=client_ip(request))
    return redirect("mining:payout", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def payout_finalise(request: HttpRequest, pk: int) -> HttpResponse:
    payout = get_object_or_404(MiningPayout, pk=pk)
    if payout.status == MiningPayout.Status.FINAL:
        return redirect("mining:payout", pk=pk)
    payout.status = MiningPayout.Status.FINAL
    payout.save(update_fields=["status", "updated_at"])
    from core.audit import audit_log, client_ip
    audit_log(request.user, "mining.payout_finalise", target_type="mining_payout",
              target_id=str(pk), metadata={"pool_isk": str(payout.pool_isk)},
              ip=client_ip(request))
    messages.success(request, _("Payout finalised."))
    return redirect("mining:payout", pk=pk)
