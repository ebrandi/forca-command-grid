"""The composed-demand authority (P2) — sibling to :mod:`apps.store.availability`.

Every "how many of this doctrine fit will the corp need?" read goes through
:func:`demand_for_fits`. It composes four independently-visible sources per
**doctrine fit** (the store's grain — hull numbers are always derived from fit
numbers, never the reverse):

* **Loss replacement** — home-corp victim killmails bucketed per ISO week over a
  trailing window, using the ingest-time ``Killmail.doctrine_fit`` tag; untagged
  hull losses are allocated across the hull's fits proportionally (a separate,
  labelled source line — the uncertainty stays visible). **NPC and awox losses
  count**: the ship is destroyed either way and must be replaced — replacement
  demand is about the loss, not the killer (killboard *performance* stats filter
  them; this deliberately does not).
* **Fleet-ops calendar** — dated events from upcoming non-recurring ops with
  fit-linked slots (``min_pilots × op_attrition_pct``). Recurring ops are
  excluded by default: their attrition is already inside the trailing loss
  history, and counting future occurrences would charge it twice.
* **Rollout target gap** — ``max(0, target_stock − (atp + incoming))``, spread
  over the horizon for rate math.
* **Manual lines** — officer-entered dated quantities (:class:`DemandLine`).

Honesty rules (Phase-0 house style): incoming supply never counts as available
(it only offsets ordering suggestions); no volatility band is shown below
``MIN_WEEKS`` of observed history — never a fabricated ±0; every composed number
decomposes into labelled sources; assumptions (attrition %, service level,
allocation) ride along in ``detail`` for the UI to print.

Demand is corp-wide in this phase (suggestions target the fit's effective
location); the per-location split happens at P3's netting layer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count
from django.db.models import Min as models_Min
from django.db.models.functions import TruncWeek
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import DemandConfig, DemandLine, ServiceLevel

#: Below this many observed weeks of corp loss history, σ and the band are None.
MIN_WEEKS = 5

#: Fixed z-score menu for the service-level knob (never a free float).
Z_SCORES = {
    ServiceLevel.P50: Decimal("0"),
    ServiceLevel.P80: Decimal("0.84"),
    ServiceLevel.P90: Decimal("1.28"),
    ServiceLevel.P95: Decimal("1.65"),
    ServiceLevel.P99: Decimal("2.33"),
}

#: linear_fit slope (losses/week per week) beyond which the trend chip shows.
_TREND_EPS = 0.05

_Q = Decimal("0.01")

# Machine keys with translated labels (the codes-with-DISPLAY_MAP house pattern —
# CSV and JSON always carry the key, templates render the label).
SOURCE_LABELS = {
    "loss_tagged": _("Loss replacement"),
    "loss_untagged": _("Untagged hull losses (allocated)"),
    "ops": _("Fleet ops"),
    "target_gap": _("Target build-up"),
    "manual": _("Manual demand lines"),
}
FLAG_LABELS = {
    "slow_mover": _("Slow mover"),
    "obsolete": _("Obsolete"),
    "upcoming": _("Upcoming"),
    "no_history": _("Insufficient history"),
}


@dataclass
class DemandSource:
    """One labelled line of a fit's demand breakdown (keys stay machine-English)."""

    key: str              # "loss_tagged" | "loss_untagged" | "ops" | "target_gap" | "manual"
    rate_week: Decimal    # 0 for pure dated events
    units: Decimal        # dated/undated quantity inside the horizon (0 for pure rates)
    detail: dict = field(default_factory=dict)

    @property
    def label(self):
        return SOURCE_LABELS.get(self.key, self.key)


@dataclass
class FitDemand:
    """Composed demand for one doctrine fit — everything a console row needs."""

    fit_id: int
    rate_week_mean: Decimal
    rate_week_hi: Decimal
    sigma_week: Decimal | None            # None below MIN_WEEKS — no fake bands
    weeks_observed: int
    buckets: list[float]                  # observed weekly losses, oldest → newest
    sources: list[DemandSource]
    events: list[tuple[date, Decimal, str]]   # dated demand (ops, manual) in horizon
    days_cover: Decimal | None            # runout projection vs ATP (None = no demand)
    days_cover_lo: Decimal | None         # same projection at the hi (p-level) rate
    suggested_reorder: int | None         # s — None when there is no demand signal
    order_up_to: int | None               # S — strictly above s when set
    suggested_order_qty: int              # ≥ 0; incoming offsets it (never cover)
    flags: list[str]                      # slow_mover | obsolete | upcoming | no_history
    trend: str                            # "rising" | "falling" | ""

    @property
    def has_band(self) -> bool:
        return self.sigma_week is not None


def planning_universe():
    """The fits demand planning covers: ACTIVE-doctrine fits ∪ fits still holding
    stock. Retired doctrines' stocked fits must stay visible (obsolete flag) —
    the pre-P2 console silently dropped them."""
    from django.db.models import Q

    from apps.doctrines.models import Doctrine, DoctrineFit

    from .models import FitStock

    stocked = FitStock.objects.filter(quantity_on_hand__gt=0).values("doctrine_fit_id")
    return list(
        DoctrineFit.objects.filter(
            Q(doctrine__status=Doctrine.Status.ACTIVE) | Q(pk__in=stocked)
        ).select_related("doctrine").order_by("doctrine__name", "name").distinct()
    )


def _week_index(now) -> tuple[datetime, list[date]]:
    """(start of the current ISO week as an aware datetime, None) helper base."""
    local_today = timezone.localtime(now).date()
    monday = local_today - timedelta(days=local_today.weekday())
    current_week_start = timezone.make_aware(datetime.combine(monday, time.min))
    return current_week_start, local_today


def _sample_sigma(series: list[float]) -> Decimal:
    n = len(series)
    mean = sum(series) / n
    var = sum((x - mean) ** 2 for x in series) / (n - 1)
    return Decimal(str(round(math.sqrt(var), 4)))


def _runout_days(atp: int, rate_week: Decimal, events, today: date) -> Decimal | None:
    """The day the composed demand curve crosses ATP — a projection, not a division.

    Walks ``daily × t`` plus every dated event at its date. With no events this
    reduces exactly to ``atp / daily``. Returns None when demand never exhausts
    the stock (no rate and events don't reach it) — "no demand", not infinity.
    """
    daily = rate_week / 7 if rate_week > 0 else Decimal("0")
    remaining = Decimal(atp)
    if remaining <= 0:
        return Decimal("0") if (daily > 0 or events) else None
    prev_t = Decimal("0")
    for event_day, units, _key in sorted(events, key=lambda e: e[0]):
        t_e = Decimal((event_day - today).days)
        if t_e < 0:
            t_e = Decimal("0")
        if daily > 0 and remaining <= daily * (t_e - prev_t):
            return (prev_t + remaining / daily).quantize(_Q)
        remaining -= daily * (t_e - prev_t)
        prev_t = t_e
        remaining -= units
        if remaining <= 0:
            return t_e.quantize(_Q)
    if daily > 0:
        return (prev_t + remaining / daily).quantize(_Q)
    return None


def demand_for_fits(fits, *, availability, config: DemandConfig | None = None,
                    horizon_days: int | None = None) -> dict[int, FitDemand]:
    """Composed demand for many fits in a fixed number of queries (≤7).

    ``availability`` is the dict from ``availability_for_fits`` — demand never
    recomputes stock. Query plan (independent of len(fits)): loss buckets ×1,
    ingest-depth probe ×1, upcoming op slots ×1, open demand lines ×1, readiness
    configs ×1, recent outbound ledger ×1, and the config singleton unless passed
    in (two reads on the first-ever create-on-miss).

    IMPORTANT (allocation grain): pass the full planning universe (or at least
    every fit sharing a hull with the ones you care about) — untagged hull losses
    are allocated across the fits *present in the call*, so a single-fit call
    hands that fit 100% of its hull's untagged losses.
    """
    from apps.killboard.models import Killmail
    from apps.operations.models import Operation, OperationShipSlot
    from apps.readiness.forecast import linear_fit
    from apps.readiness.models import DoctrineReadinessConfig

    from .models import FitStockEntry

    fits = list(fits)
    if not fits:
        return {}
    config = config or DemandConfig.active()
    horizon = max(1, int(horizon_days or config.horizon_days))  # 0 would divide by zero
    z = Z_SCORES.get(config.service_level, Z_SCORES[ServiceLevel.P90])
    now = timezone.now()
    current_week_start, today = _week_index(now)
    horizon_end = today + timedelta(days=horizon)
    n_weeks = int(config.history_weeks)
    window_start = current_week_start - timedelta(weeks=n_weeks)

    fit_ids = {f.id for f in fits}
    hull_fits: dict[int, list] = {}
    for f in fits:
        if f.ship_type_id:  # a fit can carry a falsy hull — guard like _required_per_set
            hull_fits.setdefault(f.ship_type_id, []).append(f)

    # --- Query 1: weekly loss buckets over the trailing window (missing weeks
    # are zero-filled in Python — the aggregate only returns weeks that had
    # losses, and forgetting the zeros understates variance catastrophically).
    week_dates = [
        (current_week_start - timedelta(weeks=n_weeks - i)).date() for i in range(n_weeks)
    ]
    week_pos = {d: i for i, d in enumerate(week_dates)}
    tagged: dict[int, list[float]] = {f.id: [0.0] * n_weeks for f in fits}
    untagged_hull: dict[int, list[float]] = {}
    rows = (
        Killmail.objects.filter(
            involves_home_corp=True,
            home_corp_role=Killmail.HomeRole.VICTIM,
            killmail_time__gte=window_start,
            killmail_time__lt=current_week_start,
        )
        .annotate(week=TruncWeek("killmail_time"))
        .values("week", "doctrine_fit_id", "victim_ship_type_id")
        .annotate(n=Count("killmail_id"))
    )
    for r in rows:
        pos = week_pos.get(timezone.localtime(r["week"]).date())
        if pos is None:
            continue
        fid = r["doctrine_fit_id"]
        if fid in fit_ids:
            tagged[fid][pos] += r["n"]
        elif fid is None and r["victim_ship_type_id"] in hull_fits:
            untagged_hull.setdefault(
                r["victim_ship_type_id"], [0.0] * n_weeks
            )[pos] += r["n"]

    # --- Query 2: ingest-depth probe. Weeks before killboard ingest began are
    # NOT quiet weeks — zero-filling them would fabricate calm history — so the
    # observed window starts at the oldest home-corp victim killmail ever seen
    # (capped at the trailing window). A genuinely quiet week inside that span
    # still zero-fills, exactly as the variance math requires.
    oldest = Killmail.objects.filter(
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
    ).aggregate(m=models_Min("killmail_time"))["m"]
    if oldest is None:
        weeks_observed = 0
    else:
        # ceil: a loss anywhere inside the oldest bucket makes that whole week observed.
        seconds_back = (current_week_start - timezone.localtime(oldest)).total_seconds()
        depth = math.ceil(max(0.0, seconds_back) / (7 * 86400))
        weeks_observed = max(0, min(n_weeks, depth))

    # Untagged allocation shares: proportional to each fit's tagged total on the
    # hull; even split when nothing is tagged.
    share: dict[int, float] = {}
    if config.include_untagged_losses:
        for _hull, hfits in hull_fits.items():
            totals = {f.id: sum(tagged[f.id]) for f in hfits}
            denom = sum(totals.values())
            for f in hfits:
                share[f.id] = (totals[f.id] / denom) if denom else 1.0 / len(hfits)

    # --- Query 3: upcoming fleet-ops slots (window on target_at, never created_at).
    ops_slots = OperationShipSlot.objects.filter(
        doctrine_fit_id__in=fit_ids,
        operation__status__in=(Operation.Status.PLANNED, Operation.Status.ACTIVE),
        operation__target_at__gte=now,
        operation__target_at__lte=now + timedelta(days=horizon),
    )
    if not config.include_recurring_ops:
        ops_slots = ops_slots.filter(operation__recurring_template__isnull=True)
    ops_by_fit: dict[int, list[tuple[date, Decimal]]] = {}
    for r in ops_slots.values("doctrine_fit_id", "min_pilots", "operation__target_at"):
        units = Decimal(r["min_pilots"] or 0) * Decimal(config.op_attrition_pct) / 100
        if units > 0:
            ops_by_fit.setdefault(r["doctrine_fit_id"], []).append(
                (timezone.localtime(r["operation__target_at"]).date(), units)
            )

    # --- Query 4: open manual lines (undated pins to the horizon end; a line
    # dated beyond the horizon waits until the horizon reaches it).
    lines_by_fit: dict[int, list[tuple[date, Decimal, int]]] = {}
    open_lines = DemandLine.objects.filter(
        fit_id__in=fit_ids, status=DemandLine.Status.OPEN
    ).values("fit_id", "quantity", "needed_by")
    for r in open_lines:
        when = r["needed_by"] or horizon_end
        if when <= horizon_end:
            lines_by_fit.setdefault(r["fit_id"], []).append(
                (when, Decimal(r["quantity"]), r["quantity"])
            )

    # --- Query 5: doctrine lifecycle signals (separate OneToOne, not selected_related
    # by the console universe).
    readiness = {
        r["doctrine_id"]: r
        for r in DoctrineReadinessConfig.objects.filter(
            doctrine_id__in={f.doctrine_id for f in fits}
        ).values("doctrine_id", "is_upcoming", "retirement_date")
    }

    # --- Query 6: fits with outbound stock movement inside the slow-mover window.
    outbound_cutoff = now - timedelta(days=int(config.slow_mover_days))
    recently_consumed = set(
        FitStockEntry.objects.filter(
            stock__doctrine_fit_id__in=fit_ids,
            kind=FitStockEntry.Kind.CONSUMED,
            created_at__gte=outbound_cutoff,
        ).values_list("stock__doctrine_fit_id", flat=True).distinct()
    )

    horizon_weeks = Decimal(horizon) / 7
    out: dict[int, FitDemand] = {}
    for f in fits:
        a = availability[f.id]
        offer = a.offer
        safety = offer.safety_stock if offer else 0
        target = offer.target_stock if offer else None

        # Observed loss series (tagged + allocated untagged), trimmed to the weeks
        # the corp actually has data for — zero-filling before ingest began would
        # fabricate quiet history and crush σ.
        u_series = untagged_hull.get(f.ship_type_id or 0, [0.0] * n_weeks)
        s_share = share.get(f.id, 0.0)
        series = [
            tagged[f.id][i] + s_share * u_series[i] for i in range(n_weeks)
        ][n_weeks - weeks_observed:] if weeks_observed else []

        loss_mean = Decimal(str(round(sum(series) / len(series), 4))) if series else Decimal("0")
        sigma = _sample_sigma(series) if len(series) >= MIN_WEEKS else None

        trend = ""
        if len(series) >= MIN_WEEKS:
            fitted = linear_fit([(float(i), v) for i, v in enumerate(series)])
            if fitted is not None:
                slope = fitted[0]
                trend = "rising" if slope > _TREND_EPS else ("falling" if slope < -_TREND_EPS else "")

        events: list[tuple[date, Decimal, str]] = []
        for when, units in ops_by_fit.get(f.id, ()):
            events.append((when, units, "ops"))
        for when, units, _qty in lines_by_fit.get(f.id, ()):
            events.append((when, units, "manual"))

        gap = max(0, (target or 0) - (a.atp + a.incoming)) if target else 0
        gap_rate = (Decimal(gap) / horizon_weeks).quantize(_Q) if gap else Decimal("0")

        rate_mean = (loss_mean + gap_rate).quantize(_Q)
        rate_hi = (rate_mean + z * sigma).quantize(_Q) if sigma is not None else rate_mean

        sources: list[DemandSource] = []
        tagged_total = sum(tagged[f.id][n_weeks - weeks_observed:]) if weeks_observed else 0
        if tagged_total:
            sources.append(DemandSource(
                key="loss_tagged",
                rate_week=Decimal(str(round(tagged_total / max(1, weeks_observed), 2))),
                units=Decimal("0"),
                detail={"losses": int(tagged_total), "weeks": weeks_observed},
            ))
        untagged_alloc = (
            s_share * sum(u_series[n_weeks - weeks_observed:]) if weeks_observed else 0.0
        )
        if untagged_alloc:
            sources.append(DemandSource(
                key="loss_untagged",
                rate_week=Decimal(str(round(untagged_alloc / max(1, weeks_observed), 2))),
                units=Decimal("0"),
                detail={"share": round(s_share, 3),
                        "hull_losses": int(sum(u_series[n_weeks - weeks_observed:]))
                        if weeks_observed else 0},
            ))
        ops_units = sum((u for _d, u, k in events if k == "ops"), Decimal("0"))
        if ops_units:
            sources.append(DemandSource(
                key="ops", rate_week=Decimal("0"), units=ops_units,
                detail={"ops": len(ops_by_fit.get(f.id, ())),
                        "attrition_pct": config.op_attrition_pct,
                        "recurring_included": config.include_recurring_ops},
            ))
        if gap:
            sources.append(DemandSource(
                key="target_gap", rate_week=gap_rate, units=Decimal(gap),
                detail={"target": target, "atp": a.atp, "incoming": a.incoming},
            ))
        manual_units = sum((u for _d, u, k in events if k == "manual"), Decimal("0"))
        if manual_units:
            sources.append(DemandSource(
                key="manual", rate_week=Decimal("0"), units=manual_units,
                detail={"lines": len(lines_by_fit.get(f.id, ()))},
            ))

        # (s, S) suggestion — S strictly above s (a review-week of demand, min 1)
        # so replenishing to S always clears the strict `atp < s` trigger.
        has_demand = rate_mean > 0 or bool(events)
        event_total = sum((u for _d, u, _k in events), Decimal("0"))
        if has_demand:
            daily = rate_mean / 7
            lead = Decimal(a.lead_days)
            z_term = z * sigma * Decimal(str(round(math.sqrt(a.lead_days / 7.0), 4))) \
                if sigma is not None else Decimal("0")
            s_point = math.ceil(daily * lead + z_term) + safety
            order_up_to = max(target or 0, s_point + max(1, math.ceil(rate_mean)))
            qty = max(0, order_up_to + math.ceil(event_total) - (a.atp + a.incoming))
        else:
            s_point = None
            order_up_to = None
            qty = 0

        flags: list[str] = []
        if weeks_observed < MIN_WEEKS:
            flags.append("no_history")
        r_cfg = readiness.get(f.doctrine_id) or {}
        retired = f.doctrine.status == "retired" or (
            r_cfg.get("retirement_date") and r_cfg["retirement_date"] < today
        )
        if retired and a.on_hand > 0:
            flags.append("obsolete")
        if r_cfg.get("is_upcoming"):
            flags.append("upcoming")
        if (
            rate_mean == 0 and not events and a.on_hand > 0
            and f.id not in recently_consumed
        ):
            flags.append("slow_mover")

        out[f.id] = FitDemand(
            fit_id=f.id,
            rate_week_mean=rate_mean,
            rate_week_hi=rate_hi,
            sigma_week=sigma,
            weeks_observed=weeks_observed,
            buckets=[round(v, 2) for v in series],
            sources=sources,
            events=sorted(events, key=lambda e: e[0]),
            days_cover=_runout_days(a.atp, rate_mean, events, today),
            days_cover_lo=_runout_days(a.atp, rate_hi, events, today),
            suggested_reorder=s_point,
            order_up_to=order_up_to,
            suggested_order_qty=qty,
            flags=flags,
            trend=trend,
        )
    return out
