"""Logistics dimension (the hauling half of the v1 ``_stock_and_logistics`` pass).

A backlog heuristic: full marks with no open hauling tasks, decaying as the queue
grows. Shares the ``stock_and_logistics`` computation with the stock provider via
the context memo (so it runs once). Emits no gaps of its own in Phase 0, exactly
as v1 did.
"""
from __future__ import annotations

from ..engine.base import DimensionResult, ReadinessContext, status_for
from ..engine.registry import register
from .sources import get_stock_logistics


class LogisticsProvider:
    key = "logistics"
    label = "Logistics Throughput"
    default_weight = 1.0
    data_sources = ["Hauling tasks"]

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        dims, _gaps = get_stock_logistics(ctx)  # shared, no recompute
        score = dims["logistics"]
        return DimensionResult(
            key=self.key, score=score, status=status_for(score),
            default_weight=self.default_weight,
        )


register(LogisticsProvider())
