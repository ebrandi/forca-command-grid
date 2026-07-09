"""Market services: price ingestion, local stock, seeding opportunities."""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from django.utils import timezone

from core.esi.client import ESIClient, ESIError, ESIRateLimited
from core.mixins import Source

from .models import MarketHistory, MarketLocation, MarketOrderSnapshot, MarketPrice

log = logging.getLogger("forca.market")


def update_price_from_orders(
    location: MarketLocation, type_id: int, orders: list[dict]
) -> MarketPrice | None:
    """Derive sell-min / buy-max / sell-volume for a type from raw ESI orders.

    With no orders we skip the upsert rather than persist a misleading
    zero-volume / null-price row (absence of data is not a price of zero).
    """
    if not orders:
        return None
    sells = [o for o in orders if not o.get("is_buy_order")]
    buys = [o for o in orders if o.get("is_buy_order")]
    sell_min = min((Decimal(str(o["price"])) for o in sells), default=None)
    buy_max = max((Decimal(str(o["price"])) for o in buys), default=None)
    sell_volume = sum(o.get("volume_remain", 0) for o in sells)

    now = timezone.now()
    price, _ = MarketPrice.objects.update_or_create(
        type_id=type_id,
        location=location,
        profile=MarketPrice.Profile.JITA_SELL,
        defaults={
            "sell_min": sell_min,
            "buy_max": buy_max,
            "volume": sell_volume,
            "source": Source.ESI_CHAR,
            "as_of": now,
            "fetched_at": now,
        },
    )
    # Replace this type's order snapshots at the location.
    MarketOrderSnapshot.objects.filter(type_id=type_id, location=location).delete()
    MarketOrderSnapshot.objects.bulk_create(
        [
            MarketOrderSnapshot(
                type_id=type_id,
                location=location,
                is_buy=o.get("is_buy_order", False),
                price=Decimal(str(o["price"])),
                volume_remain=o.get("volume_remain", 0),
                source=Source.ESI_CHAR,
                as_of=now,
            )
            for o in orders
        ]
    )
    return price


def ingest_market_prices(
    location: MarketLocation, type_ids: list[int], client: ESIClient | None = None
) -> int:
    """Fetch public region orders for the given types and store prices."""
    if not location.region_id:
        return 0
    client = client or ESIClient()
    count = 0
    for type_id in type_ids:
        # Paginate: liquid hubs exceed one page, and buy_max often lives on a
        # later page — a single page would compute wrong prices.
        orders = client.get_paged(
            f"/markets/{location.region_id}/orders/",
            params={"type_id": type_id, "order_type": "all"},
        )
        update_price_from_orders(location, type_id, orders)
        count += 1
    return count


def ingest_adjusted_prices(client: ESIClient | None = None) -> int:
    """Refresh CCP's daily reference prices from the public ``/markets/prices/``.

    One unauthenticated call returns ``adjusted_price`` / ``average_price`` for
    (almost) every type in the game. We store these as ``ADJUSTED``-profile rows
    (no location — they're global CCP figures), and ``price_for`` uses them as the
    reference fallback when a type has no live Jita aggregate. This is what lets us
    drop the bogus ``base_price`` fallback (see apps/market/pricing.py).

    Returns the number of types upserted. Done as a bulk split-upsert because the
    payload is tens of thousands of rows.
    """
    client = client or ESIClient()
    resp = client.get("/markets/prices/", use_etag=False)
    rows = resp.data or []
    now = timezone.now()

    existing = dict(
        MarketPrice.objects.filter(
            profile=MarketPrice.Profile.ADJUSTED, location__isnull=True
        ).values_list("type_id", "id")
    )
    to_create: list[MarketPrice] = []
    to_update: list[MarketPrice] = []
    for row in rows:
        type_id = row.get("type_id")
        if not type_id:
            continue
        adjusted = _dec(row.get("adjusted_price"))
        average = _dec(row.get("average_price"))
        if adjusted is None and average is None:
            continue
        pk = existing.get(type_id)
        obj = MarketPrice(
            id=pk,
            type_id=type_id,
            location=None,
            profile=MarketPrice.Profile.ADJUSTED,
            adjusted_price=adjusted,
            average_price=average,
            source=Source.ESI_CHAR,
            as_of=now,
            fetched_at=now,
        )
        (to_update if pk else to_create).append(obj)

    if to_create:
        MarketPrice.objects.bulk_create(to_create, batch_size=1000)
    if to_update:
        MarketPrice.objects.bulk_update(
            to_update,
            ["adjusted_price", "average_price", "source", "as_of", "fetched_at"],
            batch_size=1000,
        )
    return len(to_create) + len(to_update)


def local_sell_volume(type_id: int, location: MarketLocation) -> int:
    return sum(
        o.volume_remain
        for o in MarketOrderSnapshot.objects.filter(
            type_id=type_id, location=location, is_buy=False
        )
    )


def seeding_deficit(type_id: int, location: MarketLocation, target: int) -> int:
    """How many units short of the desired on-market quantity at a location."""
    return max(0, target - local_sell_volume(type_id, location))


def seeding_opportunities(location: MarketLocation, targets: dict[int, int]) -> list[dict]:
    """Items under their seeding target at a location (a supplier opportunity)."""
    out: list[dict] = []
    for type_id, target in targets.items():
        deficit = seeding_deficit(type_id, location, target)
        if deficit > 0:
            out.append({"type_id": type_id, "target": target, "deficit": deficit})
    return out


def tracked_history_type_ids(limit: int = 120) -> list[int]:
    """The types worth syncing daily history for, most market-relevant first.

    The adjusted-price import fills MarketPrice with (almost) every type in the
    game — including types ESI refuses history for ("Type not tradable on
    market!") — so "all MarketPrice type_ids" stopped being a sane work list.
    Rank real Jita-priced types by sell-side depth instead; volume nulls sort
    last so the selection degrades to id order rather than crashing.
    """
    from django.db.models import F

    return list(
        MarketPrice.objects.filter(profile=MarketPrice.Profile.JITA_SELL, sell_min__gt=0)
        .order_by(F("volume").desc(nulls_last=True), "type_id")
        .values_list("type_id", flat=True)
        .distinct()[:limit]
    )


def ingest_market_history(
    region_id: int, type_ids: list[int], days: int = 90, client: ESIClient | None = None
) -> tuple[int, int]:
    """Fetch and store public daily market history for types in a region.

    ``/markets/{region}/history/`` is a PUBLIC endpoint — no token required —
    so this works without any director/CEO grant. Keeps only the most recent
    ``days`` rows per type. Returns ``(stored_rows, skipped_types)``.

    A per-type ESI error (a non-tradable type, a transient 5xx after retries)
    skips THAT type and continues — one bad type must never kill the whole run
    (that exact failure mode silently starved history for a week once). Rate
    limiting is the exception: it aborts the run instead of hammering ESI;
    the catch-up beat retries later.
    """
    client = client or ESIClient()
    stored = 0
    skipped = 0
    for type_id in type_ids:
        try:
            resp = client.get(
                f"/markets/{region_id}/history/", params={"type_id": type_id}, use_etag=False
            )
        except ESIRateLimited:
            raise
        except ESIError as exc:
            log.warning("market history skipped type %s: %s", type_id, exc)
            skipped += 1
            continue
        rows = (resp.data or [])[-days:]
        for row in rows:
            day = row.get("date")
            if isinstance(day, str):
                day = datetime.strptime(day, "%Y-%m-%d").date()
            MarketHistory.objects.update_or_create(
                type_id=type_id,
                region_id=region_id,
                date=day,
                defaults={
                    "average": _dec(row.get("average")),
                    "highest": _dec(row.get("highest")),
                    "lowest": _dec(row.get("lowest")),
                    "volume": row.get("volume", 0) or 0,
                    "order_count": row.get("order_count", 0) or 0,
                    "source": Source.ESI_CHAR,
                    "as_of": timezone.now(),
                },
            )
            stored += 1
    return stored, skipped


def _dec(value):
    return Decimal(str(value)) if value is not None else None


def price_trend(type_id: int, region_id: int, days: int = 30) -> dict | None:
    """Summarise recent history for a type: latest avg, change %, avg daily volume."""
    rows = list(
        MarketHistory.objects.filter(type_id=type_id, region_id=region_id).order_by("-date")[:days]
    )
    if not rows:
        return None
    rows.reverse()  # oldest -> newest
    first, last = rows[0], rows[-1]
    change = 0.0
    if first.average and last.average and first.average > 0:
        change = float((last.average - first.average) / first.average * 100)
    avg_volume = sum(r.volume for r in rows) / len(rows)
    spark = [float(r.average) for r in rows if r.average is not None]
    return {
        "type_id": type_id,
        "latest": last.average,
        "change_pct": change,
        "avg_volume": int(avg_volume),
        "days": len(rows),
        "spark": spark,
        "as_of": last.date,
    }


def price_trends(type_ids, region_id: int, days: int = 30) -> dict[int, dict]:
    """Batched ``price_trend`` for many types in a single query (avoids N+1).

    Returns ``{type_id: trend}`` only for types that have history. One indexed
    query bounded by a date window, grouped in Python — replaces one query per row.
    """
    from datetime import timedelta

    ids = list(type_ids)
    if not ids:
        return {}
    # Generous window so we always have at least ``days`` rows where they exist,
    # without scanning the whole 178k-row table.
    cutoff = (timezone.now() - timedelta(days=days * 2 + 5)).date()
    grouped: dict[int, list] = {}
    for r in (
        MarketHistory.objects.filter(type_id__in=ids, region_id=region_id, date__gte=cutoff)
        .order_by("type_id", "-date")
        .values("type_id", "date", "average", "volume")
    ):
        grouped.setdefault(r["type_id"], []).append(r)

    out: dict[int, dict] = {}
    for tid, rows in grouped.items():
        rows = rows[:days]          # newest first → keep the most recent `days`
        rows.reverse()              # oldest → newest
        first, last = rows[0], rows[-1]
        change = 0.0
        if first["average"] and last["average"] and first["average"] > 0:
            change = float((last["average"] - first["average"]) / first["average"] * 100)
        avg_volume = sum(r["volume"] for r in rows) / len(rows)
        out[tid] = {
            "type_id": tid,
            "latest": last["average"],
            "change_pct": change,
            "avg_volume": int(avg_volume),
            "days": len(rows),
            "spark": [float(r["average"]) for r in rows if r["average"] is not None],
            "as_of": last["date"],
        }
    return out


def margin_opportunities(min_margin: float = 5.0, limit: int = 25) -> list[dict]:
    """Items whose sell/buy spread leaves a worthwhile margin (a trade signal).

    Margin = (sell_min - buy_max) / sell_min. Computed from stored Jita
    aggregates; this is a spread signal, not a guaranteed profit (it ignores
    taxes, broker fees, and fill time — surfaced as such in the UI).
    """
    rows = (
        MarketPrice.objects.filter(
            profile=MarketPrice.Profile.JITA_SELL, sell_min__gt=0, buy_max__gt=0
        )
        .values("type_id", "sell_min", "buy_max", "volume")
    )
    out: list[dict] = []
    for r in rows:
        sell, buy = r["sell_min"], r["buy_max"]
        if sell and buy and sell > buy:
            spread = sell - buy
            margin = float(spread / sell * 100)
            if margin >= min_margin:
                out.append({
                    "type_id": r["type_id"], "sell_min": sell, "buy_max": buy,
                    "spread": spread, "margin": margin, "volume": r["volume"] or 0,
                })
    out.sort(key=lambda x: x["margin"], reverse=True)
    return out[:limit]


def build_opportunities(min_profit: float = 1_000_000, limit: int = 25) -> list[dict]:
    """Items cheaper to build than to buy — the industrialist's "what to make".

    For every tracked, buildable type we compare a one-level build cost (BOM ×
    prices) against the Jita sell price. A positive gap means producing it and
    selling beats buying. Build cost is one level deep and ignores job
    fees/time, so it's an opportunity signal, not a guaranteed profit.
    """
    from apps.sde.models import SdeBlueprintMaterial

    from .pricing import build_price_index

    # In-memory price lookup (two queries total) instead of 1–2 queries per item.
    price = build_price_index()
    priced = {
        tid for tid in MarketPrice.objects.filter(
            profile=MarketPrice.Profile.JITA_SELL, sell_min__gt=0
        ).values_list("type_id", flat=True)
    }
    if not priced:
        return []

    # One query for every manufacturing recipe whose product we have a price for,
    # grouped product -> [(material, qty), …]. Replaces a per-type BOM query.
    recipes: dict[int, list[tuple[int, int]]] = {}
    for product, mat, qty in (
        SdeBlueprintMaterial.objects.filter(
            activity=SdeBlueprintMaterial.MANUFACTURING, product_type_id__in=priced
        ).values_list("product_type_id", "material_type_id", "quantity")
    ):
        recipes.setdefault(product, []).append((mat, qty))

    out: list[dict] = []
    min_profit_dec = Decimal(str(min_profit))
    for product, mats in recipes.items():
        build_cost = sum((price(m) * q for m, q in mats), Decimal("0"))
        sell = price(product)
        if build_cost <= 0 or not sell:
            continue
        profit = sell - build_cost
        if profit >= min_profit_dec:
            out.append({
                "type_id": product, "build_cost": build_cost, "sell": sell,
                "profit": profit, "margin": float(profit / sell * 100),
            })
    out.sort(key=lambda x: x["profit"], reverse=True)
    return out[:limit]


# Market data only changes when the daily imports run, so the expensive trade
# signals are safe to cache for a while. Keeps the dashboard a cache read.
_SIGNALS_KEY = "market:signals:v1"
_SIGNALS_TTL = 1800  # 30 min


def dashboard_signals(*, force: bool = False) -> dict:
    """Cached margin + build opportunities for the market dashboard.

    Recomputed at most every ``_SIGNALS_TTL`` (or when ``force``); the warm beat
    task keeps it fresh so no web request ever pays the full computation.
    """
    from django.core.cache import cache

    if not force:
        cached = cache.get(_SIGNALS_KEY)
        if cached is not None:
            return cached
    data = {
        "margins": margin_opportunities(min_margin=5.0, limit=20),
        "build_ops": build_opportunities(min_profit=1_000_000, limit=20),
    }
    cache.set(_SIGNALS_KEY, data, _SIGNALS_TTL)
    return data


# --------------------------------------------------------------------------- #
# Live Jita price refresh (scheduled + `manage.py price_types`)
#
# JITA_SELL rows are the authoritative live price behind ``price_for`` (store
# quotes, SRP payouts, industry costs, killmail valuation). They are populated
# from Fuzzwork's public bulk aggregator (100 types/request over The Forge) —
# far cheaper than per-type ESI order pulls. Historically only the manual
# ``price_types`` command refreshed them, so between runs every ``price_for``
# silently fell back to CCP's daily adjusted reference; the scheduled task below
# closes that gap.
# --------------------------------------------------------------------------- #
FUZZWORK_AGGREGATES = "https://market.fuzzwork.co.uk/aggregates/"
THE_FORGE = 10000002  # Jita's region — the corp's price reference
# Planetary Industry SDE categories: 42 = raw resources (P0), 43 = commodities (P1–P4).
_PI_CATEGORY_IDS = (42, 43)


def referenced_type_ids() -> set[int]:
    """Every type id the corp's data references and therefore needs a live price for.

    Union of killmail ships/items/participants, industry project + material rows,
    hauling tasks, stockpile items and PI plan inputs, plus every published PI
    commodity (P0–P4) so the planetary planner always has a live Jita signal rather
    than only the whole-game adjusted fallback. Cross-app imports are local to keep
    this module free of import cycles (killboard/industry import market.pricing).
    """
    from apps.industry.models import IndustryProjectItem, MaterialRequirement
    from apps.killboard.models import Killmail, KillmailItem, KillmailParticipant
    from apps.sde.models import SdeType
    from apps.stockpile.models import HaulingTask, StockpileItem

    # ``.distinct()`` pushes dedup into Postgres so only the few-thousand distinct ids
    # cross the wire — the item/participant tables are millions of rows and this now
    # runs unattended as a daily beat (a full-table materialise could OOM the worker).
    ids: set[int] = set()
    ids |= set(Killmail.objects.values_list("victim_ship_type_id", flat=True).distinct())
    ids |= set(KillmailItem.objects.values_list("item_type_id", flat=True).distinct())
    ids |= set(
        KillmailParticipant.objects.exclude(ship_type_id=None)
        .values_list("ship_type_id", flat=True)
        .distinct()
    )
    ids |= set(IndustryProjectItem.objects.values_list("type_id", flat=True).distinct())
    ids |= set(MaterialRequirement.objects.values_list("type_id", flat=True).distinct())
    ids |= set(
        HaulingTask.objects.exclude(type_id=None).values_list("type_id", flat=True).distinct()
    )
    ids |= set(StockpileItem.objects.values_list("type_id", flat=True).distinct())
    ids |= set(
        SdeType.objects.filter(
            group__category_id__in=_PI_CATEGORY_IDS, published=True
        ).values_list("type_id", flat=True)
    )
    return {int(i) for i in ids if i}


def refresh_jita_prices(type_ids: list[int] | None = None) -> int:
    """Refresh live Jita sell/buy aggregates from Fuzzwork into JITA_SELL rows.

    Prices ``type_ids`` (default: every :func:`referenced_type_ids`) in 100-type
    chunks and upserts a ``JITA_SELL`` MarketPrice per type at the reference
    location. Returns the number of type rows priced. Callers that read prices in
    the same process should :func:`~apps.market.pricing.reset_price_cache` after.
    """
    import requests
    from django.conf import settings

    loc = MarketLocation.objects.filter(is_price_reference=True).first()
    if loc is None:
        # No reference location configured: prices still store (MarketPrice.location is
        # nullable and price_for/build_price_index don't filter on it), but flag the
        # misconfiguration so it isn't masked by "prices look fresh".
        log.warning("no price-reference MarketLocation; pricing JITA_SELL to null location")
    ids = sorted(type_ids if type_ids is not None else referenced_type_ids())
    if not ids:
        return 0
    headers = {"User-Agent": settings.ESI_USER_AGENT}
    priced = 0
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        resp = requests.get(
            FUZZWORK_AGGREGATES,
            params={"region": THE_FORGE, "types": ",".join(map(str, chunk))},
            headers=headers,
            timeout=90,
        )
        resp.raise_for_status()
        now = timezone.now()
        for tid, agg in resp.json().items():
            sell = agg.get("sell", {}).get("min")
            buy = agg.get("buy", {}).get("max")
            MarketPrice.objects.update_or_create(
                type_id=int(tid),
                location=loc,
                profile=MarketPrice.Profile.JITA_SELL,
                defaults={
                    "sell_min": Decimal(str(sell)) if sell else None,
                    "buy_max": Decimal(str(buy)) if buy else None,
                    "source": Source.ESI_CHAR,
                    "as_of": now,
                },
            )
            priced += 1
    return priced


def revalue_from_prices() -> dict:
    """Re-value every killmail and recompute industry BOMs off the current prices.

    Run after :func:`refresh_jita_prices` so stored killmail ISK values and project
    BOMs reflect the fresh market, then rebuild the corp/member rollups the rankings
    read. Uses a single in-memory price index (two queries) so re-valuing the whole
    killboard is not a query-per-item. Returns a small counts summary.
    """
    from apps.industry.models import IndustryProject
    from apps.industry.services import compute_project_bom
    from apps.killboard.models import Killmail
    from apps.killboard.stats import rebuild_corp_metrics, rebuild_member_metrics
    from apps.killboard.valuation import apply_valuation

    from .pricing import build_price_index, reset_price_cache

    reset_price_cache()
    price_lookup = build_price_index()
    killmails = 0
    for km in Killmail.objects.iterator(chunk_size=500):
        apply_valuation(km, price_lookup)
        killmails += 1
    projects = 0
    for project in IndustryProject.objects.all():
        compute_project_bom(project)
        projects += 1
    rebuild_corp_metrics()
    rebuild_member_metrics()
    return {"killmails": killmails, "projects": projects}
