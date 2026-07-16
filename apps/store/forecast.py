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

import math
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

JITA_SYSTEM_ID = 30000142
WEEKS_PER_MONTH = Decimal("4.345")
SHIP_CATEGORY_ID = 6
CAPSULE_GROUPS = {29, 1380}  # Capsule, Capsule - Genolution — pods, not worth stocking
# Price at most this many distinct hulls (busiest first) to bound the work.
_MAX_CANDIDATES = 80

# EVE's fixed repackaged ship volumes (m³) by broad hull class — used to amortise a
# jump-freighter load across how many hulls fit in it.
PACKAGED_VOL = {
    "Frigate": 2_500, "Destroyer": 5_000, "Cruiser": 10_000, "Battlecruiser": 15_000,
    "Battleship": 50_000, "Industrial": 20_000, "Capital": 1_300_000,
    "Freighter": 1_300_000, "Other": 10_000,
}


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
    per_week: float
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


def _staging_hops(staging_system_id: int) -> int:
    """Cyno hops from Jita to the staging system at a JDC V jump-freighter range."""
    if not staging_system_id:
        return 0
    from apps.logistics.jumps import SHIPS_BY_KEY, effective_range
    from apps.logistics.routing import jf_route_facts

    rng = effective_range(SHIPS_BY_KEY["jf"]["range"], 5)
    try:
        return jf_route_facts(JITA_SYSTEM_ID, staging_system_id, rng)["jumps"]
    except Exception:  # noqa: BLE001 - no route / no coords → treat as no jump legs
        return 0


def _freight_unit(card, hops: int, hull_class: str, unit_value: Decimal,
                  *, packaged_vol: float | None = None) -> Decimal:
    """Per-hull jump-freight cost Jita→staging, off the corp's own JF rate card.

    Uses the real repackaged volume when we have it (EVE Ref reference-data), falling
    back to a per-class approximation otherwise.
    """
    if hops <= 0:
        return Decimal("0")
    from apps.logistics.models import ShipClass
    from apps.logistics.pricing import caps_for, quote

    vol = packaged_vol or PACKAGED_VOL.get(hull_class, 10_000)
    max_m3, max_coll = caps_for(card, ShipClass.JF)
    by_vol = int(max_m3 // vol) if vol else 1
    by_coll = int(max_coll // unit_value) if unit_value > 0 else by_vol
    units = max(1, min(by_vol or 1, by_coll or 1))
    q = quote(
        card, ship_class=ShipClass.JF, jumps=hops, jump_hops=hops,
        volume_m3=vol * units, collateral=float(unit_value) * units, sec_band="nullsec",
    )
    if not q.ok:
        return Decimal("0")
    return (Decimal(q.reward) / units).quantize(Decimal("0.01"))


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

    from .pricing import price_doctrine_fit, price_hull_order
    from .services import active_config

    cfg = active_config()
    losses = recent_losses(window_days)
    if not losses:
        return _empty(window_days, staging_system_id)

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

        per_week = float(Decimal(demand) / period_weeks)
        fc_week = max(1, math.ceil(per_week))
        fc_month = max(1, math.ceil(per_week * float(WEEKS_PER_MONTH)))
        margin = (sell_unit - supply_cost).quantize(Decimal("0.01"))

        rows.append(SupplyRow(
            ship_type_id=tid, ship_name=priced.ship_name,
            doctrine=doctrine, fit_id=fit_id, fit_name=fit_name, is_doctrine=is_doctrine,
            hull_class=hull_class, losses=demand, per_week=round(per_week, 2),
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

    # 30-day Jita price trend for the shown rows (cheap: only the ones we return; needs
    # market history loaded — see import_everef_market_history).
    from apps.market.everef_history import THE_FORGE
    from apps.market.services import price_trend
    for r in top:
        pt = price_trend(r.ship_type_id, THE_FORGE, 30)
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
