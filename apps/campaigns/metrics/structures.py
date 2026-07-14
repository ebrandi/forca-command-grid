"""``structures.fuel_days`` — minimum fuel-days across selected structures (doc 00 §6, doc 02 §4.10).

Reads ``CorpStructure.fuel_days_left`` (a model property derived from ``fuel_expires``) and reports
the *minimum* across the chosen structures — the binding constraint. Thresholds stay owned by
``StructureAlertConfig``; campaigns reads values, never re-defines fuel alerting. With no structure
carrying a known expiry the source raises (no honest value), so the objective keeps its last value.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from .base import Measurement, MetricSource, _dec, register


class StructuresFuelDays(MetricSource):
    key = "structures.fuel_days"
    label = _("Structures — minimum fuel days")
    unit = "days"
    data_class = "default"
    params_schema = [
        {"name": "structure_ids", "kind": "ints", "widget": "structure_multi", "required": False,
         "label": _("Structures"),
         "help": _("Leave all unchecked to use every corp structure.")},
    ]

    def measure(self, params: dict) -> Measurement:
        from django.utils import timezone

        from apps.corporation.models import CorpStructure

        ids = params.get("structure_ids")
        qs = CorpStructure.objects.all()
        if ids:
            qs = qs.filter(structure_id__in=ids)

        fuel = [(s.fuel_days_left, s.as_of) for s in qs if s.fuel_days_left is not None]
        if not fuel:
            raise ValueError("no structures with a known fuel expiry")
        min_days = min(days for days, _as_of in fuel)
        as_of = max(a for _days, a in fuel) or timezone.now()
        return Measurement(value=_dec(round(min_days, 2)), as_of=as_of, detail={"structures": len(fuel)})


register(StructuresFuelDays())
