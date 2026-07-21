"""Number formatting for the Tocha's Lab telemetry (WS-13 API-18).

The engine emits raw floats (a 40 000 m optimal, a 123.0 DPS). These filters render them the
way a fitting tool should: thousands-separated, trailing ``.0`` trimmed, and long ranges shown
in kilometres. Raw values are always kept in the payload/telemetry dict — this is display only,
so JavaScript (which re-renders the whole panel server-side) never depends on the formatted text.
"""
from __future__ import annotations

from django import template

register = template.Library()

_KM_THRESHOLD = 10_000.0  # ranges at/above 10 km read as km; below that, metres


def _to_float(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf → treat as unknown
        return None
    return f


def _trim(text: str) -> str:
    """Drop a trailing ``.0`` (and any redundant trailing zeros after a real decimal)."""
    if "." in text:
        # Split off any grouping-safe integer/fraction; only touch the fraction.
        head, _, frac = text.partition(".")
        frac = frac.rstrip("0")
        return head if not frac else f"{head}.{frac}"
    return text


@register.filter
def num(value, decimals=1):
    """A telemetry number: grouped thousands, up to ``decimals`` places, trailing ``.0`` trimmed.

    ``123.0`` → ``123``; ``1234.5`` → ``1,234.5``; ``11150.0`` → ``11,150``. Non-numeric → ``—``.
    Used for velocities, signature, HP, scan resolution and other bare magnitudes."""
    f = _to_float(value)
    if f is None:
        return "—"
    try:
        places = int(decimals)
    except (TypeError, ValueError):
        places = 1
    return _trim(f"{f:,.{places}f}")


@register.filter
def rangem(value):
    """A distance, rendered like a fitting tool: ``40 km`` / ``42.5 km`` at ≥10 km, else grouped
    metres (``8,500 m``). Includes the unit. ``0`` → ``0 m``; non-numeric → ``—``.

    Apply to optimal/falloff/target range and other true distances (NOT signature radius, which
    is conventionally always metres — use ``num`` there)."""
    f = _to_float(value)
    if f is None:
        return "—"
    if abs(f) >= _KM_THRESHOLD:
        return f"{_trim(f'{f / 1000.0:,.1f}')} km"
    return f"{_trim(f'{f:,.1f}')} m"
