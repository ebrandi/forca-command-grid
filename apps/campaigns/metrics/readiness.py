"""``readiness.dimension`` — a corp readiness dimension score (doc 00 §6, doc 02 §4.4).

Reads ``apps.readiness.services.compute_readiness()["dimensions"][key]`` (a 0–100 score, or
``None`` when the provider was unavailable). An unavailable dimension raises so the objective keeps
its last value — the readiness "honest score" rule carried through to the objective (doc 08 §2.1).
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from .base import Measurement, MetricSource, _dec, register


class ReadinessDimension(MetricSource):
    key = "readiness.dimension"
    label = _("Readiness — dimension score")
    unit = "score"
    data_class = "skills"
    params_schema = [
        {"name": "dimension", "kind": "str", "widget": "readiness_dimension", "label": _("Dimension"), "required": True,
         "help": _("Which readiness dimension score to track.")},
    ]

    def measure(self, params: dict) -> Measurement:
        from django.utils import timezone

        from apps.readiness.services import compute_readiness

        key = str(params["dimension"]).strip()
        dims = compute_readiness().get("dimensions") or {}
        if key not in dims:
            raise KeyError(f"unknown readiness dimension {key!r}")
        raw = dims[key]
        score = raw.get("score") if isinstance(raw, dict) else raw
        if score is None:
            raise ValueError(f"readiness dimension {key!r} is unavailable")
        return Measurement(value=_dec(score), as_of=timezone.now(), detail={"dimension": key})


register(ReadinessDimension())
