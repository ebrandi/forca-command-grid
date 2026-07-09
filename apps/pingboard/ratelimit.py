"""Rate limiting, cooldowns and duplicate suppression — Redis counters.

Uses the broker-backed Django cache (matches the house style: no rate-limit table;
the ``Alert``/``AlertDelivery`` rows are the durable audit). All limits are read from
the ``anti_abuse`` config domain so leadership tunes them without a deploy.
"""
from __future__ import annotations

import hashlib
import json

from . import config


def _incr(key: str, window_seconds: int) -> int:
    from django.core.cache import cache

    if cache.add(key, 1, timeout=window_seconds):
        return 1
    try:
        return cache.incr(key)
    except ValueError:  # key expired between add and incr
        cache.add(key, 1, timeout=window_seconds)
        return 1


def _current(key: str) -> int:
    from django.core.cache import cache

    return cache.get(key, 0) or 0


def try_consume_dispatch(actor_id, category: str, priority: str) -> tuple[bool, str]:
    """Check every applicable bucket; if all under limit, consume one from each.

    Returns ``(ok, reason)``. On rejection nothing is consumed. ``limit == 0`` means
    "unlimited" for that bucket.
    """
    aa = config.get("anti_abuse")
    buckets = [
        (f"pb:rl:officer:{actor_id or 0}", aa["max_per_officer_per_hour"], 3600, "per-officer hourly limit reached"),
        (f"pb:rl:cat:{category}", aa["max_per_category_per_hour"], 3600, "per-category hourly limit reached"),
    ]
    if priority in ("urgent", "emergency"):
        buckets.append(("pb:rl:urgent", aa["max_urgent_per_day"], 86400, "daily urgent-alert limit reached"))

    for key, limit, _window, label in buckets:
        if limit and _current(key) >= limit:
            return False, label
    for key, _limit, window, _label in buckets:
        _incr(key, window)
    return True, ""


def duplicate_hash(category: str, audience: dict, body: str, source_object_id: str = "") -> str:
    # source_object_id distinguishes automation/service alerts fired for different objects
    # (same rule body) so they are not falsely collapsed; manual alerts pass "" (unchanged).
    blob = json.dumps(
        {"c": category, "a": audience or {}, "b": body or "", "o": str(source_object_id or "")},
        sort_keys=True, default=str,
    ).encode()
    return hashlib.sha256(blob).hexdigest()


def is_duplicate(dedup_hash: str) -> bool:
    """True if an identical alert fired inside the duplicate window (and marks it seen)."""
    aa = config.get("anti_abuse")
    if not aa.get("suppress_duplicates"):
        return False
    from django.core.cache import cache

    key = f"pb:dup:{dedup_hash}"
    if cache.get(key):
        return True
    cache.set(key, 1, timeout=int(aa["duplicate_window_minutes"]) * 60)
    return False


def cooldown_active(category: str, audience: dict) -> bool:
    """True if this (category, audience) is within its post-send cooldown window."""
    aa = config.get("anti_abuse")
    minutes = int(aa.get("cooldown_minutes", 0))
    if minutes <= 0:
        return False
    from django.core.cache import cache

    h = hashlib.sha256(
        json.dumps({"c": category, "a": audience or {}}, sort_keys=True, default=str).encode()
    ).hexdigest()
    key = f"pb:cd:{h}"
    if cache.get(key):
        return True
    cache.set(key, 1, timeout=minutes * 60)
    return False
