"""``industry.deliveries`` — built items delivered in the campaign window (doc 00 §6, doc 02 §4.6).

There is no ready-made windowed helper in ``apps.erp`` (documented absence), so the window query
lives here, not in erp: ``Delivery`` rows (``created_at`` in the campaign window) joined to their
``BuildJob.output_type_id``. The window lower bound is the campaign start injected by
``build_call_params`` (``_since``); the upper bound is the measurement instant.
"""
from __future__ import annotations

from .base import Measurement, MetricSource, _dec, register


class IndustryDeliveries(MetricSource):
    key = "industry.deliveries"
    label = "Industry — deliveries in window"
    unit = "units"
    data_class = "industry_jobs"
    params_schema = [
        {"name": "type_ids", "kind": "ints", "widget": "type_multi", "label": "Output items", "required": True,
         "help": "Built items to count deliveries of."},
    ]

    def measure(self, params: dict) -> Measurement:
        from django.db.models import Sum
        from django.utils import timezone

        from apps.erp.models import Delivery

        type_ids = params["type_ids"]
        now = params.get("_now") or timezone.now()
        since = params.get("_since")

        qs = Delivery.objects.filter(job__output_type_id__in=type_ids, created_at__lte=now)
        if since:
            qs = qs.filter(created_at__gte=since)
        total = qs.aggregate(q=Sum("quantity"))["q"] or 0
        return Measurement(
            value=_dec(total), as_of=now,
            detail={"type_ids": list(type_ids), "since": since.isoformat() if since else None},
        )


register(IndustryDeliveries())
