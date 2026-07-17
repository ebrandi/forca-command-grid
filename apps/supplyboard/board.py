"""Compose the Supply Command board from every registered provider, and cache it.

The cached payload holds ALL sections (machine keys + raw params only — no locale-frozen
prose). Role filtering happens at SERVE time in the view: the view strips
``role="director"`` sections for non-directors before the template sees them. A provider
that raises yields an honest 'unavailable' stub — one broken family must not blank the
morning sweep.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone

from . import providers

log = logging.getLogger("forca.supplyboard")

_CACHE_KEY = "supplyboard:board:v1"
_CACHE_TTL = 1800  # 30 min — the default_dashboard shape


def board_data(*, refresh: bool = False) -> dict:
    """The cached board payload ``{"sections": [...], "built_at": datetime}``.

    A warm read is a single cache hit; ``refresh=True`` rebuilds and overwrites."""
    if not refresh:
        cached = cache.get(_CACHE_KEY)
        if cached is not None:
            return cached
    data = _compose()
    cache.set(_CACHE_KEY, data, _CACHE_TTL)
    return data


def _compose() -> dict:
    from apps.store.models import ShipyardPolicy
    from apps.store.views_inventory import inventory_rows

    from .models import BoardConfig

    config = BoardConfig.active()
    # Prime the shared inventory rows once so the fit-based providers (readiness,
    # discrepancies, obsolete) don't each recompute availability + demand.
    token = providers._INVENTORY_ROWS.set(inventory_rows(ShipyardPolicy.active()))
    sections = []
    try:
        for key, fn in providers.REGISTRY.items():
            try:
                sections.append(fn(config))
            except Exception:  # noqa: BLE001 — one broken family must not blank the board
                log.exception("board provider %s failed", key)
                sections.append(providers.stub_section(key))
    finally:
        providers._INVENTORY_ROWS.reset(token)
    return {"sections": sections, "built_at": timezone.now()}
