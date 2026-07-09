"""SRP solvency intelligence source (design doc 04 §2, category "financial").

The three numbers that answer "can we keep paying SRP": open ship-replacement
liability (``srp.services.exposure``), ISK actually paid this calendar month
(``srp.services.spent_for_period``) and the current period's allocation
(``srp.SrpBudget``). Totals only (doc 04 §5).
"""
from __future__ import annotations

from ..engine.base import OK, PARTIAL, UNKNOWN, SnapshotContext, SourceSlice
from ..engine.registry import register_source
from ._util import isk, now_iso


class SrpSource:
    key = "srp"
    label = "SRP Solvency"
    category = "financial"
    default_enabled = True

    def collect(self, ctx: SnapshotContext) -> SourceSlice:
        from django.utils import timezone

        from apps.srp.models import SrpBudget
        from apps.srp.services import exposure, spent_for_period

        period = timezone.now().strftime("%Y-%m")
        liability = exposure()
        spent = spent_for_period(period)
        budget = (
            SrpBudget.objects.filter(period=period)
            .values_list("allocated", flat=True)
            .first()
        )

        if not liability and not spent and budget is None:
            return SourceSlice(
                key=self.key, version=1, facts={}, as_of=now_iso(),
                coverage_pct=0.0, status=UNKNOWN,
                notes=("no SRP activity or budget for the current period",),
            )

        facts = {
            "open_liability_isk": isk(liability),
            "spent_period_isk": isk(spent),
            "budget_isk": isk(budget) if budget is not None else None,
        }
        if budget is not None:
            return SourceSlice(
                key=self.key, version=1, facts=facts, as_of=now_iso(),
                coverage_pct=100.0, status=OK,
            )
        return SourceSlice(
            key=self.key, version=1, facts=facts, as_of=now_iso(),
            coverage_pct=100.0, status=PARTIAL,
            notes=(f"no SRP budget set for {period}",),
        )


register_source(SrpSource())
