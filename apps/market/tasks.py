"""Celery tasks for market price and history ingestion."""
from __future__ import annotations

import logging

from celery import shared_task

from .models import MarketLocation
from .services import (
    ingest_adjusted_prices,
    ingest_market_history,
    ingest_market_prices,
    tracked_history_type_ids,
)

log = logging.getLogger("forca.market")

THE_FORGE = 10000002  # Jita's region — the corp's price reference


@shared_task(name="market.sync_adjusted_prices")
def sync_adjusted_prices() -> int:
    """Daily refresh of CCP reference prices (the ``price_for`` fallback).

    The endpoint is public and recomputed by CCP once a day, so a daily cadence
    is the useful maximum. Keeps thinly-traded / off-market types (and historical
    killmail items) priced from a sane reference instead of falling through to 0.
    """
    count = ingest_adjusted_prices()
    log.info("adjusted price sync: %s types", count)

    from apps.admin_audit.health import record_sync

    record_sync("market_adjusted_prices", types=count, rows=count)
    return count


@shared_task(name="market.sync_prices")
def sync_market_prices(location_id: int, type_ids: list[int]) -> int:
    location = MarketLocation.objects.filter(pk=location_id).first()
    if not location:
        return 0
    return ingest_market_prices(location, type_ids)


@shared_task(name="market.sync_jita_prices", soft_time_limit=3600, time_limit=3900)
def sync_jita_prices() -> int:
    """Daily live-Jita price refresh: the authoritative signal behind ``price_for``.

    JITA_SELL rows are what store quotes, SRP payouts, industry costs and killmail
    valuation resolve to; without this beat they only refreshed on the manual
    ``price_types`` run, so between runs every ``price_for`` silently fell back to
    the once-daily CCP adjusted reference. Refreshes prices first (the critical,
    always-recorded step), resets the process price cache so readers see them, then
    re-values the killboard + recomputes BOMs best-effort so a heavy-step failure
    never loses the price refresh. Returns the number of types priced.
    """
    from apps.admin_audit.health import record_sync

    from .pricing import reset_price_cache
    from .services import refresh_jita_prices, revalue_from_prices

    priced = refresh_jita_prices()
    reset_price_cache()
    record_sync("market_jita_prices", types=priced, rows=priced)
    log.info("jita price sync: %s types", priced)
    try:
        stats = revalue_from_prices()
        log.info(
            "jita price revalue: %s killmails, %s projects",
            stats["killmails"], stats["projects"],
        )
    except Exception:  # noqa: BLE001 - prices are refreshed; revalue is best-effort
        log.exception("jita price revalue/recompute failed (prices still refreshed)")
    return priced


@shared_task(name="market.sync_history")
def sync_market_history(max_types: int = 120) -> int:
    """Daily refresh of public Jita history for the most-traded tracked types.

    The history endpoint is cached ~24h server-side, so a daily cadence is the
    useful maximum — running more often just re-fetches identical data. Public
    endpoint, so this needs no token. Per-type ESI errors are skipped inside
    the ingest — the run (and its health stamp) survives non-tradable types.
    """
    type_ids = tracked_history_type_ids(limit=max_types)
    if not type_ids:
        return 0
    stored, skipped = ingest_market_history(THE_FORGE, type_ids, days=90)
    log.info(
        "market history sync: %s types, %s day-rows, %s skipped",
        len(type_ids), stored, skipped,
    )

    from apps.admin_audit.health import record_sync

    record_sync("market_history", types=len(type_ids), rows=stored, skipped=skipped)
    return stored


@shared_task(name="market.ensure_history_fresh")
def ensure_history_fresh(max_age_hours: int = 20) -> int:
    """Catch-up guard: rerun the history sync if the last success is too old.

    The primary run is daily at 11:30 UTC; if it fails (worker down, ESI
    outage) this guard — on a 4-hour beat — retries until the stamp is fresh
    again, keeping the feed's worst-case staleness inside ~24 hours.
    """
    from datetime import timedelta

    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    from apps.admin_audit.health import _last_sync

    rec = _last_sync("market_history")
    last = parse_datetime(rec["at"]) if rec and rec.get("at") else None
    if last is not None and timezone.now() - last < timedelta(hours=max_age_hours):
        return 0
    log.warning("market history stamp is stale (last=%s) — running catch-up sync", last)
    return sync_market_history()


@shared_task(name="market.warm_dashboard")
def warm_dashboard() -> int:
    """Recompute + cache the market dashboard trade signals so no request pays for it."""
    from .services import dashboard_signals

    data = dashboard_signals(force=True)
    return len(data.get("build_ops", [])) + len(data.get("margins", []))
