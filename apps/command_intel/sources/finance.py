"""Financial intelligence source (design doc 04 §2, category "financial").

Summarises the corp wallet via ``corporation.finance_analytics.default_dashboard``
to balance, 30-day income/expense/net and an ISK runway forecast — totals only,
never journal line items or named earners (doc 04 §5).
"""
from __future__ import annotations

from ..engine.base import OK, PARTIAL, UNKNOWN, SnapshotContext, SourceSlice
from ..engine.registry import register_source
from ._util import isk, now_iso


class FinanceSource:
    key = "finance"
    label = "Corporation Finance"
    category = "financial"
    default_enabled = True

    def collect(self, ctx: SnapshotContext) -> SourceSlice:
        from apps.corporation.finance_analytics import default_dashboard

        data = ctx.cached("finance_dashboard", default_dashboard)
        balance = data.get("current_balance")
        income = data.get("income_total")
        expense = data.get("expense_total")
        if not balance and not income and not expense:
            return SourceSlice(
                key=self.key, version=1, facts={}, as_of=now_iso(),
                coverage_pct=0.0, status=UNKNOWN,
                notes=("no corp wallet history synced",),
            )

        forecast = data.get("forecast") or {}
        enough = bool(forecast.get("enough"))
        facts = {
            "balance_isk": isk(balance),
            "net_30d_isk": isk(data.get("net_total")),
            "income_30d_isk": isk(income),
            "expense_30d_isk": abs(isk(expense)),
            "runway_days": forecast.get("runway_days") if enough else None,
        }
        if enough:
            return SourceSlice(
                key=self.key, version=1, facts=facts, as_of=now_iso(),
                coverage_pct=100.0, status=OK,
            )
        return SourceSlice(
            key=self.key, version=1, facts=facts, as_of=now_iso(),
            coverage_pct=100.0, status=PARTIAL,
            notes=("insufficient history for a runway forecast",),
        )


register_source(FinanceSource())
