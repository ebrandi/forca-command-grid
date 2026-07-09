"""Shared pricing helper used by valuation, industry, SRP, store and market.

Price resolution order (best market signal first):

1. The requested profile's live Jita aggregate (``sell_min`` — populated by
   ``price_types`` from Fuzzwork / by the ESI order sync).
2. CCP's daily **adjusted price** (profile ``ADJUSTED``, from ``/markets/prices/``)
   as a reference fallback for anything not currently on the Jita market.
3. ``0`` when we have no price signal at all.

We deliberately do **not** fall back to ``SdeType.base_price``: that is an internal
SDE bookkeeping figure, not a market value, and for whole item classes it is wrong
by orders of magnitude (e.g. blueprint base prices are tens of billions, ammo/ore
base prices are 100–10,000× their real price). Using it inflated every consumer —
killmail valuation, SRP payouts and industry build costs alike. A known ``0`` for an
unpriced type is safer (and visibly "unknown") than a fabricated number.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from decimal import Decimal

from .models import MarketPrice

# --------------------------------------------------------------------------- #
# Process-local price snapshot
#
# ``price_for`` is called per-material, per-node by the industry BOM/calculator/
# chain and (thousands of times) by the doctrine supply aggregation. Querying the
# DB on every call is an N+1 that made those pages take tens of seconds. Market
# prices only refresh once a day (Celery ``market.sync_*``), so we cache the whole
# JITA-sell + CCP-adjusted price map in-process for a few minutes: after the first
# call in a TTL window every ``price_for`` is an O(1) dict lookup, zero queries.
# Mirrors the process-local name memo in apps/sde/templatetags/eve.py.
# --------------------------------------------------------------------------- #
_SNAP_TTL = 300.0  # seconds; well under the daily price-refresh cadence
_SNAP_LOCK = threading.Lock()
_SNAP: dict = {"at": 0.0, "jita": None, "adjusted": None, "meta": None}


def _snapshot() -> dict:
    now = time.monotonic()
    snap = _SNAP
    if snap["jita"] is not None and (now - snap["at"]) < _SNAP_TTL:
        return snap
    with _SNAP_LOCK:
        if _SNAP["jita"] is not None and (time.monotonic() - _SNAP["at"]) < _SNAP_TTL:
            return _SNAP
        jita: dict[int, Decimal] = {}
        # ``meta`` mirrors each type's JITA_SELL buy price + freshness for the
        # planetary ``PriceProvider``; folding it into this pass keeps the whole
        # snapshot a single JITA_SELL scan. Dropping the ``sell_min`` filter here is
        # harmless — null-``sell_min`` rows are simply skipped when building ``jita``,
        # so ``jita`` is byte-for-byte what the filtered query produced.
        meta: dict[int, tuple[Decimal | None, object, bool]] = {}
        for type_id, sell, buy_max, as_of in (
            MarketPrice.objects.filter(profile=MarketPrice.Profile.JITA_SELL)
            .order_by("-as_of")
            .values_list("type_id", "sell_min", "buy_max", "as_of")
        ):
            meta.setdefault(type_id, (buy_max, as_of, True))
            if sell is not None:
                jita.setdefault(type_id, Decimal(sell))
        adjusted: dict[int, Decimal] = {}
        for type_id, price in (
            MarketPrice.objects.filter(
                profile=MarketPrice.Profile.ADJUSTED, adjusted_price__isnull=False
            )
            .order_by("-as_of")
            .values_list("type_id", "adjusted_price")
        ):
            adjusted.setdefault(type_id, Decimal(price))
        _SNAP.update(at=time.monotonic(), jita=jita, adjusted=adjusted, meta=meta)
        return _SNAP


def reset_price_cache() -> None:
    """Drop the in-process price snapshot (tests call this between cases)."""
    _SNAP.update(at=0.0, jita=None, adjusted=None, meta=None)


def price_maps() -> dict:
    """Cached JITA-sell / CCP-adjusted / JITA-meta price maps (read-only).

    Returns the same process-local TTL snapshot that backs :func:`price_for` (and
    is reset by :func:`reset_price_cache`). ``jita`` and ``adjusted`` map
    ``type_id -> Decimal`` (latest ``as_of`` first); ``meta`` maps
    ``type_id -> (buy_max, as_of, True)`` from the JITA_SELL rows. Callers must
    treat the returned dicts as shared, read-only state — copy before mutating.
    """
    return _snapshot()


def price_for(type_id: int, profile: str = MarketPrice.Profile.JITA_SELL) -> Decimal:
    """Best available market price for a type; ``0`` if we have no signal.

    Resolution order is unchanged (live Jita sell → CCP adjusted → 0); the two
    common profiles are served from the cached snapshot.
    """
    snap = _snapshot()
    if profile == MarketPrice.Profile.JITA_SELL:
        value = snap["jita"].get(type_id)
        if value is None:
            value = snap["adjusted"].get(type_id)
        return value if value is not None else Decimal("0")
    if profile == MarketPrice.Profile.ADJUSTED:
        value = snap["adjusted"].get(type_id)
        return value if value is not None else Decimal("0")

    # Uncommon profile (e.g. Jita buy): keep the original per-call resolution.
    price = (
        MarketPrice.objects.filter(type_id=type_id, profile=profile).order_by("-as_of").first()
    )
    if price and price.sell_min is not None:
        return Decimal(price.sell_min)
    adjusted = snap["adjusted"].get(type_id)
    return adjusted if adjusted is not None else Decimal("0")


def build_price_index() -> Callable[[int], Decimal]:
    """A drop-in replacement for ``price_for`` backed by an in-memory snapshot.

    Loads every current price once and returns a lookup with the same resolution
    order (live Jita sell → CCP adjusted → 0). For batch jobs that price millions
    of items (e.g. re-valuing the whole killboard) this turns one query-per-item
    into a single pair of queries, without changing the result.
    """
    jita: dict[int, Decimal] = {}
    for type_id, sell in (
        MarketPrice.objects.filter(
            profile=MarketPrice.Profile.JITA_SELL, sell_min__isnull=False
        )
        .order_by("-as_of")
        .values_list("type_id", "sell_min")
    ):
        jita.setdefault(type_id, Decimal(sell))  # first seen == latest as_of

    adjusted: dict[int, Decimal] = {}
    for type_id, price in (
        MarketPrice.objects.filter(
            profile=MarketPrice.Profile.ADJUSTED, adjusted_price__isnull=False
        )
        .order_by("-as_of")
        .values_list("type_id", "adjusted_price")
    ):
        adjusted.setdefault(type_id, Decimal(price))

    def lookup(type_id: int) -> Decimal:
        value = jita.get(type_id)
        if value is None:
            value = adjusted.get(type_id)
        return value if value is not None else Decimal("0")

    return lookup
