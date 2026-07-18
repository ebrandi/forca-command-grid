"""KB-20 — ingest source-health / precedence summary.

A read-only view over :class:`~apps.killboard.models.IngestSourceHealth`: which killmail
feeds are healthy and how fresh the board is. The three primaries are always expected to
run; the R2Z2 killstream is an optional fallback whose row only matters when it is enabled.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import IngestSourceHealth, KillstreamState

# (source key, label, expected freshness window, is-a-primary-feed). The two periodic
# home-corp feeds run every 15 min, so >30 min without a success is "stale"; the optional
# killstream runs every minute, so its window is tighter.
SOURCES: list[tuple[str, object, timedelta, bool]] = [
    ("esi_corp", _("ESI Director feed"), timedelta(minutes=30), True),
    ("zkill_query", _("zKillboard query poll"), timedelta(minutes=30), True),
    ("killstream", _("Realtime fallback (R2Z2)"), timedelta(minutes=5), False),
]


def _verdict(row: IngestSourceHealth | None, expected: timedelta, enabled: bool) -> str:
    """``off`` (disabled fallback) / ``idle`` (never ran) / ``fresh`` / ``stale`` / ``down``.

    ``down`` = actively failing: a current failure streak and no *recent* success. This makes
    a feed that has only ever failed read as a problem rather than as "never tried" (idle).
    """
    if not enabled:
        return "off"
    if row is None:
        return "idle"
    if row.last_success_at is not None and (timezone.now() - row.last_success_at) <= expected:
        return "fresh"
    if row.consecutive_failures:
        return "down"
    if row.last_success_at is None:
        return "idle"
    return "stale"


def ingest_status() -> dict:
    """Per-source health rows + the killstream state, for the officer health panel."""
    rows = {r.source: r for r in IngestSourceHealth.objects.all()}
    killstream = KillstreamState.load()
    sources = []
    for key, label, expected, primary in SOURCES:
        row = rows.get(key)
        enabled = True if primary else killstream.enabled
        sources.append({
            "source": key,
            "label": label,
            "primary": primary,
            "enabled": enabled,
            "last_success_at": row.last_success_at if row else None,
            "last_run_at": row.last_run_at if row else None,
            "last_count": row.last_count if row else 0,
            "consecutive_failures": row.consecutive_failures if row else 0,
            "last_error": row.last_error if row else "",
            "verdict": _verdict(row, expected, enabled),
        })
    return {"sources": sources, "killstream": killstream}
