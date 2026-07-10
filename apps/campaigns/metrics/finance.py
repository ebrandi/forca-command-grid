"""``finance.wallet_balance`` — a corp wallet division balance (doc 00 §6, doc 02 §4.12).

Sensitive by default (``sensitive_default``): the objective form defaults ``is_sensitive`` on so
the value renders only to directors + the campaign commander (brief §5/§7). Totals only — the
detail never carries journal line items (the command_intel "aggregates, not line items" rule).
"""
from __future__ import annotations

from .base import Measurement, MetricSource, _dec, register


class FinanceWalletBalance(MetricSource):
    key = "finance.wallet_balance"
    label = "Finance — wallet division balance"
    unit = "ISK"
    data_class = "default"
    sensitive_default = True
    params_schema = [
        {"name": "division", "kind": "int", "widget": "wallet_division", "label": "Wallet division", "required": True,
         "help": "Corp wallet division to read the balance of."},
    ]

    def measure(self, params: dict) -> Measurement:
        from django.utils import timezone

        from apps.corporation.models import CorpWalletDivision

        division = int(params["division"])
        row = CorpWalletDivision.objects.get(pk=division)
        as_of = row.as_of or timezone.now()
        return Measurement(value=_dec(row.balance), as_of=as_of, detail={"division": division})


register(FinanceWalletBalance())
