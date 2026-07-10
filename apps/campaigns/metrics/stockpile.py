"""``stockpile.on_hand`` — effective on-hand for a stockpile row set (doc 00 §6, doc 02 §4.5).

Reads ``apps.stockpile.services.reconcile_stockpile``, which returns per-row *effective* on-hand
(ESI when the location is covered, else the manual stocktake) plus the ``covered`` honesty flag.
That flag is surfaced verbatim in ``detail`` so an uncovered (ESI-blind) location reads honestly.
"""
from __future__ import annotations

from .base import Measurement, MetricSource, _dec, register


class StockpileOnHand(MetricSource):
    key = "stockpile.on_hand"
    label = "Stockpile — effective on-hand"
    unit = "units"
    data_class = "assets"
    params_schema = [
        {"name": "stockpile_id", "kind": "int", "label": "Stockpile id", "required": True,
         "help": "The corp stockpile to reconcile against live ESI on-hand."},
        {"name": "type_id", "kind": "int", "label": "Item type id", "required": False,
         "help": "Optional — a single item type; omitted sums every item in the stockpile."},
    ]

    def measure(self, params: dict) -> Measurement:
        from django.utils import timezone

        from apps.stockpile.models import Stockpile
        from apps.stockpile.services import reconcile_stockpile

        stockpile = Stockpile.objects.get(pk=int(params["stockpile_id"]))
        recon = reconcile_stockpile(stockpile)
        covered = recon["covered"]
        rows = recon["rows"]

        type_id = params.get("type_id")
        if type_id is not None:
            type_id = int(type_id)
            rows = [r for r in rows if r["type_id"] == type_id]

        effective = sum(r["effective"] for r in rows)
        as_of = stockpile.as_of or timezone.now()
        detail = {"covered": covered, "stockpile_id": stockpile.pk, "items": len(rows)}
        if type_id is not None:
            detail["type_id"] = type_id
        if not covered:
            detail["note"] = "ESI cannot see this location; the manual stocktake is authoritative."
        return Measurement(value=_dec(effective), as_of=as_of, detail=detail)


register(StockpileOnHand())
