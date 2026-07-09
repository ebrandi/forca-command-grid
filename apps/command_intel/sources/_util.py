"""Shared helpers for the intelligence-source providers (design doc 04 §1).

JSON-safety + freshness helpers used by every :class:`SourceProvider` so each
slice's ``facts`` are plain JSON (ints/floats/strings) and carry an honest ISO8601
``as_of``. Kept tiny and dependency-light; the only Django touch (``timezone``) is
imported lazily so importing a source at app-load never reaches into the ORM.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any


def now_iso() -> str:
    """Current time as an ISO8601 string — the collection ``as_of`` for live reads."""
    from django.utils import timezone

    return timezone.now().isoformat()


def isk(value: Any) -> int:
    """Coerce a Decimal/float/None ISK figure to a JSON-safe whole-ISK int."""
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return int(value)
    return int(round(float(value)))


def pct(value: Any, digits: int = 1) -> float | None:
    """Coerce a percentage/ratio figure to a JSON-safe rounded float (None-safe)."""
    if value is None:
        return None
    return round(float(value), digits)
