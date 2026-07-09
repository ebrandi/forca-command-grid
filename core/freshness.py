"""Data-freshness helpers: per-class staleness thresholds + 'as of' labels."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

# Per-data-class staleness thresholds (tune against live ESI cache TTLs).
THRESHOLDS: dict[str, timedelta] = {
    "killmail": timedelta(minutes=10),
    "skills": timedelta(days=7),
    "skillqueue": timedelta(days=1),
    "affiliation": timedelta(hours=2),
    "market_price": timedelta(hours=1),
    "market_history": timedelta(hours=24),
    "assets": timedelta(hours=24),
    "industry_jobs": timedelta(hours=6),
    # Command Intelligence reuses a cached snapshot within this window before
    # rebuilding the cross-domain picture for a new report (design doc 06 §2).
    "command_intel": timedelta(minutes=15),
    "default": timedelta(hours=24),
}


def is_stale(as_of, data_class: str = "default") -> bool:
    if as_of is None:
        return True
    threshold = THRESHOLDS.get(data_class, THRESHOLDS["default"])
    return (timezone.now() - as_of) > threshold


def humanize_as_of(as_of) -> str:
    if as_of is None:
        return "unknown"
    delta = timezone.now() - as_of
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"
