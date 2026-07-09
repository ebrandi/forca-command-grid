"""Forecast findings — predict a dimension breaching its red band (design doc 05 §3.3).

A transparent least-squares linear fit over recent ``ReadinessSnapshot`` history: if a
dimension is trending down and projected to cross its red threshold within the forecast
window, emit a ``kind="forecast"`` finding with ``predicted_breach_at``. Graceful cold
start: nothing is emitted with fewer than ``_MIN_POINTS`` snapshots (no false alarms on
thin history). Pure-Python fit (``linear_fit``/``project_breach``) is unit-tested in
isolation; the DB-touching ``forecast_findings`` upserts into the same register.
"""
from __future__ import annotations

import datetime as dt

_MIN_POINTS = 5           # below this, history is too thin to project honestly
_DEFAULT_WINDOW_DAYS = 14
_HISTORY = 40             # most-recent snapshots to fit over


def linear_fit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Ordinary least-squares ``(slope, intercept)`` for ``[(x, y), …]``, or None.

    Returns None when there are too few points or x has no spread (vertical fit).
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


def project_breach(points, red: float, window_days: float):
    """Days-until the fitted trend crosses ``red`` (declining), within the window.

    Returns the float days-from-the-last-point until breach, or None if the trend
    isn't declining toward red, already breached, or breaches only beyond the window.
    ``points`` are ``(day_index, score)`` with day_index increasing.
    """
    fit = linear_fit(points)
    if fit is None:
        return None
    slope, intercept = fit
    if slope >= 0:
        return None  # flat or improving — no breach
    last_x = points[-1][0]
    last_y = slope * last_x + intercept
    if last_y <= red:
        return None  # already at/below red — that's a current finding, not a forecast
    # x where y == red: red = slope*x + intercept → x = (red - intercept)/slope
    x_breach = (red - intercept) / slope
    days = x_breach - last_x
    if days <= 0 or days > window_days:
        return None
    return days


def forecast_findings(now=None) -> int:
    """Upsert forecast findings from each enabled dimension's snapshot trend.

    Returns the number of forecast findings active after the run. Resolves any prior
    forecast whose trend no longer projects a breach (same lifecycle as gaps).
    """
    from django.utils import timezone

    from . import config as config_module
    from .models import ReadinessFinding, ReadinessSnapshot

    now = now or timezone.now()
    snaps = list(ReadinessSnapshot.objects.order_by("-created_at")[:_HISTORY])
    snaps.reverse()
    if len(snaps) < _MIN_POINTS:
        return 0

    dims_cfg = config_module.get("dimensions")
    scoring = config_module.get("scoring")
    window = float(scoring.get("default_forecast_window_days", _DEFAULT_WINDOW_DAYS))

    base = snaps[0].created_at
    seen_keys: set[tuple] = set()
    active = 0
    for key, entry in dims_cfg.items():
        if not entry.get("enabled", True):
            continue
        red = float((entry.get("thresholds") or {}).get("red", 40))
        points = []
        for s in snaps:
            score = (s.dimensions or {}).get(key)
            if isinstance(score, int):
                days = (s.created_at - base).total_seconds() / 86400.0
                points.append((days, float(score)))
        if len(points) < _MIN_POINTS:
            continue
        days_to_breach = project_breach(points, red, window)
        dedupe = (key, "forecast", "forecast", key)
        if days_to_breach is None:
            continue
        seen_keys.add(dedupe)
        breach_at = now + dt.timedelta(days=days_to_breach)
        label = (
            f"{entry.get('label', key) if isinstance(entry, dict) else key} "
            f"trending toward its red band in ~{round(days_to_breach)}d"
        )
        ReadinessFinding.objects.update_or_create(
            dimension_key=key, kpi_key="forecast", ref_type="forecast", ref_id=key,
            defaults={
                "kind": ReadinessFinding.Kind.FORECAST,
                "severity": ReadinessFinding.Severity.WARN,
                "title": f"Forecast: {key} may breach red in ~{round(days_to_breach)}d",
                "detail": label, "weight": round(window - days_to_breach + 1, 1),
                "predicted_breach_at": breach_at, "last_seen": now,
                "status": ReadinessFinding.Status.OPEN,
            },
        )
        active += 1

    # Resolve stale forecasts (trend recovered → no longer projecting a breach).
    for f in ReadinessFinding.objects.filter(
        kind=ReadinessFinding.Kind.FORECAST,
        status__in=[ReadinessFinding.Status.OPEN, ReadinessFinding.Status.ACKNOWLEDGED],
    ):
        if (f.dimension_key, f.kpi_key, f.ref_type, f.ref_id) not in seen_keys:
            f.status = ReadinessFinding.Status.RESOLVED
            f.save(update_fields=["status"])
    return active
