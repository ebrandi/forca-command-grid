"""ESI good-citizen guards: error budget (420) and token bucket (429).

State is kept in the Django cache (Redis in prod). The client records headers
from every response and consults the guard before firing non-essential calls.
See handbooks/contributor-handbook/esi-integration.md §7.
"""
from __future__ import annotations

import time

from django.core.cache import cache

_ERR_REMAIN_KEY = "esi:error_limit_remain"
_ERR_RESET_KEY = "esi:error_limit_reset_at"
_BUCKET_BLOCK_KEY = "esi:bucket_blocked_until"

# Stop firing non-essential calls when fewer than this many errors remain.
ERROR_BUDGET_FLOOR = 10


def record_response(headers: dict) -> None:
    """Update guard state from an ESI response's headers (case-insensitive)."""
    h = {k.lower(): v for k, v in headers.items()}

    remain = h.get("x-esi-error-limit-remain")
    reset = h.get("x-esi-error-limit-reset")
    if remain is not None:
        try:
            cache.set(_ERR_REMAIN_KEY, int(remain), timeout=120)
        except (TypeError, ValueError):
            pass
    if reset is not None:
        try:
            cache.set(_ERR_RESET_KEY, time.time() + int(reset), timeout=120)
        except (TypeError, ValueError):
            pass


def note_retry_after(seconds: int) -> None:
    """Record a 429 Retry-After window (token-bucket exhausted)."""
    cache.set(_BUCKET_BLOCK_KEY, time.time() + max(0, seconds), timeout=max(1, seconds) + 5)


def seconds_until_unblocked() -> float:
    """How long to wait before a non-essential call is allowed (0 = now)."""
    blocked_until = cache.get(_BUCKET_BLOCK_KEY)
    if blocked_until:
        wait = blocked_until - time.time()
        if wait > 0:
            return wait
    remain = cache.get(_ERR_REMAIN_KEY)
    if remain is not None and remain <= ERROR_BUDGET_FLOOR:
        reset_at = cache.get(_ERR_RESET_KEY)
        if reset_at:
            wait = reset_at - time.time()
            if wait > 0:
                return wait
        return 1.0
    return 0.0


def can_call(essential: bool = False) -> bool:
    """Whether a call may proceed now. Essential calls bypass the budget floor
    (but still respect a hard 429 block)."""
    blocked_until = cache.get(_BUCKET_BLOCK_KEY)
    if blocked_until and (blocked_until - time.time()) > 0:
        return False
    if essential:
        return True
    return seconds_until_unblocked() <= 0
