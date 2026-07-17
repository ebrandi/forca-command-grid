"""P4 supplier reliability rollup (nightly beat) + shared board math.

Reads FROZEN evidence only — ``unit_price_isk`` (frozen at approval) and
``unit_jita_at_receipt`` (frozen at receipt) — so a later live-price move never
changes a historical figure and the rollup is re-run stable. Over the last N
COMPLETE weeks (the P2 convention: the current partial week is excluded).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from datetime import time as dt_time
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Max, Sum
from django.utils import timezone

from .models import (
    ProcurementConfig,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplyAgreement,
)
from .services import COUNTED_STATUSES, TERMINAL_STATUSES

S = PurchaseOrder.Status
_CENT = Decimal("0.01")
_CLOSED = (PurchaseOrder.Status.DELIVERED, PurchaseOrder.Status.RECONCILED)

# The evidence feeds whose freshness the board surfaces. Stale = no stamp, or older
# than the threshold — so a disarmed evidence beat reads honestly stale, never green.
_FRESHNESS_FEEDS = (
    "corp_contracts", "procurement_match", "procurement_reconcile", "procurement_sweep",
)
_FRESHNESS_STALE_AFTER = timedelta(hours=3)


def _week_floor(now) -> datetime:
    """Midnight at the start of the current ISO week (local) — the exclusive upper
    bound so only complete weeks count."""
    local = timezone.localtime(now)
    monday = local.date() - timedelta(days=local.weekday())
    return timezone.make_aware(datetime.combine(monday, dt_time.min), local.tzinfo)


def _supplier_reliability(supplier: Supplier, window_start, window_end, grace_days: int) -> dict:
    """On-time rate, fill rate, quantity-weighted price variance and the sample
    size for one supplier's closed POs promised within the window."""
    pos = list(
        PurchaseOrder.objects.filter(
            supplier=supplier, status__in=_CLOSED,
            promised_by__isnull=False, promised_by__gte=window_start, promised_by__lt=window_end,
        ).prefetch_related("lines", "receipts")
    )
    sample = len(pos)
    if sample == 0:
        return {"on_time_rate": None, "fill_rate": None, "price_variance_pct": None, "sample": 0}

    grace = timedelta(days=grace_days)
    on_time = 0
    total_ordered = total_received = 0
    var_weight = Decimal(0)
    var_accum = Decimal(0)
    delivered = (
        PurchaseOrder.objects.filter(pk__in=[p.pk for p in pos])
        .annotate(last_receipt=Max("receipts__created_at"))
        .values_list("pk", "last_receipt")
    )
    last_receipt_by_po = dict(delivered)

    for po in pos:
        last = last_receipt_by_po.get(po.pk)
        if last is not None and po.promised_by is not None and last <= po.promised_by + grace:
            on_time += 1
        for line in po.lines.all():
            total_ordered += line.quantity_ordered
            total_received += line.quantity_received
        for receipt in po.receipts.all():
            jita = receipt.unit_jita_at_receipt or Decimal(0)
            if jita <= 0:
                continue
            unit_price = receipt.line.unit_price_isk if receipt.line_id else Decimal(0)
            var_accum += (unit_price - jita) / jita * receipt.quantity
            var_weight += receipt.quantity

    on_time_rate = (Decimal(on_time) / Decimal(sample)).quantize(Decimal("0.0001"))
    fill_rate = (
        (Decimal(total_received) / Decimal(total_ordered)).quantize(Decimal("0.0001"))
        if total_ordered else None
    )
    variance = (var_accum / var_weight).quantize(Decimal("0.0001")) if var_weight else None
    return {"on_time_rate": on_time_rate, "fill_rate": fill_rate,
            "price_variance_pct": variance, "sample": sample}


def rollup_reliability() -> dict:
    """Nightly: recompute and persist each supplier's reliability. No-op unless armed."""
    cfg = ProcurementConfig.active()
    if not cfg.reliability_rollup_enabled:
        return {"status": "disabled"}
    now = timezone.now()
    window_end = _week_floor(now)
    window_start = window_end - timedelta(weeks=cfg.reliability_window_weeks)
    updated = 0
    for supplier in Supplier.objects.all():
        stats = _supplier_reliability(supplier, window_start, window_end, cfg.overdue_grace_days)
        supplier.on_time_rate = stats["on_time_rate"]
        supplier.fill_rate = stats["fill_rate"]
        supplier.price_variance_pct = stats["price_variance_pct"]
        supplier.reliability_sample = stats["sample"]
        supplier.reliability_computed_at = now
        supplier.save(update_fields=[
            "on_time_rate", "fill_rate", "price_variance_pct",
            "reliability_sample", "reliability_computed_at", "updated_at",
        ])
        updated += 1
    return {"status": "ok", "suppliers": updated}


def _q(value) -> Decimal:
    return Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


# --- board aggregation (pure reads for the Director board) --------------------

def open_obligations() -> dict:
    """The outstanding commitment across counted POs: ISK still owed, units still
    to arrive, and how many orders carry it. Outstanding = ordered − received."""
    pos = PurchaseOrder.objects.filter(status__in=COUNTED_STATUSES)
    outstanding = F("quantity_ordered") - F("quantity_received")
    agg = PurchaseOrderLine.objects.filter(po__in=pos).aggregate(
        isk=Sum(
            ExpressionWrapper(
                F("unit_price_isk") * outstanding,
                output_field=DecimalField(max_digits=30, decimal_places=2),
            )
        ),
        units=Sum(outstanding),
    )
    return {
        "isk": agg["isk"] or Decimal(0),
        "units": agg["units"] or 0,
        "count": pos.count(),
    }


def due_and_late() -> dict:
    """The two attention lists: POs already overdue (oldest first), and counted POs
    promised within the next 7 days that are not yet overdue (soonest first)."""
    now = timezone.now()
    soon = now + timedelta(days=7)
    late = list(
        PurchaseOrder.objects.filter(status=S.OVERDUE)
        .select_related("supplier").order_by("overdue_since")
    )
    due_soon = list(
        PurchaseOrder.objects.filter(
            status__in=COUNTED_STATUSES, promised_by__isnull=False, promised_by__lte=soon,
        ).exclude(status=S.OVERDUE)
        .select_related("supplier").order_by("promised_by")
    )
    return {"late": late, "due_soon": due_soon}


def agreement_utilisation() -> list:
    """Per ACTIVE agreement line: how much of its per-cycle volume is already
    committed on live (non-terminal) POs, as an absolute count and a fraction."""
    rows = []
    for ag in (
        SupplyAgreement.objects.filter(status=SupplyAgreement.Status.ACTIVE)
        .select_related("supplier").prefetch_related("lines")
    ):
        committed_by_type = {
            row["type_id"]: row["c"]
            for row in PurchaseOrderLine.objects.filter(po__agreement=ag)
            .exclude(po__status__in=TERMINAL_STATUSES)
            .values("type_id").annotate(c=Sum("quantity_ordered"))
        }
        for line in ag.lines.all():
            committed = committed_by_type.get(line.type_id, 0) or 0
            per_cycle = line.quantity_per_cycle or 0
            pct = (
                (Decimal(committed) / Decimal(per_cycle)).quantize(Decimal("0.0001"))
                if per_cycle else Decimal(0)
            )
            rows.append({
                "agreement": ag, "type_id": line.type_id,
                "committed": committed, "per_cycle": per_cycle, "pct": pct,
            })
    return rows


def reliability_table() -> list:
    """Every supplier with its stored reliability fields, name order — the rollup's
    latest figures (never live-computed here)."""
    return list(Supplier.objects.order_by("display_name", "pk"))


def board_freshness() -> dict:
    """Per evidence-feed freshness for the board's stale chips. ``stale`` is True when
    the feed has never stamped a run or its last run is older than the threshold, so a
    disarmed or stalled beat can never render silently green."""
    from django.utils.dateparse import parse_datetime

    from apps.admin_audit.health import _last_sync

    now = timezone.now()
    out = {}
    for key in _FRESHNESS_FEEDS:
        rec = _last_sync(key)
        at = parse_datetime(rec["at"]) if rec and rec.get("at") else None
        stale = at is None or (now - at) > _FRESHNESS_STALE_AFTER
        out[key] = {"key": key, "at": at, "stale": stale}
    return out
