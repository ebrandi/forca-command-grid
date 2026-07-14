"""``logistics.hauled_m3`` — m³ hauled in the campaign window (doc 00 §6, doc 02 §4.9).

A windowed ``Sum("magnitude")`` over the idempotent ``ContributionEvent(kind=HAUL)`` ledger — the
same aggregation idiom the pilots service uses. Destination filtering is deferred (FUTURE: the
courier model has no indexed destination / ``delivered_at``), so v1 counts every HAUL in the window.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from .base import Measurement, MetricSource, _dec, register


class LogisticsHauledM3(MetricSource):
    key = "logistics.hauled_m3"
    label = _("Logistics — m³ hauled in window")
    unit = "m³"
    data_class = "default"
    params_schema = []

    def measure(self, params: dict) -> Measurement:
        from django.db.models import Sum
        from django.utils import timezone

        from apps.pilots.models import ContributionEvent

        now = params.get("_now") or timezone.now()
        since = params.get("_since")

        qs = ContributionEvent.objects.filter(kind=ContributionEvent.Kind.HAUL, occurred_at__lte=now)
        if since:
            qs = qs.filter(occurred_at__gte=since)
        total = qs.aggregate(m=Sum("magnitude"))["m"] or 0
        return Measurement(value=_dec(total), as_of=now,
                           detail={"since": since.isoformat() if since else None})


register(LogisticsHauledM3())
