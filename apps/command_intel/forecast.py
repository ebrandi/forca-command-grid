"""Forecast findings — project a constraint toward binding (design doc 18 P6, doc 05 §6).

A transparent least-squares fit over this app's own ``OperationalConstraint`` history: if a
constraint's *headroom* is trending down and projected to cross zero (i.e. become binding)
within the forecast window, surface a forecast finding with the predicted breach timing.

Pure-Python fit reimplemented here (not imported from readiness) to keep the app
self-contained (ADR-0001); it mirrors the readiness ``forecast.py`` math exactly. Graceful
cold start: nothing is projected with fewer than ``_MIN_POINTS`` distinct snapshots, so thin
history never raises a false alarm.
"""
from __future__ import annotations

import datetime as dt

_MIN_POINTS = 4      # below this, history is too thin to project honestly
_HISTORY = 60        # most-recent constraint rows per key to fit over


def linear_fit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Ordinary least-squares ``(slope, intercept)`` for ``[(x, y), …]``, or None.

    Returns None with too few points or no spread in x (a vertical, unfittable line).
    """
    n = len(points)
    if n < 2:
        return None
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def project_zero_crossing(points: list[tuple[float, float]], window_days: float):
    """Days until the fitted headroom trend crosses zero (binding), within the window.

    ``points`` are ``(day_index, headroom)`` with day_index increasing. Returns the float
    days-from-the-last-point until breach, or None if the trend isn't declining, has already
    breached (a current finding, not a forecast), or only breaches beyond the window.
    """
    fit = linear_fit(points)
    if fit is None:
        return None
    slope, intercept = fit
    if slope >= 0:
        return None  # flat or improving — no breach
    last_x = points[-1][0]
    last_y = slope * last_x + intercept
    if last_y <= 0:
        return None  # already binding — that's a current constraint, not a forecast
    # y == 0 at x = -intercept / slope
    x_cross = -intercept / slope
    days = x_cross - last_x
    if days <= 0 or days > window_days:
        return None
    return days


def _history() -> dict[str, list]:
    """``key → [OperationalConstraint, …]`` ordered oldest→newest across snapshots."""
    from .models import OperationalConstraint

    rows = (
        OperationalConstraint.objects.filter(status="computed", headroom__isnull=False)
        .select_related("snapshot")
        .order_by("snapshot__created_at")
    )
    by_key: dict[str, list] = {}
    for c in rows:
        if c.snapshot is None or c.snapshot.created_at is None:
            continue
        by_key.setdefault(c.key, []).append(c)
    return by_key


def forecast_findings(window_days: float | None = None, now=None) -> list[dict]:
    """Project each constraint's headroom forward; emit a finding for any in-window breach.

    Returns a list of ``{key, label, category, days_to_breach, breach_at, current_headroom}``
    sorted soonest-breach first. Deterministic for a given history + window.
    """
    from django.utils import timezone

    from . import config

    now = now or timezone.now()
    if window_days is None:
        window_days = float((config.get("constraints").get("global") or {}).get("forecast_window_days", 21))

    out: list[dict] = []
    for key, series in _history().items():
        series = series[-_HISTORY:]
        if len({c.snapshot_id for c in series}) < _MIN_POINTS:
            continue
        base = series[0].snapshot.created_at
        points = [
            ((c.snapshot.created_at - base).total_seconds() / 86400.0, float(c.headroom))
            for c in series
        ]
        days = project_zero_crossing(points, window_days)
        if days is None:
            continue
        latest = series[-1]
        out.append({
            "key": key,
            "label": latest.label,
            "category": latest.category,
            "days_to_breach": round(days, 1),
            "breach_at": now + dt.timedelta(days=days),
            "current_headroom": float(latest.headroom),
            "unit": latest.unit,
        })
    out.sort(key=lambda f: f["days_to_breach"])
    return out
