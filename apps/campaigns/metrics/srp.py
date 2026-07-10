"""``srp.reserve`` — SRP solvency reserve for a period (doc 00 §6, doc 02 §4.13).

``allocated − spent_for_period − exposure`` — the exact arithmetic the SRP budget view uses
(``apps.srp.services.spent_for_period`` / ``exposure`` + ``SrpBudget.allocated``). Sensitive by
default. Campaigns never creates budgets or claims; directors adjust ``SrpBudget`` in the SRP UI.
"""
from __future__ import annotations

from decimal import Decimal

from .base import Measurement, MetricSource, _dec, register


class SrpReserve(MetricSource):
    key = "srp.reserve"
    label = "SRP — reserve (allocated − spent − exposure)"
    unit = "ISK"
    data_class = "default"
    sensitive_default = True
    params_schema = [
        {"name": "period", "kind": "str", "label": "Period (YYYY-MM)", "required": False,
         "help": "Budget period; defaults to the current month."},
    ]

    def measure(self, params: dict) -> Measurement:
        from django.utils import timezone

        from apps.srp.models import SrpBudget
        from apps.srp.services import exposure, spent_for_period

        now = params.get("_now") or timezone.now()
        period = (params.get("period") or "").strip() or now.strftime("%Y-%m")
        budget = SrpBudget.objects.filter(period=period).first()
        allocated = Decimal(budget.allocated) if budget else Decimal(0)
        reserve = allocated - spent_for_period(period) - exposure()
        return Measurement(
            value=_dec(reserve), as_of=now,
            detail={"period": period, "allocated": str(allocated)},
        )


register(SrpReserve())
