"""Cost & profitability: actual-vs-estimated margin, quote drift, revenue evidence.

The margin authority for the Corp Store. Everything here reads FROZEN order columns
plus its own leaf tables (:class:`OrderSettlement`, :class:`OrderBasisDrift`) — it never
mutates a live order (drift and settlement live in their own rows), never calls a pricing
engine from the summary path, and never fabricates an actual (revenue is evidence — a
token-matched journal line or an officer-recorded completed contract — or it is
"unevidenced").

Four pieces:

* ``margin_summary``      — actual-vs-estimated margin by fulfilment method (A4).
* ``reconcile_settlements`` — match delivered orders to corp-wallet revenue (A2).
* ``check_quote_drift``   — flag open orders whose frozen basis drifted (A3).
* the cost-basis registry — the P4/P6 actual-cost seam (A5), exercised empty in v1.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import (
    FULFILMENT_STAMP_CHOICES,
    MarginConfig,
    OrderBasisDrift,
    OrderSettlement,
    PriceBasis,
    StoreOrder,
)

log = logging.getLogger("forca.store")

# Money-IN wallet ref types a buyer's payment can arrive under (the mirror image of the
# buyback money-OUT donation match). The payment TOKEN in the reason is the collision-proof
# evidence — never a party heuristic.
_REVENUE_REF_TYPES = ("player_donation", "player_trading")
# ESI terminal states that mean the contract actually COMPLETED (buyer paid) — the
# procurement vocabulary. ``date_completed`` alone is set for rejected/cancelled/failed
# too, so it must never be the completion test (that would fabricate never-paid revenue).
_FINISHED_CONTRACT_STATUSES = ("finished", "finished_issuer", "finished_contractor", "accepted")
# Statuses whose frozen basis is still meaningful to drift-check: prices freeze once at
# placement and a claim never re-prices, so an unclaimed capital quote drifts exactly like a
# claimed one. READY+ is already built — its basis is moot.
_DRIFT_STATUSES = (
    StoreOrder.Status.OPEN, StoreOrder.Status.CLAIMED,
    StoreOrder.Status.DEPOSIT_PAID, StoreOrder.Status.IN_PRODUCTION,
)
# Fresh-flag count in one run that, on its own, trips the leadership margin-erosion alert.
_DRIFT_SPIKE = 5

# Machine code → translated label for a drift snapshot's provenance (the SUGGESTION_LABELS
# discipline: codes machine-English, labels resolved at render).
BASIS_SOURCE_LABELS = {
    "everef": _("EVE Ref job cost"),
    "estimate": _("Local material estimate"),
    "jita": _("Jita sell reference"),
    "unknown": _("Unknown — cost source unavailable"),
}

_STAMP_LABELS = {code: label for code, label in FULFILMENT_STAMP_CHOICES}


def basis_source_label(code: str):
    return BASIS_SOURCE_LABELS.get(code, code)


def method_label(code: str):
    """Human label for a stamped fulfilment method (blank = 'Unrecorded')."""
    if not code:
        return _("Unrecorded")
    return _STAMP_LABELS.get(code, code)


def _q(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator is None or denominator <= 0:
        return None
    return (numerator / denominator).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
#  A5 — cost-basis provider registry (the P4/P6 actual-cost seam)
# --------------------------------------------------------------------------- #

# Keyed by fulfilment-method lane ("supplier", "import", …). One owner per lane. The
# callable returns ``{"unit_cost": Decimal, "source": str}`` from PERSISTED evidence only
# (no pricing calls, no ESI) or ``None`` = no evidence, fall back to the labelled estimate.
COST_BASIS: dict[str, Callable[[StoreOrder], dict | None]] = {}


def register_cost_basis(lane: str, fn: Callable[[StoreOrder], dict | None]) -> None:
    """Register the per-order actual-cost provider for a fulfilment lane.

    One owner per lane — re-registering raises (the board-provider rule). P4 registers
    ``supplier`` (PO prices) and P6 ``import`` (landed cost) when they land; v1 registers
    nothing, so the labelled-estimate fallback IS the v1 behaviour."""
    if lane in COST_BASIS:
        raise ValueError(f"cost basis for lane {lane!r} is already registered")
    COST_BASIS[lane] = fn


def cost_basis_for(order: StoreOrder) -> dict | None:
    """The registered actual-cost provider's answer for this order's lane, or ``None``.

    A provider fault degrades to the estimate lane — it must never break the summary."""
    fn = COST_BASIS.get(order.fulfilment_method or "")
    if fn is None:
        return None
    try:
        result = fn(order)
    except Exception:  # noqa: BLE001 — a provider fault must never break the summary
        log.exception("cost-basis provider failed for lane %s", order.fulfilment_method)
        return None
    if result is None or result.get("unit_cost") is None:
        return None
    return result


# --------------------------------------------------------------------------- #
#  A4 — margin summary
# --------------------------------------------------------------------------- #


@dataclass
class MarginMethodRow:
    """One fulfilment-method group's margin over the window."""

    method: str                     # machine code ("stock"/…/""=unrecorded)
    order_count: int = 0
    quoted_revenue: Decimal = Decimal("0")      # frozen total_price, all orders
    evidenced_revenue: Decimal = Decimal("0")   # settlement sums, evidenced orders
    unevidenced_count: int = 0
    estimate_cost: Decimal = Decimal("0")       # cost lane, all orders
    evidenced_cost: Decimal = Decimal("0")      # cost lane, evidenced orders only
    has_reference_cost: bool = False            # a Jita reference was used (not a real cost)
    cost_sources: set = field(default_factory=set)

    @property
    def label(self):
        return method_label(self.method)

    @property
    def estimate_margin(self) -> Decimal:
        return self.quoted_revenue - self.estimate_cost

    @property
    def evidence_margin(self) -> Decimal | None:
        if not self.evidenced_revenue:
            return None
        return self.evidenced_revenue - self.evidenced_cost


def _order_cost(order: StoreOrder) -> tuple[Decimal, str, bool]:
    """(cost, source_code, is_reference) for one delivered order.

    A registered actual-cost provider wins (source from the provider). Otherwise a
    BUILD-basis order uses its frozen ``unit_cost`` (source ``estimate``) and every other
    lane uses the frozen ``unit_jita`` as a labelled Jita REFERENCE — not a real cost."""
    q = int(order.quantity or 0)
    provided = cost_basis_for(order)
    if provided is not None:
        return (Decimal(provided["unit_cost"]) * q, str(provided.get("source") or "actual"), False)
    if order.price_basis == PriceBasis.BUILD:
        return (order.unit_cost * q, "estimate", False)
    return (order.unit_jita * q, "jita", True)


def margin_summary(*, window_days: int | None = None) -> dict:
    """Actual-vs-estimated margin per delivered order, grouped by fulfilment method.

    Pure arithmetic over frozen columns + settlements — NO pricing calls, no second margin
    calculator. Revenue is quoted (frozen ``total_price``) and, where evidence exists,
    evidenced (settlement sums); the shortfall is counted as unevidenced. The cost side is
    the labelled estimate lane (or a registered actual-cost provider). Honest by
    construction: v1's non-BUILD costs are Jita references, flagged as such."""
    cfg = MarginConfig.active()
    window = int(window_days or cfg.margin_window_days)
    since = timezone.now() - timedelta(days=window)
    orders = list(
        StoreOrder.objects.filter(
            status=StoreOrder.Status.DELIVERED, delivered_at__gte=since,
        ).order_by("delivered_at")
    )
    order_ids = [o.pk for o in orders]
    evidenced: dict[int, Decimal] = {}
    if order_ids:
        for row in (
            OrderSettlement.objects.filter(order_id__in=order_ids, occurred_at__isnull=False)
            .values("order_id").annotate(s=Sum("amount"))
        ):
            evidenced[row["order_id"]] = row["s"] or Decimal("0")

    groups: dict[str, MarginMethodRow] = {}
    for o in orders:
        key = o.fulfilment_method or ""
        g = groups.get(key)
        if g is None:
            g = groups[key] = MarginMethodRow(method=key)
        g.order_count += 1
        g.quoted_revenue += o.total_price
        cost, source, is_ref = _order_cost(o)
        g.estimate_cost += cost
        g.cost_sources.add(source)
        if is_ref:
            g.has_reference_cost = True
        ev = evidenced.get(o.pk)
        if ev is None:
            g.unevidenced_count += 1
        else:
            g.evidenced_revenue += ev
            g.evidenced_cost += cost

    rows = sorted(groups.values(), key=lambda r: (r.method == "", r.method))
    t_quoted = sum((r.quoted_revenue for r in rows), Decimal("0"))
    t_evidenced = sum((r.evidenced_revenue for r in rows), Decimal("0"))
    t_estimate_cost = sum((r.estimate_cost for r in rows), Decimal("0"))
    t_evidenced_cost = sum((r.evidenced_cost for r in rows), Decimal("0"))
    t_orders = sum((r.order_count for r in rows), 0)
    t_uneviden = sum((r.unevidenced_count for r in rows), 0)
    evidence_margin = (t_evidenced - t_evidenced_cost) if t_evidenced else None
    return {
        "window_days": window,
        "methods": rows,
        "totals": {
            "order_count": t_orders,
            "unevidenced_count": t_uneviden,
            "quoted_revenue": t_quoted,
            "evidenced_revenue": t_evidenced,
            "estimate_cost": t_estimate_cost,
            "evidenced_cost": t_evidenced_cost,
            "estimate_margin": t_quoted - t_estimate_cost,
            "evidence_margin": evidence_margin,
            "evidence_margin_ratio": _ratio(evidence_margin, t_evidenced)
            if evidence_margin is not None else None,
        },
        "has_reference_cost": any(r.has_reference_cost for r in rows),
    }


# --------------------------------------------------------------------------- #
#  A2 — settlement reconcile (token journal match + contract fill)
# --------------------------------------------------------------------------- #


def reconcile_settlements(*, limit: int = 200) -> dict:
    """Match delivered/ready orders to corp-wallet revenue (evidence, never inference).

    Two lanes, mirroring the buyback reconcile with the sign flipped to money IN:
    (1) a journal line carrying the order's ``SO-{pk}-`` token, dated on/after the order,
    for at least the outstanding total — created as a token settlement (the
    ``journal_entry_id`` partial unique collapses racing workers); (2) a pending
    officer-recorded contract link is filled with ``price``/``date_completed`` once the
    matching ``CorpContract`` completes. INERT unless armed. Stamps ``record_sync`` so
    console staleness is honest."""
    cfg = MarginConfig.active()
    if not cfg.settlement_reconcile_enabled:
        return {"status": "disabled"}

    from apps.admin_audit.health import record_sync
    from apps.corporation.models import CorpWalletJournalEntry
    from apps.logistics.models import CorpContract

    now = timezone.now()
    since = now - timedelta(days=2 * int(cfg.margin_window_days))
    used = set(
        OrderSettlement.objects.filter(journal_entry_id__isnull=False)
        .values_list("journal_entry_id", flat=True)
    )
    linked = 0
    orders = list(
        StoreOrder.objects.filter(
            status__in=(StoreOrder.Status.READY, StoreOrder.Status.DELIVERED),
            created_at__gte=since,
        ).order_by("pk")[:limit]
    )
    # One evidence lane per order (DECISION 1): an order being settled by a recorded
    # contract must never ALSO token-match a wallet line — that is the only way the two
    # lanes could double-count. Pre-loaded once, not per-order.
    contract_orders = set(
        OrderSettlement.objects.filter(
            kind=OrderSettlement.Kind.CONTRACT, order_id__in=[o.pk for o in orders]
        ).values_list("order_id", flat=True)
    )
    for order in orders:
        if order.pk in contract_orders:
            continue
        already = (
            OrderSettlement.objects.filter(order=order, occurred_at__isnull=False)
            .aggregate(s=Sum("amount"))["s"] or Decimal("0")
        )
        remaining = order.total_price - already
        if remaining <= 0:
            continue
        token = order.payment_token
        entry = (
            CorpWalletJournalEntry.objects.filter(
                ref_type__in=_REVENUE_REF_TYPES,
                date__gte=order.created_at,
                amount__gte=remaining,          # money IN — the flipped-sign pin
                reason__icontains=token,        # trailing dash: SO-5- never matches SO-50-
            ).order_by("date").first()
        )
        if entry is None or entry.entry_id in used:
            continue
        # Seam B: the beat-written note is pinned English (locale-independent, never the
        # worker's locale, never an f-string) — officer-typed notes stay verbatim.
        from apps.erp.messages import english_text

        note = english_text("order.settlement_matched", {"token": token})
        try:
            with transaction.atomic():
                OrderSettlement.objects.create(
                    order=order, kind=OrderSettlement.Kind.JOURNAL,
                    matched_by=OrderSettlement.MatchedBy.TOKEN,
                    journal_entry_id=entry.entry_id, amount=entry.amount,
                    occurred_at=entry.date, note=note,
                )
        except IntegrityError:
            continue  # another worker took this entry — partial-unique collapse
        used.add(entry.entry_id)
        linked += 1

    filled = 0
    pending = list(
        OrderSettlement.objects.filter(
            kind=OrderSettlement.Kind.CONTRACT, occurred_at__isnull=True,
            contract_id__isnull=False,
        ).order_by("pk")[:limit]
    )
    for sett in pending:
        # A contract that actually COMPLETED (buyer paid) — not merely reached a terminal
        # state. ``date_completed`` is populated for rejected/cancelled/failed too, so
        # gating on it alone would fabricate never-paid revenue.
        contract = (
            CorpContract.objects.filter(
                contract_id=sett.contract_id,
                status__in=_FINISHED_CONTRACT_STATUSES,
            ).exclude(date_completed=None).first()
        )
        if contract is None:
            continue
        # Guarded fill: idempotent, and a concurrent fill collapses on the isnull filter.
        if OrderSettlement.objects.filter(pk=sett.pk, occurred_at__isnull=True).update(
            amount=contract.price, occurred_at=contract.date_completed,
        ):
            filled += 1

    record_sync("store_settlements", linked=linked, filled=filled)
    return {"linked": linked, "filled": filled}


# --------------------------------------------------------------------------- #
#  A3 — quote drift (pinned prices, persisted snapshots, ack watermark)
# --------------------------------------------------------------------------- #


def _pinned_price(snapshot: dict):
    """A ``price_for``-compatible callable over ONE immutable snapshot — the price-cache
    TTL must not flip numbers mid-run (the P3-documented failure mode)."""
    jita, adjusted = snapshot["jita"], snapshot["adjusted"]

    def price(type_id: int) -> Decimal:
        value = jita.get(type_id)
        if value is None:
            value = adjusted.get(type_id)
        return value if value is not None else Decimal("0")

    return price


def _reprice_manifest(order: StoreOrder, price) -> Decimal | None:
    """Re-price the frozen per-unit manifest through the pinned snapshot.

    ``None`` when ANY line that carried a frozen price is now unpriced — a cold OR PARTIAL
    market sync must read as "unknown", never a fabricated drift. A line only priced 0 now
    is a market-data gap (not a real move) exactly when it was priced > 0 at freeze time;
    a genuinely-free line (frozen 0) staying 0 is fine."""
    total = Decimal("0")
    for item in order.manifest or []:
        tid = item.get("type_id")
        if not tid:
            continue
        current_unit = price(int(tid))
        frozen_unit = Decimal(str(item.get("unit_jita") or "0"))
        if frozen_unit > 0 and current_unit <= 0:
            return None  # a previously-priced line lost its price → gap, not drift
        total += current_unit * int(item.get("quantity", 0) or 0)
    return _q(total)


@dataclass
class _DriftEval:
    checked: bool
    newly_flagged: bool


def _evaluate_drift(order: StoreOrder, *, price, threshold: Decimal, floor: Decimal,
                    now) -> _DriftEval:
    """Compute + persist one order's drift. Compare-before-write: an unchanged re-run
    writes nothing (timestamps stay stable). Returns whether it counted and freshly flagged."""
    from .pricing import production_cost_detail

    basis = order.price_basis
    if basis == PriceBasis.BUILD:
        frozen = order.unit_cost
        detail = production_cost_detail(order.ship_type_id)
        if detail is None:
            current, source = None, "unknown"
        else:
            current, source = _q(detail["cost"]), detail["source"]
    else:
        frozen = order.unit_jita
        current = _reprice_manifest(order, price)
        source = "jita" if current is not None else "unknown"

    drift_pct = None
    raw_flag = False
    if current is not None and frozen and frozen > 0:
        delta = abs(current - frozen)
        drift_pct = ((current - frozen) / frozen).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        if abs(drift_pct) >= threshold and delta >= floor:
            raw_flag = True

    existing = OrderBasisDrift.objects.filter(order=order).first()
    ack = existing.acknowledged_pct if existing else None
    if current is None:
        # Missing data is "unknown", never "no drift": keep the prior flag state rather
        # than clearing a live flag during a cost-source outage (which would also re-ping
        # officers on recovery). basis_source="unknown"/current_value=None self-explain it.
        flagged = existing.flagged if existing is not None else False
    else:
        # Respect the ack watermark: re-flag only once |drift| moves another threshold
        # step past what the officer acknowledged.
        flagged = raw_flag
        if raw_flag and ack is not None and drift_pct is not None and \
                abs(drift_pct) < (abs(ack) + threshold):
            flagged = False

    new_values = {
        "checked_at": now,
        "basis": basis,
        "basis_source": source,
        "frozen_value": frozen or Decimal("0"),
        "current_value": current,
        "drift_pct": drift_pct,
        "flagged": flagged,
    }
    if existing is None:
        OrderBasisDrift.objects.create(order=order, acknowledged_pct=None, **new_values)
        return _DriftEval(checked=True, newly_flagged=flagged)

    # Compare-before-write, excluding the timestamp: an otherwise-unchanged row must not
    # be rewritten (idempotency / stable timestamps, §9).
    substantive = [
        f for f in ("basis", "basis_source", "frozen_value", "current_value",
                    "drift_pct", "flagged")
        if getattr(existing, f) != new_values[f]
    ]
    if not substantive:
        return _DriftEval(checked=True, newly_flagged=False)
    was_flagged = existing.flagged
    for f, v in new_values.items():
        setattr(existing, f, v)
    existing.save(update_fields=[*new_values.keys()])
    return _DriftEval(checked=True, newly_flagged=flagged and not was_flagged)


def check_quote_drift(*, limit: int = 500) -> dict:
    """Flag open orders whose frozen basis drifted beyond threshold AND floor.

    One ``price_maps`` snapshot pins the whole run. BUILD basis re-estimates through
    ``production_cost_detail`` (breaker ⇒ ``unknown``, flags nothing); JITA basis re-prices
    the frozen manifest through the pinned snapshot. Never touches the order row. A fresh
    flag pings ``store.quote_drift`` (officer); the run's tail may raise the leadership
    margin-erosion alert. INERT unless armed. Stamps ``record_sync``."""
    cfg = MarginConfig.active()
    if not cfg.drift_check_enabled:
        return {"status": "disabled"}

    from apps.admin_audit.health import record_sync
    from apps.market.pricing import price_maps

    now = timezone.now()
    price = _pinned_price(price_maps())
    threshold = cfg.drift_threshold_pct
    floor = cfg.drift_min_isk
    orders = list(
        StoreOrder.objects.filter(status__in=_DRIFT_STATUSES).order_by("pk")[:limit]
    )
    checked = flagged = 0
    newly = []
    for order in orders:
        result = _evaluate_drift(order, price=price, threshold=threshold, floor=floor, now=now)
        if not result.checked:
            continue
        checked += 1
        if result.newly_flagged:
            flagged += 1
            newly.append(order)

    for order in newly:
        _emit_drift_ping(order)
    _maybe_emit_erosion(cfg, newly_flagged=len(newly))
    record_sync("store_drift", checked=checked, flagged=flagged)
    return {"checked": checked, "flagged": flagged}


def _pct_str(ratio) -> str:
    if ratio is None:
        return "—"
    return f"{float(ratio) * 100:+.1f}%"


def _emit_drift_ping(order: StoreOrder) -> None:
    """Officer ping for a freshly-flagged drift (registered event, best-effort)."""
    try:
        from apps.pingboard import services as pingboard
        from apps.pingboard.models import AlertCategory
        from apps.pingboard.notifications import is_enabled

        if not is_enabled("store.quote_drift"):
            return
        drift = OrderBasisDrift.objects.filter(order=order).first()
        pct = _pct_str(drift.drift_pct if drift else None)
        ship = order.ship_name or order.fit_name or f"Order #{order.pk}"
        day = timezone.now().strftime("%Y%m%d")
        pingboard.emit_broadcast(
            category=AlertCategory.CUSTOM,
            title="Quote drift flagged",
            body=(
                f"The frozen quote for {ship} has drifted {pct} from its basis. "
                "Review it on the margin console: /store/margin/"
            ),
            template="store.quote_drift",
            context={"ship_name": ship, "percent": pct, "link": "/store/margin/"},
            audience={"kind": "officer"},
            source_service="store",
            source_object_id=f"quote_drift:{order.pk}:{day}",
            idempotency_key=f"store:quote_drift:{order.pk}:{day}",
        )
    except Exception:  # noqa: BLE001 — a notification must never break the beat
        log.exception("quote-drift ping failed (order %s)", order.pk)


def _maybe_emit_erosion(cfg: MarginConfig, *, newly_flagged: int) -> None:
    """Leadership margin-erosion alert (from the drift beat's tail, best-effort).

    Fires when the window's evidenced-margin ratio drops below the floor, or a run flags a
    spike of fresh drift rows. Leadership-routed (``leadership_audience``) so ISK never
    reaches a mass channel; at most one per day via the idempotency key."""
    try:
        summary = margin_summary(window_days=cfg.margin_window_days)
        ratio = summary["totals"]["evidence_margin_ratio"]
        low_margin = ratio is not None and ratio < cfg.margin_alert_floor_pct
        spike = newly_flagged >= _DRIFT_SPIKE
        if not (low_margin or spike):
            return

        from apps.pingboard import services as pingboard
        from apps.pingboard.models import AlertCategory
        from apps.pingboard.notifications import is_enabled, leadership_audience

        if not is_enabled("store.margin_erosion"):
            return
        day = timezone.now().strftime("%Y%m%d")
        pct = _pct_str(ratio)
        detail = (
            f"{newly_flagged} newly-flagged drift(s)" if spike
            else f"evidenced margin {pct}"
        )
        pingboard.emit_broadcast(
            category=AlertCategory.CUSTOM,
            title="Margin erosion",
            body=(
                f"Corp Store margin is eroding ({detail}). Review the margin console: "
                "/store/margin/"
            ),
            template="store.margin_erosion",
            context={"percent": pct, "count": newly_flagged,
                     "details": detail, "link": "/store/margin/"},
            audience=leadership_audience(),
            source_service="store",
            source_object_id=f"margin_erosion:{day}",
            idempotency_key=f"store:margin_erosion:{day}",
        )
    except Exception:  # noqa: BLE001 — a notification must never break the beat
        log.exception("margin-erosion alert failed")


# --------------------------------------------------------------------------- #
#  Console write helpers (called by views_margin; the write logic lives here)
# --------------------------------------------------------------------------- #


def record_contract_settlement(order: StoreOrder, *, contract_id, actor, note: str = "",
                               audit_action: str = "store.settlement_record") -> bool:
    """Record a *pending* contract settlement (officer-named bare contract id).

    The reconcile beat fills ``amount``/``occurred_at`` once the matching ``CorpContract``
    completes. A contract id already settling another order collapses on the partial unique
    → returns ``False`` (the conflict is surfaced, never silently reassigned). Shared by the
    READY capture path and the margin console."""
    try:
        cid = int(contract_id)
    except (TypeError, ValueError):
        return False
    if cid <= 0:
        return False
    from core.audit import audit_log

    try:
        with transaction.atomic():
            OrderSettlement.objects.create(
                order=order, kind=OrderSettlement.Kind.CONTRACT,
                matched_by=OrderSettlement.MatchedBy.OFFICER,
                contract_id=cid, amount=Decimal("0"),
                recorded_by=actor, note=(note or "")[:200],
            )
    except IntegrityError:
        return False  # this contract id already settles another order
    audit_log(actor, audit_action, target_type="store_order",
              target_id=str(order.pk), metadata={"contract_id": cid})
    return True


def acknowledge_drift(drift: OrderBasisDrift, *, actor) -> bool:
    """Officer acknowledges a flagged drift: unflag + set the ack watermark.

    Re-flag only once |drift| moves another threshold step past the acknowledged level.
    Status-guarded on ``flagged`` so a double-ack is a no-op."""
    updated = OrderBasisDrift.objects.filter(pk=drift.pk, flagged=True).update(
        flagged=False,
        acknowledged_pct=abs(drift.drift_pct) if drift.drift_pct is not None else None,
        acknowledged_by=actor,
        acknowledged_at=timezone.now(),
    )
    return bool(updated)
