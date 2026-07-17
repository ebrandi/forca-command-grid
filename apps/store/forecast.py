"""Supply-and-profit forecaster for corp builders and traders.

Answers "what should we stock?" by joining four signals we already compute:

* **Demand** — how often the corp loses each doctrine hull (killboard), turned into a
  weekly/monthly replacement forecast.
* **Fair sell price** — the corp store's doctrine price (Jita sell × markup).
* **Supply cost** — the cheaper of importing the fitted ship from Jita or building
  the hull locally and buying its modules.
* **Logistics** — the jump-freight cost of moving it from Jita to the staging system,
  priced off the corp's own freight rate card.

Margin × forecast = the profit of supplying it. Pure reads; writes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

# The freight-costing primitives now live in apps.logistics.costing (one authority,
# also consumed by the MRP import lane and the P6 freight pipeline). Re-exported here
# so existing ``from apps.store.forecast import PACKAGED_VOL`` callers keep working.
from apps.logistics.costing import (  # noqa: F401
    JITA_SYSTEM_ID,
    PACKAGED_VOL,
    _freight_unit,
    _staging_hops,
)

WEEKS_PER_MONTH = Decimal("4.345")
SHIP_CATEGORY_ID = 6
CAPSULE_GROUPS = {29, 1380}  # Capsule, Capsule - Genolution — pods, not worth stocking
# Price at most this many distinct hulls (busiest first) to bound the work.
_MAX_CANDIDATES = 80


@dataclass
class SupplyRow:
    ship_type_id: int
    ship_name: str
    doctrine: str
    fit_id: int
    fit_name: str
    is_doctrine: bool
    hull_class: str
    losses: int
    per_week: float          # composed weekly demand (sum over the hull's fits)
    demand_week_hi: float | None   # service-level band top (None = insufficient history)
    mixed_fits: bool         # >1 fit contributes demand → margin×demand is approximate
    demand_sources: list     # merged DemandSource lines for the breakdown
    forecast_week: int
    forecast_month: int
    jita_unit: Decimal       # fitted Jita sell (import basis, pre-freight; 0 = no reference)
    freight_unit: Decimal
    import_cost: Decimal | None   # None when there is no market reference to import at
    build_cost: Decimal | None
    supply_cost: Decimal
    method: str              # "build" | "import"
    build_source: str        # "everef" (full job cost) | "estimate" (local) | ""
    sell_unit: Decimal       # fair store price
    margin_unit: Decimal
    profit_week: Decimal
    profit_month: Decimal
    trend_pct: float | None = None   # 30-day Jita price change, if history is loaded


def recent_losses(window_days: int) -> dict[int, int]:
    """Count corp ship losses per hull over the window (killboard victims)."""
    from apps.killboard.models import Killmail

    since = timezone.now() - timedelta(days=window_days)
    rows = (
        Killmail.objects.filter(
            involves_home_corp=True,
            home_corp_role=Killmail.HomeRole.VICTIM,
            killmail_time__gte=since,
        )
        .values("victim_ship_type_id")
        .annotate(n=Count("killmail_id"))
    )
    return {r["victim_ship_type_id"]: r["n"] for r in rows}


def _home_alliance_id() -> int | None:
    corp_id = getattr(settings, "FORCA_HOME_CORP_ID", 0)
    if not corp_id:
        return None
    from apps.corporation.models import EveCorporation

    c = EveCorporation.objects.filter(corporation_id=corp_id).first()
    return getattr(c, "alliance_id", None) if c else None


def supply_forecast(*, window_days: int = 30, staging_system_id: int = 0,
                    limit: int = 50) -> dict:
    """Rank the ships the corp actually loses by the profit of supplying them.

    Demand is every ship hull lost in the window (pods excluded). A hull that matches
    a doctrine fit is valued as the full ready-to-fly fit (the store's doctrine
    price); any other lost hull is valued as a bare hull to import-and-sell.
    """
    from apps.doctrines.hulls import hull_class_for_group
    from apps.doctrines.models import Doctrine, DoctrineFit
    from apps.industry.bom import build_cost
    from apps.industry.everef_cost import manufacturing_cost_per_unit
    from apps.logistics.services import active_rate_card
    from apps.market.pricing import price_for
    from apps.sde.models import SdeType

    from .availability import availability_for_fits
    from .demand import DemandSource, demand_for_fits, planning_universe
    from .models import ShipyardPolicy
    from .pricing import price_doctrine_fit, price_hull_order
    from .services import active_config

    cfg = active_config()
    losses = recent_losses(window_days)
    if not losses:
        return _empty(window_days, staging_system_id)

    # Composed demand per fit (P2) — hull rows sum their fits' demand, which kills
    # the old one-canonical-fit collapse for the demand half. Pricing stays the
    # per-hull canonical lane below.
    universe = planning_universe()
    demand_avail = availability_for_fits(universe, policy=ShipyardPolicy.active())
    composed = demand_for_fits(universe, availability=demand_avail)
    fits_by_hull: dict[int, list] = {}
    for f in universe:
        if f.ship_type_id:
            fits_by_hull.setdefault(f.ship_type_id, []).append(f)

    # Keep only real ship hulls (category 6, no pods); busiest first, capped.
    ship_groups = {
        tid: gid
        for tid, gid in SdeType.objects.filter(
            type_id__in=list(losses), group__category_id=SHIP_CATEGORY_ID, published=True
        ).exclude(group_id__in=CAPSULE_GROUPS).values_list("type_id", "group_id")
    }
    if not ship_groups:
        return _empty(window_days, staging_system_id)
    candidates = sorted(ship_groups, key=lambda t: losses[t], reverse=True)[:_MAX_CANDIDATES]
    pkg_vols = dict(
        SdeType.objects.filter(type_id__in=candidates).values_list("type_id", "packaged_volume")
    )

    # One canonical fit per doctrine hull (prefer the main fit over a cheap alt).
    hull_fit: dict[int, DoctrineFit] = {}
    for f in (
        DoctrineFit.objects.filter(doctrine__status=Doctrine.Status.ACTIVE)
        .select_related("doctrine").order_by("is_cheap_alt", "name")
    ):
        hull_fit.setdefault(f.ship_type_id, f)

    card = active_rate_card()
    hops = _staging_hops(staging_system_id)
    period_weeks = Decimal(window_days) / 7

    rows: list[SupplyRow] = []
    for tid in candidates:
        demand = losses[tid]
        hull_class = hull_class_for_group(ship_groups[tid])
        fit = hull_fit.get(tid)
        if fit:
            priced = price_doctrine_fit(fit, cfg.doctrine_markup)
            doctrine, fit_id, fit_name, is_doctrine = fit.doctrine.name, fit.id, fit.name, True
        else:
            # The store's own hull pricer: sub-caps off Jita, capital-class hulls off
            # the estimated build cost — so "fair sell price" matches what the store
            # would actually charge (a capital with no cost source prices not-ok and
            # is skipped, exactly like the order path refuses it).
            priced = price_hull_order(tid, cfg)
            doctrine, fit_id, fit_name, is_doctrine = "—", 0, "hull only", False
        # A build-priced capital can carry a valid sell price with NO Jita/adjusted
        # reference at all (it never trades there) — keep it. Only rows with no
        # usable sell price are dropped.
        if not priced.ok or priced.unit_price <= 0:
            continue

        jita_unit = priced.unit_jita
        sell_unit = priced.unit_price
        freight = _freight_unit(card, hops, hull_class, jita_unit, packaged_vol=pkg_vols.get(tid))
        # Importing is fiction without a real market reference, so the import lane
        # only competes when one exists.
        import_cost = (jita_unit + freight).quantize(Decimal("0.01")) if jita_unit > 0 else None

        # Build path: build the hull (EVE Ref's full job cost — materials + install
        # fee; falls back to our one-level estimate), buy any modules at Jita.
        # Deliberately a separate lookup from the sell price above: the store quotes
        # off the universe-average job cost (system-independent), while the
        # build-vs-import decision costs the job at the pinned staging system.
        ev_cost = manufacturing_cost_per_unit(tid, system_id=staging_system_id or None)
        if ev_cost is not None:
            hull_build, build_source = ev_cost, "everef"
        else:
            local = build_cost(tid)
            hull_build, build_source = (Decimal(local), "estimate") if local is not None else (None, "")
        build_total = None
        if hull_build is not None:
            modules_jita = jita_unit - price_for(tid)
            build_total = (hull_build + modules_jita + freight).quantize(Decimal("0.01"))

        if build_total is not None and (import_cost is None or build_total < import_cost):
            supply_cost, method = build_total, "build"
        elif import_cost is not None:
            supply_cost, method = import_cost, "import"
        else:
            continue  # no lane can actually supply it

        # Composed demand (P2): a doctrine hull's rate is the SUM over its fits —
        # no floors, no false precision. A hull with no offered fits keeps the raw
        # window-loss rate (there is no fit-grain signal to compose).
        hull_demand = [composed[f.id] for f in fits_by_hull.get(tid, [])]
        if hull_demand:
            per_week = float(sum(d.rate_week_mean for d in hull_demand))
            has_band = any(d.has_band for d in hull_demand)
            demand_week_hi = (
                float(sum(d.rate_week_hi for d in hull_demand)) if has_band else None
            )
            contributing = [d for d in hull_demand if d.rate_week_mean > 0 or d.events]
            mixed = len(contributing) > 1
            merged: dict[str, DemandSource] = {}
            for d in hull_demand:
                for s in d.sources:
                    slot = merged.setdefault(
                        s.key, DemandSource(key=s.key, rate_week=Decimal("0"),
                                            units=Decimal("0")))
                    slot.rate_week += s.rate_week
                    slot.units += s.units
            demand_sources = list(merged.values())
        else:
            per_week = float(Decimal(demand) / period_weeks)
            demand_week_hi = None
            mixed = False
            demand_sources = []
        # Honest rounded values — one loss in 180 days no longer forecasts ≥1/week.
        fc_week = round(per_week)
        fc_month = round(per_week * float(WEEKS_PER_MONTH))
        margin = (sell_unit - supply_cost).quantize(Decimal("0.01"))

        rows.append(SupplyRow(
            ship_type_id=tid, ship_name=priced.ship_name,
            doctrine=doctrine, fit_id=fit_id, fit_name=fit_name, is_doctrine=is_doctrine,
            hull_class=hull_class, losses=demand, per_week=round(per_week, 2),
            demand_week_hi=round(demand_week_hi, 2) if demand_week_hi is not None else None,
            mixed_fits=mixed, demand_sources=demand_sources,
            forecast_week=fc_week, forecast_month=fc_month,
            jita_unit=jita_unit, freight_unit=freight, import_cost=import_cost,
            build_cost=build_total, supply_cost=supply_cost, method=method,
            build_source=build_source,
            sell_unit=sell_unit, margin_unit=margin,
            profit_week=(margin * fc_week).quantize(Decimal("0.01")),
            profit_month=(margin * fc_month).quantize(Decimal("0.01")),
        ))

    rows.sort(key=lambda r: r.profit_month, reverse=True)
    top = rows[:limit]

    # 30-day Jita price trend for the shown rows in ONE batched query (needs
    # market history loaded — see import_everef_market_history).
    from apps.market.everef_history import THE_FORGE
    from apps.market.services import price_trends
    trends = price_trends([r.ship_type_id for r in top], THE_FORGE, 30)
    for r in top:
        pt = trends.get(r.ship_type_id)
        if pt:
            r.trend_pct = round(pt["change_pct"], 1)

    return {
        "rows": top,
        "window_days": window_days,
        "hops": hops,
        "staging_system_id": staging_system_id,
        "total_profit_month": sum((r.profit_month for r in top
                                   if r.profit_month > 0), start=Decimal("0")),
    }


def _empty(window_days: int, staging_system_id: int) -> dict:
    return {"rows": [], "window_days": window_days, "hops": 0,
            "staging_system_id": staging_system_id, "total_profit_month": Decimal("0")}
