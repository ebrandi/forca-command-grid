"""Infrastructure Readiness dimension (``infrastructure``) — design doc 06 §12.

Scores the corp's standing structures from data already synced: minimum fuel days
across structures, exposure to reinforcement timers (lower is better), and mean
sovereignty ADM (reusing ``SovStructure.is_soft``). Pure provider, no new tables.
Honest score: with no corp structures the dimension is unavailable (a corp that
owns nothing isn't graded on infrastructure).
"""
from __future__ import annotations

from ..engine.base import (
    DimensionResult,
    Finding,
    KpiResult,
    ReadinessContext,
    combine_kpi_scores,
    status_for,
    threshold_score,
)
from ..engine.registry import register

# Fuel runway bands (days): 14d+ → full, 3d → red.
_FUEL_AMBER = 14
_FUEL_RED = 3
# Sov ADM bands: 6 (max) → full, the "soft" threshold 3 → red (reuses is_soft).
_ADM_AMBER = 6.0
_ADM_RED = 3.0


def _kpi(key, value, score, detail):
    return KpiResult(key=key, value=value, score=score, status=status_for(score), detail=detail)


class InfrastructureProvider:
    key = "infrastructure"
    label = "Infrastructure Readiness"
    default_weight = 0.9
    data_sources = ["Corp structures", "Sovereignty structures"]
    kpi_catalogue = [
        ("infrastructure.fuel_cover", "Fuel cover"),
        ("infrastructure.timer_exposure", "Timer exposure"),
        ("infrastructure.sov_health", "Sov health"),
    ]

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        from apps.corporation.models import CorpStructure
        from apps.operations.models import SovStructure

        structures = list(CorpStructure.objects.all())
        sov = list(SovStructure.objects.all())
        if not structures and not sov:
            return DimensionResult(
                key=self.key, score=None, status="unavailable",
                default_weight=self.default_weight,
                detail={"reason": "No corp structures or sovereignty to grade."},
            )

        kpis: list[KpiResult] = []
        findings: list[Finding] = []

        if structures:
            # Snapshot each structure's fuel days ONCE (the property recomputes against
            # the clock on every access, so re-deriving it later wouldn't re-match).
            fueled = [(s, s.fuel_days_left) for s in structures if s.fuel_days_left is not None]
            min_pair = min(fueled, key=lambda p: p[1]) if fueled else None
            if min_pair is not None:
                low, min_fuel = min_pair
                kpis.append(_kpi(
                    "infrastructure.fuel_cover", round(min_fuel, 1),
                    threshold_score(min_fuel, amber=_FUEL_AMBER, red=_FUEL_RED),
                    {"min_fuel_days": round(min_fuel, 1), "structures": len(structures)},
                ))
                if min_fuel < _FUEL_RED:
                    findings.append(Finding(
                        kind="risk", dimension_key=self.key, kpi_key="infrastructure.fuel_cover",
                        severity="high", weight=round(100 * (_FUEL_RED - min_fuel) / _FUEL_RED),
                        label=f"{low.name or 'A structure'} has {min_fuel:.1f} days of fuel left",
                        ref_type="structure", ref_id=str(low.structure_id),
                        task_type="deliver", task_title=f"Refuel {low.name or 'structure'}",
                        owner_tag="logistics_director",
                    ))

            # timer_exposure — structures currently reinforced (lower is better).
            reinforced = sum(1 for s in structures if s.is_reinforced)
            kpis.append(_kpi(
                "infrastructure.timer_exposure", reinforced,
                threshold_score(reinforced, amber=0, red=max(1, len(structures)),
                                direction="lower_is_better"),
                {"reinforced": reinforced, "structures": len(structures)},
            ))

        if sov:
            mean_adm = sum(s.adm for s in sov) / len(sov)
            kpis.append(_kpi(
                "infrastructure.sov_health", round(mean_adm, 2),
                threshold_score(mean_adm, amber=_ADM_AMBER, red=_ADM_RED),
                {"mean_adm": round(mean_adm, 2), "sov_structures": len(sov),
                 "soft": sum(1 for s in sov if s.is_soft)},
            ))

        score = combine_kpi_scores(kpis, ctx.config.get("kpis", {}))
        return DimensionResult(
            key=self.key, score=score, status=status_for(score),
            default_weight=self.default_weight, kpis=kpis, findings=findings,
            detail={"structures": len(structures), "sov": len(sov)},
        )


register(InfrastructureProvider())
