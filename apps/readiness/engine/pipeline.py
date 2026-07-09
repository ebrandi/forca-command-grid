"""The readiness pipeline: run every provider, combine, assemble the payload.

Generic and isolation-safe (doc 05 §3): a provider that raises degrades only its
own dimension (``score=None status="unavailable"``, dropped from the index) and is
logged — one bad provider never fails the whole run. The returned payload preserves
the v1 ``{index, dimensions, coverage, gaps}`` shape so existing callers (the
``/readiness/`` dashboard, ``pilots`` briefing, the ``readiness.warm`` task) are
untouched. Persistence and caching are the caller's concern (keeps the engine free
of model/cache imports).
"""
from __future__ import annotations

import logging

from .base import UNAVAILABLE, DimensionResult, ReadinessContext, combine
from .registry import providers

logger = logging.getLogger(__name__)

# Cap on the number of ranked gaps surfaced, matching the v1 dashboard.
MAX_GAPS = 8


def _dimension_enabled(config: dict, key: str) -> bool:
    """Whether a dimension is enabled in config (default on).

    Phase 0 ships no config documents, so every registered dimension runs. Phase 1
    introduces ``readiness.dimensions.<key>.enabled`` honoured here with no pipeline
    change.
    """
    dims = (config or {}).get("dimensions") or {}
    entry = dims.get(key)
    if isinstance(entry, dict) and "enabled" in entry:
        return bool(entry["enabled"])
    return True


def execute(ctx: ReadinessContext, config: dict | None = None) -> tuple[list[DimensionResult], dict]:
    """Run the enabled providers (isolation-safe) and assemble the v1 payload.

    Returns ``(results, payload)`` — the raw :class:`DimensionResult` list (so callers
    can persist the emitted findings) plus the ``{index, dimensions, coverage, gaps}``
    payload. ``run`` is the payload-only convenience over this.
    """
    config = config or {}
    results: list[DimensionResult] = []
    for provider in providers():
        if not _dimension_enabled(config, provider.key):
            continue
        try:
            result = provider.compute(ctx)
        except Exception:  # provider isolation (NFR6) — degrade one dimension only
            logger.exception("readiness provider %r failed; marking unavailable", provider.key)
            result = DimensionResult(key=provider.key, score=None, status=UNAVAILABLE, computed=False)
        results.append(result)
    return results, payload_from(results, config)


def payload_from(results: list[DimensionResult], config: dict | None = None) -> dict:
    """Assemble the v1 ``{index, dimensions, coverage, gaps}`` payload from results."""
    config = config or {}
    dimensions = {r.key: r.score for r in results}
    index = combine(results, config)

    # Corp-level sample coverage is published by whichever provider measures it
    # (the doctrine/skill scan); merge so the payload carries v1's coverage dict.
    coverage: dict = {}
    for result in results:
        coverage.update(result.detail.get("corp_coverage", {}))

    # Gaps: flatten provider findings in registration order, rank by weight desc
    # (stable — ties keep provider order, as the v1 concatenation did), cap at MAX_GAPS.
    findings = [f for result in results for f in result.findings]
    gaps = sorted((f.as_gap() for f in findings), key=lambda g: -g["weight"])[:MAX_GAPS]

    # Per-KPI breakdown (doc PRD FR4): every computed KPI keyed by its (already
    # dimension-namespaced) key, so the persisted snapshot can carry KPI-level history
    # for trends/forecasts. Additive — existing callers ignore the extra key.
    kpis = {
        kpi.key: {"value": kpi.value, "score": kpi.score, "status": kpi.status}
        for result in results for kpi in result.kpis
    }

    return {
        "index": index,
        "dimensions": dimensions,
        "coverage": coverage,
        "gaps": gaps,
        "kpis": kpis,
    }


def run(ctx: ReadinessContext, config: dict | None = None) -> dict:
    """Execute the enabled providers against ``ctx`` and assemble the v1 payload."""
    return execute(ctx, config)[1]
