"""Point-in-time historical pricing + multi-oracle routing (KB-35).

Two capabilities live here, both in ``apps.market`` so the killboard *consumes*
pricing without owning it:

1. **Historical price** — :func:`price_at` resolves a per-type price *as of* a
   date from the local :class:`~apps.market.models.MarketHistory` time series (EVE
   Ref's daily Jita/The-Forge history). On a miss it fetches and caches the whole
   day's Forge rows once (so one download prices an entire killmail), tolerates
   absent days by stepping back to the nearest available day within a tolerance
   window, and only then falls back to the live price — never to a silent zero.

   Why ``MarketHistory`` and not ``MarketPrice``: ``MarketPrice`` is a *latest*
   store (``unique_together = (type_id, location, profile)`` — one row per type),
   so it cannot hold a time series. ``MarketHistory`` is exactly a per-type,
   per-day series for The Forge, already populated by the same EVE Ref day-files
   via ``apps.market.everef_history`` and the ``import_everef_market_history``
   command. The day's volume-weighted ``average`` is also inherently
   manipulation-resistant, which is the fair basis for historical valuation.

2. **Oracle routing** — :func:`oracle_price` decides which market signal to trust
   for a type's *live* price: the default Jita sell (``price_for``), Fuzzwork's
   percentile aggregate for high-value items (resists a thin manipulated sell
   wall), and Janice for PLEX / skill injectors when a key is configured. Every
   result carries a short ``source`` label so a valuation is auditable (SRP
   disputes read the per-item breakdown).

All external fetches are bounded (timeout + a couple of retries), cached, and log
loudly on persistent failure; a type that cannot be priced keeps the live price
with a fallback label rather than silently reading as zero.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone

from .everef_history import THE_FORGE, day_url, iter_region_rows
from .models import MarketHistory
from .pricing import price_for

log = logging.getLogger("forca.market")

# --- source labels (short, stored on Killmail.value_source; ≤24 chars) --------
SOURCE_LIVE = "live"                    # priced from the current live market
SOURCE_HISTORY = "everef_history"       # priced from EVE Ref market history at/near the date
SOURCE_LIVE_FALLBACK = "live_fallback"  # history unavailable → live price used instead
SOURCE_FUZZWORK = "fuzzwork_pct"        # high-value item → Fuzzwork percentile
SOURCE_JANICE = "janice"                # PLEX / injector → Janice split
SOURCE_UNPRICED = "unpriced"            # no signal anywhere → 0
SOURCE_MIXED = "mixed"                  # a killmail whose items span several bases


@dataclass(frozen=True)
class PriceResult:
    amount: Decimal
    source: str


# Jita 4-4 (Caldari Navy Assembly Plant) — the station Fuzzwork/Janice quote against.
JITA_4_4_STATION_ID = 60003760
FUZZWORK_AGGREGATES = "https://market.fuzzwork.co.uk/aggregates/"
JANICE_APPRAISAL_URL = "https://janice.e-351.com/api/rest/v2/pricer"

# The PLEX / skill-trading item class Janice prices best (documented routing map). PLEX and
# the skill injectors/extractors trade on their own thin, spiky markets where the daily
# average and a single Jita sell wall both mislead; Janice's split is the accurate signal.
DEFAULT_JANICE_TYPE_IDS = (
    44992,   # PLEX
    40520,   # Large Skill Injector
    45635,   # Small Skill Injector
    40519,   # Skill Extractor
)


def _tolerance_days() -> int:
    return int(getattr(settings, "MARKET_HISTORY_TOLERANCE_DAYS", 7))


def _fetch_enabled() -> bool:
    return bool(getattr(settings, "MARKET_HISTORY_FETCH_ENABLED", True))


def _http_timeout() -> float:
    return float(getattr(settings, "MARKET_ORACLE_HTTP_TIMEOUT_S", 10.0))


def _as_date(on) -> dt.date:
    return on.date() if isinstance(on, dt.datetime) else on


def _dec(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d > 0 else None


# --------------------------------------------------------------------------- #
#  Historical price (read the local MarketHistory series; fetch a day on miss)
# --------------------------------------------------------------------------- #
def history_price_at(type_id: int, on) -> Decimal | None:
    """The Forge daily ``average`` for ``type_id`` on the nearest day ≤ ``on``.

    Bounded to ``MARKET_HISTORY_TOLERANCE_DAYS`` before ``on`` so a long gap (an
    untraded item, an early-EVE sparse period) reads as "no signal" rather than a
    stale price from months earlier. Read-only — no network.
    """
    on = _as_date(on)
    floor = on - dt.timedelta(days=_tolerance_days())
    row = (
        MarketHistory.objects.filter(
            type_id=type_id, region_id=THE_FORGE,
            date__lte=on, date__gte=floor, average__isnull=False,
        )
        .order_by("-date")
        .values_list("average", flat=True)
        .first()
    )
    return _dec(row)


# A per-process guard so a killmail with 40 items on the same day triggers ONE day-file
# fetch, and a genuinely-absent day (404) is not re-requested for the rest of the process.
_DAY_LOCK = threading.Lock()
_DAY_ATTEMPTED: set[dt.date] = set()


def reset_history_fetch_memo() -> None:
    """Forget which day-files were already fetched (tests call this between cases)."""
    with _DAY_LOCK:
        _DAY_ATTEMPTED.clear()


def _fetch_day_into_history(on: dt.date) -> int:
    """Download EVE Ref's market-history day-file for ``on`` and cache every Forge row.

    Caches the WHOLE day (all Forge types, not just tracked ones) so one download
    prices an entire killmail and every later kill on that day. Idempotent upsert
    into :class:`MarketHistory`. Never raises: a network hiccup or an absent day
    logs and returns 0, and the caller falls back to the live price.
    """
    import requests

    from core.mixins import Source
    from core.netcap import download_to_buffer

    url = day_url(on)
    headers = {"User-Agent": settings.ESI_USER_AGENT}
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=_http_timeout(), stream=True)
        except requests.RequestException as exc:  # noqa: PERF203 - bounded retry loop
            last_exc = exc
            time.sleep(0.5 * (attempt + 1))
            continue
        if resp.status_code == 404:
            log.info("market history day %s not published yet (404)", on)
            return 0  # day genuinely absent — caller steps back to the nearest available
        if resp.status_code != 200:
            last_exc = RuntimeError(f"HTTP {resp.status_code}")
            time.sleep(0.5 * (attempt + 1))
            continue
        try:
            blob = download_to_buffer(resp, chunk=131072)
            now = timezone.now()
            objs = []
            for r in iter_region_rows(blob):  # type_ids=None → the whole Forge day
                try:
                    d = dt.datetime.strptime(r["date"], "%Y-%m-%d").date()
                except (TypeError, ValueError):
                    continue
                objs.append(MarketHistory(
                    type_id=r["type_id"], region_id=THE_FORGE, date=d,
                    average=_dec(r["average"]), highest=_dec(r["highest"]),
                    lowest=_dec(r["lowest"]), volume=r["volume"],
                    order_count=r["order_count"], source=Source.EVEREF, as_of=now,
                ))
            if objs:
                MarketHistory.objects.bulk_create(
                    objs, update_conflicts=True,
                    unique_fields=["type_id", "region_id", "date"],
                    update_fields=["average", "highest", "lowest", "volume",
                                   "order_count", "source", "as_of"],
                    batch_size=2000,
                )
            return len(objs)
        except Exception as exc:  # noqa: BLE001 - parsing/DB issue: log, don't break valuation
            last_exc = exc
            break
    log.warning("market history fetch for %s failed: %s", on, last_exc)
    return 0


def price_at(type_id: int, on, *, fetch: bool | None = None) -> PriceResult:
    """Price ``type_id`` as of ``on`` (a date or aware datetime).

    Resolution: (1) local MarketHistory ≤ ``on`` within tolerance; (2) on a miss,
    fetch+cache the day's Forge history once and re-query (exact day, then nearest
    within tolerance); (3) fall back to the live price, labelled ``live_fallback``
    (or ``unpriced`` when there is no live signal either). ``fetch=False`` skips
    the network step (used by hot paths that must not block on a download).
    """
    on_date = _as_date(on)
    hit = history_price_at(type_id, on_date)
    if hit is not None:
        return PriceResult(hit, SOURCE_HISTORY)

    if fetch is None:
        fetch = _fetch_enabled()
    if fetch:
        with _DAY_LOCK:
            attempt = on_date not in _DAY_ATTEMPTED
            _DAY_ATTEMPTED.add(on_date)
        if attempt:
            _fetch_day_into_history(on_date)
            hit = history_price_at(type_id, on_date)
            if hit is not None:
                return PriceResult(hit, SOURCE_HISTORY)

    live = price_for(type_id)
    return PriceResult(live, SOURCE_LIVE_FALLBACK if live > 0 else SOURCE_UNPRICED)


# --------------------------------------------------------------------------- #
#  Multi-oracle routing for LIVE prices
# --------------------------------------------------------------------------- #
def _janice_type_ids() -> set[int]:
    return {int(t) for t in getattr(settings, "JANICE_TYPE_IDS", DEFAULT_JANICE_TYPE_IDS)}


def _janice_enabled() -> bool:
    return bool(getattr(settings, "JANICE_API_KEY", ""))


def _fuzzwork_enabled() -> bool:
    return bool(getattr(settings, "MARKET_ORACLE_FUZZWORK_ENABLED", False))


def _fuzzwork_threshold() -> Decimal:
    return Decimal(str(getattr(settings, "MARKET_ORACLE_FUZZWORK_THRESHOLD_ISK", 1_000_000_000)))


# Small per-process TTL cache for external oracle quotes: routing only fires for the few
# high-value / PLEX types on a board, but a backfill re-prices them across many mails, so
# caching keeps each type to one external call per TTL window.
_ORACLE_TTL = 300.0
_ORACLE_LOCK = threading.Lock()
_ORACLE_CACHE: dict[tuple[str, int], tuple[float, Decimal | None]] = {}


def reset_oracle_cache() -> None:
    with _ORACLE_LOCK:
        _ORACLE_CACHE.clear()


def _cached(provider: str, type_id: int, fetch: Callable[[], Decimal | None]) -> Decimal | None:
    key = (provider, type_id)
    now = time.monotonic()
    with _ORACLE_LOCK:
        entry = _ORACLE_CACHE.get(key)
        if entry is not None and (now - entry[0]) < _ORACLE_TTL:
            return entry[1]
    value = fetch()
    with _ORACLE_LOCK:
        _ORACLE_CACHE[key] = (time.monotonic(), value)
    return value


def fuzzwork_percentile(type_id: int) -> Decimal | None:
    """Jita 4-4 sell **percentile** from Fuzzwork — the manipulation-resistant signal.

    The percentile aggregates ~5% of order volume, so a single lowball/highball wall
    can't move it the way ``sell.min`` can. Returns ``None`` (caller falls back) on any
    error — never raises into a valuation.
    """
    def _fetch() -> Decimal | None:
        import requests
        try:
            resp = requests.get(
                FUZZWORK_AGGREGATES,
                params={"station": JITA_4_4_STATION_ID, "types": str(type_id)},
                headers={"User-Agent": settings.ESI_USER_AGENT},
                timeout=_http_timeout(),
            )
            resp.raise_for_status()
            agg = (resp.json() or {}).get(str(type_id)) or {}
            return _dec((agg.get("sell") or {}).get("percentile"))
        except Exception as exc:  # noqa: BLE001 - external oracle: degrade, don't crash
            log.warning("fuzzwork percentile fetch failed for type %s: %s", type_id, exc)
            return None

    return _cached("fuzzwork", type_id, _fetch)


def janice_price(type_id: int) -> Decimal | None:
    """Best Jita price for ``type_id`` from Janice (PLEX / injectors), or ``None``.

    Optional — only used when ``JANICE_API_KEY`` is configured. No key ⇒ this is never
    reached (the router skips Janice), so we never hard-depend on a keyed service.
    """
    def _fetch() -> Decimal | None:
        import requests
        try:
            resp = requests.post(
                JANICE_APPRAISAL_URL,
                params={"market": 2, "designation": 100, "pricing": 200},  # Jita, appraisal, split
                headers={
                    "X-ApiKey": settings.JANICE_API_KEY,
                    "Content-Type": "text/plain",
                    "Accept": "application/json",
                    "User-Agent": settings.ESI_USER_AGENT,
                },
                data=str(type_id),
                timeout=_http_timeout(),
            )
            resp.raise_for_status()
            data = resp.json() or []
            row = data[0] if isinstance(data, list) and data else data
            prices = (row or {}).get("itemType", {}).get("immediatePrices") or (row or {}).get(
                "immediatePrices", {}
            )
            return _dec(prices.get("sellPrice") or prices.get("splitPrice"))
        except Exception as exc:  # noqa: BLE001 - external oracle: degrade, don't crash
            log.warning("janice fetch failed for type %s: %s", type_id, exc)
            return None

    return _cached("janice", type_id, _fetch)


def oracle_price(type_id: int, *, live: Decimal | None = None) -> PriceResult:
    """Resolve a type's live price through the routing map, with a source label.

    Order: Janice for the configured PLEX/injector class (best accuracy there),
    then Fuzzwork percentile for high-value types (resists manipulation), else the
    default Jita ``price_for``. Any oracle miss degrades to Jita — a labelled real
    number, never a silent zero.
    """
    base = price_for(type_id) if live is None else live

    if _janice_enabled() and type_id in _janice_type_ids():
        amt = janice_price(type_id)
        if amt is not None:
            return PriceResult(amt, SOURCE_JANICE)

    if _fuzzwork_enabled() and base > _fuzzwork_threshold():
        amt = fuzzwork_percentile(type_id)
        if amt is not None:
            return PriceResult(amt, SOURCE_FUZZWORK)

    return PriceResult(base, SOURCE_LIVE if base > 0 else SOURCE_UNPRICED)


# --------------------------------------------------------------------------- #
#  A price lookup bound to a kill date, recording which sources it used
# --------------------------------------------------------------------------- #
class HistoricalPriceLookup:
    """A ``type_id -> Decimal`` callable that prices as of a fixed date.

    Drop-in for the ``price_lookup`` that :func:`apps.killboard.valuation.compute_value`
    accepts, so a killmail can be re-valued at period-accurate prices with the exact
    same item logic (BPCs zeroed, quantities). It routes the base historical price
    through the oracle for high-value / PLEX types and records a source tally so the
    caller can derive one representative label for the whole killmail.
    """

    def __init__(self, on, *, fetch: bool | None = None, use_oracle: bool = True):
        self.on = on
        self.fetch = fetch
        self.use_oracle = use_oracle
        self.sources: Counter[str] = Counter()
        self.type_sources: dict[int, str] = {}  # per-type label (auditable per-item panel)
        self._memo: dict[int, Decimal] = {}

    def __call__(self, type_id: int) -> Decimal:
        if type_id in self._memo:
            return self._memo[type_id]
        res = price_at(type_id, self.on, fetch=self.fetch)
        # Oracle refinement rides on the historical figure: for a high-value/PLEX type we
        # prefer the manipulation-resistant / accurate live signal over a thin day-average.
        if self.use_oracle and res.source in (SOURCE_HISTORY, SOURCE_LIVE_FALLBACK):
            oracle = oracle_price(type_id, live=res.amount)
            if oracle.source in (SOURCE_FUZZWORK, SOURCE_JANICE):
                res = oracle
        self.sources[res.source] += 1
        self.type_sources[type_id] = res.source
        self._memo[type_id] = res.amount
        return res.amount

    def dominant_source(self) -> str:
        """One label summarising the killmail: the single source if uniform, else ``mixed``.

        ``unpriced`` components (0-price items) don't define the basis on their own, so
        they're ignored unless nothing else priced.
        """
        meaningful = {s: n for s, n in self.sources.items() if s != SOURCE_UNPRICED}
        if not meaningful:
            return SOURCE_UNPRICED if self.sources else SOURCE_LIVE
        if len(meaningful) == 1:
            return next(iter(meaningful))
        return SOURCE_MIXED
