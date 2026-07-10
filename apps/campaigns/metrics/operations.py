"""Operations metric sources (doc 00 §6, doc 02 §4.1).

``operations.completed`` counts DONE ops of a type in the campaign window; ``operations.attendance``
counts confirmed PAP rows across the campaign's linked operations. Both are plain ORM windows over
``apps.operations`` — operations stays unaware of campaigns (the join lives on the campaigns side).
The documented caveat: ``Operation`` DONE is a manual officer action, so un-marked ops never count.
"""
from __future__ import annotations

from .base import Measurement, MetricSource, _dec, register


def _op_type_choices():
    """Operation type options, resolved lazily so the model import stays out of package load."""
    from apps.operations.models import Operation

    return Operation.Type.choices


class OperationsCompleted(MetricSource):
    key = "operations.completed"
    label = "Operations — completed of type"
    unit = "ops"
    data_class = "default"
    params_schema = [
        {"name": "op_type", "kind": "choice", "label": "Operation type", "required": True,
         "choices": _op_type_choices,
         "help": "Counts DONE operations of this type since the campaign started."},
    ]

    def measure(self, params: dict) -> Measurement:
        from django.utils import timezone

        from apps.operations.models import Operation

        op_type = str(params["op_type"])
        now = params.get("_now") or timezone.now()
        since = params.get("_since")

        qs = Operation.objects.filter(type=op_type, status=Operation.Status.DONE, target_at__lte=now)
        if since:
            qs = qs.filter(target_at__gte=since)
        count = qs.count()
        return Measurement(
            value=_dec(count), as_of=now,
            detail={"op_type": op_type,
                    "caveat": "Operation DONE is set manually; un-marked ops are not counted."},
        )


class OperationsAttendance(MetricSource):
    key = "operations.attendance"
    label = "Operations — confirmed attendance"
    unit = "pilots"
    data_class = "default"
    params_schema = []

    def measure(self, params: dict) -> Measurement:
        from django.utils import timezone

        from apps.operations.models import OperationAttendance

        op_ids = params.get("_operation_ids") or []
        now = params.get("_now") or timezone.now()
        count = (
            OperationAttendance.objects.filter(confirmed=True, operation_id__in=op_ids).count()
            if op_ids else 0
        )
        return Measurement(value=_dec(count), as_of=now, detail={"linked_operations": len(op_ids)})


register(OperationsCompleted())
register(OperationsAttendance())
