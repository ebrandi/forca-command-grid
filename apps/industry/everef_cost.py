"""Full manufacturing cost from EVE Ref's industry-cost API.

Our local ``bom.build_cost`` is one-level direct materials at Jita with **no job
install fee** — it makes building look cheaper than it is. EVE Ref's
``api.everef.net/v1/industry/cost`` computes the real job: ME-adjusted material
quantities, market-priced materials, and the install fee (EIV × system cost index +
SCC surcharge). We use it for the Supply Forecast's build path.

Robustness: results are cached, and a tripped circuit breaker makes us skip the
network for a minute after any timeout/error so a slow API never stalls a page —
callers fall back to the local estimate on a ``None`` return.
"""
from __future__ import annotations

from decimal import Decimal

import requests
from django.conf import settings
from django.core.cache import cache

_API = "https://api.everef.net/v1/industry/cost"
_TTL = 12 * 3600          # build cost moves slowly; cache half a day
_TIMEOUT = 6
_HEALTH_KEY = "everef:indcost:down"
_DEFAULT_ME = 10


def _api_down() -> bool:
    return cache.get(_HEALTH_KEY) is not None


def _trip_breaker() -> None:
    cache.set(_HEALTH_KEY, 1, 60)


def manufacturing_cost_per_unit(
    product_id: int, *, me: int = _DEFAULT_ME, runs: int = 1, system_id: int | None = None
) -> Decimal | None:
    """Per-unit manufacturing cost (materials + job fee), or ``None`` if the product
    isn't manufacturable or the API is unavailable (caller should fall back)."""
    key = f"everef:indcost:{product_id}:{me}:{runs}:{system_id or 0}"
    cached = cache.get(key)
    if cached is not None:
        return Decimal(cached) if cached else None
    if _api_down():
        return None  # breaker open — don't touch the network this minute

    # In-flight dedup: the store's order POST can now reach this on a user-driven
    # path, and N concurrent callers for the same cold key must not fan out N
    # parallel requests from our IP. One caller fetches; the rest fall back (None)
    # until the cache is warm. Atomic on Redis; best-effort elsewhere.
    guard = f"{key}:inflight"
    if not cache.add(guard, 1, _TIMEOUT + 2):
        return None

    params: dict[str, object] = {"product_id": product_id, "runs": runs, "me": me}
    if system_id:
        params["system_id"] = system_id

    value: Decimal | None = None
    try:
        resp = requests.get(
            _API, params=params, timeout=_TIMEOUT,
            headers={"User-Agent": settings.ESI_USER_AGENT},
        )
        if resp.status_code == 200:
            entry = (resp.json().get("manufacturing") or {}).get(str(product_id))
            if entry and entry.get("total_cost_per_unit") is not None:
                raw = Decimal(str(entry["total_cost_per_unit"]))
                # JSON NaN/Infinity parse quietly into Decimal, and a zero or negative
                # "cost" is as bogus — none of them may be cached or reach a caller
                # that freezes the figure into an order.
                if raw.is_finite() and raw > 0:
                    value = raw.quantize(Decimal("0.01"))
    except requests.RequestException:
        _trip_breaker()
        return None  # don't cache a transient failure as "not manufacturable"
    except (ValueError, KeyError, TypeError, ArithmeticError):
        # ArithmeticError covers decimal.InvalidOperation (e.g. a non-numeric string),
        # which is NOT a ValueError — without it a malformed payload 500s the caller.
        value = None  # malformed response → treat as not manufacturable
    finally:
        cache.delete(guard)

    # Cache both a real cost and a confirmed "not manufacturable" (empty) result.
    cache.set(key, str(value) if value is not None else "", _TTL)
    return value
