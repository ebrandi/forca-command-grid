"""Infrastructure intelligence source (design doc 04 §2, category "infrastructure").

Surfaces imminent structure/sov risk: low-fuel + reinforced corp structures
(``corporation.CorpStructure`` — ``fuel_days_left``/``is_low_fuel``/``is_reinforced``)
and soft-sov systems (``operations.SovStructure.is_soft``). Structure names are
corp-internal asset labels, not pilot PII.
"""
from __future__ import annotations

from ..engine.base import OK, UNKNOWN, SnapshotContext, SourceSlice
from ..engine.registry import register_source
from ._util import now_iso, pct

# Surface a structure once it drops under a week of fuel — the planning horizon for
# a resupply run (a touch wider than the model's own three-day "refuel now" flag).
_LOW_FUEL_DAYS = 7


class InfrastructureSource:
    key = "infrastructure"
    label = "Structures & Sovereignty"
    category = "infrastructure"
    default_enabled = True

    def collect(self, ctx: SnapshotContext) -> SourceSlice:
        from apps.corporation.models import CorpStructure
        from apps.operations.models import SovStructure

        structures = list(CorpStructure.objects.all())
        sov = list(SovStructure.objects.all())
        if not structures and not sov:
            return SourceSlice(
                key=self.key, version=1, facts={}, as_of=now_iso(),
                coverage_pct=0.0, status=UNKNOWN,
                notes=("no structures or sovereignty tracked",),
            )

        low_fuel = sorted(
            (
                {"name": s.name or f"Structure {s.structure_id}",
                 "fuel_days_left": pct(s.fuel_days_left)}
                for s in structures
                if s.fuel_days_left is not None and s.fuel_days_left < _LOW_FUEL_DAYS
            ),
            key=lambda r: r["fuel_days_left"],
        )
        facts = {
            "low_fuel_structures": low_fuel,
            "reinforced": sum(1 for s in structures if s.is_reinforced),
            "soft_sov": sum(1 for s in sov if s.is_soft),
        }
        return SourceSlice(
            key=self.key, version=1, facts=facts, as_of=now_iso(),
            coverage_pct=100.0, status=OK,
        )


register_source(InfrastructureSource())
