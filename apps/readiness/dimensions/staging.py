"""Asset Staging dimension (``staging``) — Gap B5.

Scores how concentrated the corp's doctrine hulls are at the staging system: the
share of member-owned doctrine hulls (the distinct ship types across all doctrine
fits) that sit in the configured staging solar system, resolved through the
personal-asset mirror's ``AssetLocation.system_id``. With **no staging system
configured the dimension is unavailable** (it ships disabled and stays disabled
until set). Honest score: ``None`` when no member owns any doctrine hull at all
(nothing to locate).
"""
from __future__ import annotations

from ..engine.base import DimensionResult, Finding, KpiResult, ReadinessContext, status_for
from ..engine.registry import register


def _kpi(key, value, score, detail) -> KpiResult:
    return KpiResult(key=key, value=value, score=score, status=status_for(score), detail=detail)


class StagingProvider:
    key = "staging"
    label = "Asset Staging"
    default_weight = 0.8
    data_sources = ["Staging system", "Personal assets", "Doctrine fits"]
    kpi_catalogue = [("staging.hulls_at_staging", "Doctrine hulls at staging")]

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        from django.db.models import Sum

        from apps.doctrines.models import DoctrineFit
        from apps.readiness.models import StagingSystem
        from apps.stockpile.models import Asset

        staging = StagingSystem.objects.filter(active=True).first()
        if staging is None:
            return DimensionResult(
                key=self.key, score=None, status="unavailable",
                default_weight=self.default_weight,
                detail={"reason": "No staging system configured."},
            )

        hull_ids = set(DoctrineFit.objects.values_list("ship_type_id", flat=True))
        char_ids = [c.character_id for c in ctx.characters]
        if not hull_ids or not char_ids:
            return DimensionResult(
                key=self.key, score=None, status="unavailable",
                default_weight=self.default_weight,
                detail={"reason": "No doctrine hulls or members to locate."},
            )

        owned = Asset.objects.filter(
            owner_type=Asset.Owner.CHARACTER, owner_id__in=char_ids, type_id__in=hull_ids)
        total = owned.aggregate(n=Sum("quantity"))["n"] or 0
        if not total:
            return DimensionResult(
                key=self.key, score=None, status="unavailable",
                default_weight=self.default_weight,
                detail={"reason": "No member-owned doctrine hulls to locate.",
                        "staging": staging.system_name or staging.system_id},
            )
        at_staging = owned.filter(
            location__system_id=staging.system_id).aggregate(n=Sum("quantity"))["n"] or 0
        score = round(100 * at_staging / total)

        kpi = _kpi("staging.hulls_at_staging", round(score / 100, 2), score,
                   {"at_staging": at_staging, "total": total,
                    "staging": staging.system_name or str(staging.system_id)})
        findings: list[Finding] = []
        if score < 50:
            findings.append(Finding(
                kind="gap", dimension_key=self.key, kpi_key="staging.hulls_at_staging",
                severity="warn", weight=float(50 - score),
                label=f"Only {score}% of doctrine hulls are at {staging.system_name or staging.system_id}",
                ref_type="staging", ref_id=str(staging.system_id),
                task_type="haul", task_title="Consolidate doctrine hulls at staging",
            ))
        return DimensionResult(
            key=self.key, score=score, status=status_for(score),
            default_weight=self.default_weight, kpis=[kpi], findings=findings,
            detail={"staging": staging.system_name or str(staging.system_id),
                    "at_staging": at_staging, "total": total},
        )


register(StagingProvider())
