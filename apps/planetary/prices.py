"""A small, reusable market-price provider for the PI planner.

Wraps the existing market layer (``apps.market.pricing`` / ``apps.market.services``)
behind one honest interface so the planner degrades gracefully when a price is
missing and other modules can reuse it later.

Price resolution, best signal first:

1. Live **Jita sell** (``price_for`` → ``MarketPrice`` JITA_SELL ``sell_min``).
2. If a **non-Jita region** is selected and that region has market *history*, its
   latest daily **average** (region-specific, honest for regional selling).
3. CCP **adjusted** price as a last reference.
4. ``None`` — no signal. The caller shows "missing price", never a fabricated number.

Nothing here calls ESI or the network; it only reads already-synced tables.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from apps.market.pricing import price_maps

from .constants import THE_FORGE

# A price older than this is flagged stale (live Jita only refreshes on the manual
# price_types run; adjusted refreshes daily). Purely advisory — shown, not enforced.
STALE_AFTER = timedelta(days=3)


@dataclass(frozen=True)
class PricePoint:
    type_id: int
    sell: Decimal | None
    buy: Decimal | None
    source: str            # "jita_sell" | "region_history" | "adjusted" | "none"
    as_of: object | None   # datetime or None
    stale: bool

    @property
    def priced(self) -> bool:
        return self.sell is not None


class PriceProvider:
    """Batch-friendly price lookups for a chosen pricing region.

    Construct once per request/calculation. ``region_id`` selects the *selling*
    hub region; Jita (The Forge) is the default and the most reliable signal.
    """

    def __init__(self, region_id: int | None = None):
        self.region_id = int(region_id or THE_FORGE)
        # Reuse the shared, process-local price snapshot that also backs
        # ``price_for`` (built once per TTL window, reset by ``reset_price_cache``).
        # ``jita``/``adjusted`` are the same maps ``build_price_index`` builds, and
        # ``meta`` carries each type's Jita buy price + freshness — all from a single
        # cached pair of scans. This constructor used to run four full MarketPrice
        # scans on every PI page; it now runs zero on a warm cache (two on a cold one,
        # shared with ``price_for``).
        maps = price_maps()
        jita = maps["jita"]
        # CCP adjusted price ≈ the item base value the customs office (POCO) taxes on.
        adjusted = maps["adjusted"]
        self._adjusted = adjusted
        # Buy price + freshness, keyed off the JITA_SELL rows.
        self._meta = maps["meta"]

        # Same resolution order as ``build_price_index``: Jita sell → adjusted → 0.
        def _jita_index(type_id: int) -> Decimal:
            value = jita.get(type_id)
            if value is None:
                value = adjusted.get(type_id)
            return value if value is not None else Decimal("0")

        self._jita_index = _jita_index
        self._region_hist: dict[int, Decimal] | None = None  # lazy

    # -- internals --------------------------------------------------------- #
    def _load_region_history(self, type_ids: list[int]) -> dict[int, Decimal]:
        """Latest daily average per type for the selected region (non-Jita only)."""
        if self.region_id == THE_FORGE:
            return {}
        from apps.market.models import MarketHistory

        out: dict[int, Decimal] = {}
        for type_id, average in (
            MarketHistory.objects.filter(region_id=self.region_id, type_id__in=type_ids)
            .order_by("type_id", "-date")
            .values_list("type_id", "average")
        ):
            out.setdefault(type_id, average)
        return out

    def prime(self, type_ids) -> None:
        """Preload regional history for a set of types (no-op for Jita)."""
        if self.region_id != THE_FORGE and self._region_hist is None:
            self._region_hist = self._load_region_history(list(type_ids))

    # -- public API -------------------------------------------------------- #
    def point(self, type_id: int) -> PricePoint:
        type_id = int(type_id)
        buy, as_of, _ = self._meta.get(type_id, (None, None, False))
        stale = as_of is None or (timezone.now() - as_of) > STALE_AFTER

        # Regional history overrides Jita only when a non-Jita region is selected.
        if self.region_id != THE_FORGE:
            if self._region_hist is None:
                self._region_hist = self._load_region_history([type_id])
            regional = self._region_hist.get(type_id)
            if regional is not None:
                return PricePoint(type_id, Decimal(regional), buy, "region_history", as_of, stale)

        jita = self._jita_index(type_id)
        if jita and jita > 0:
            # The jita index already folds adjusted in; distinguish for honesty.
            source = "jita_sell" if type_id in self._meta else "adjusted"
            return PricePoint(type_id, Decimal(jita), buy, source, as_of, stale)
        return PricePoint(type_id, None, buy, "none", as_of, True)

    def sell(self, type_id: int) -> Decimal:
        """Best sell price, or ``Decimal('0')`` when unpriced (degraded mode)."""
        p = self.point(type_id)
        return p.sell if p.sell is not None else Decimal("0")

    def buy(self, type_id: int) -> Decimal:
        p = self.point(type_id)
        return p.buy if p.buy is not None else Decimal("0")

    def value(self, type_id: int, quantity) -> Decimal:
        return self.sell(type_id) * Decimal(str(quantity))

    def base_value(self, type_id: int) -> Decimal:
        """CCP adjusted price — the tax base the customs office uses. Falls back to
        the sell price so a customs estimate still appears when adjusted is absent."""
        adjusted = self._adjusted.get(int(type_id))
        if adjusted is not None:
            return adjusted
        return self.sell(type_id)

    def is_priced(self, type_id: int) -> bool:
        return self.point(type_id).priced

    def missing(self, type_ids) -> list[int]:
        """Which of these types have no sell signal at all."""
        return [t for t in type_ids if not self.is_priced(t)]
