"""Fleet Composition dimension (``fleet_comp``) — design doc 06 §3.

Scores whether the roster can field the shapes a fleet needs: mean role coverage
across the strategic roles, the logistics ratio, and the FC bench — all measured as
qualified pilots vs ``StrategicRoleTarget`` headcounts via the shared ``roles``
helper. Honest score: with no role targets configured the dimension is unavailable.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from ..engine.base import (
    DimensionResult,
    Finding,
    KpiResult,
    ReadinessContext,
    combine_kpi_scores,
    status_for,
)
from ..engine.registry import register
from .roles import role_score

# Roles that make up a fleet's shape (logi/tackle/links/dictor/ewar/…) — the
# role_coverage KPI averages these; logi and fc get their own headline KPIs too.
_FLEET_ROLES = ("logi", "tackle", "dictor", "hic", "links", "recon", "ewar")


def _kpi(key, value, score, detail):
    return KpiResult(key=key, value=value, score=score, status=status_for(score), detail=detail)


class FleetCompProvider:
    key = "fleet_comp"
    label = _("Fleet Composition")
    default_weight = 1.0
    data_sources = [_("Strategic role targets"), _("Character skills")]
    kpi_catalogue = [
        ("fleet_comp.role_coverage", _("Role coverage")),
        ("fleet_comp.logi_ratio", _("Logi ratio")),
        ("fleet_comp.fc_bench", _("FC bench")),
    ]

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        from apps.readiness.models import StrategicRoleTarget

        targets = list(StrategicRoleTarget.objects.filter(active=True))
        by_role = {t.role_key: t for t in targets}
        kpis: list[KpiResult] = []
        findings: list[Finding] = []

        # role_coverage — mean of min(qualified, desired)/desired across fleet roles.
        role_scores = []
        for role_key in _FLEET_ROLES:
            target = by_role.get(role_key)
            if target is None:
                continue
            qualified, score = role_score(target)
            if score is None:
                continue
            role_scores.append(score)
            if qualified < target.desired_count:
                findings.append(Finding(
                    kind="gap", dimension_key=self.key, kpi_key="fleet_comp.role_coverage",
                    severity="warn", weight=float(target.desired_count - qualified),
                    label=f"{target.label} short by {target.desired_count - qualified}",
                    ref_type="role", ref_id=role_key,
                    task_type="train", task_title=f"Train pilots into {target.label}",
                ))
        if role_scores:
            kpis.append(_kpi("fleet_comp.role_coverage", None,
                             round(sum(role_scores) / len(role_scores)),
                             {"roles_scored": len(role_scores)}))

        # logi_ratio + fc_bench — headline single-role KPIs when targeted.
        for role_key, kpi_key in (("logi", "fleet_comp.logi_ratio"), ("fc", "fleet_comp.fc_bench")):
            target = by_role.get(role_key)
            if target is None:
                continue
            qualified, score = role_score(target)
            if score is None:
                continue
            kpis.append(_kpi(kpi_key, qualified, score,
                             {"qualified": qualified, "desired": target.desired_count}))

        if not kpis:
            return DimensionResult(
                key=self.key, score=None, status="unavailable",
                default_weight=self.default_weight,
                detail={"reason": "No strategic role targets configured to grade fleet shape."},
            )

        score = combine_kpi_scores(kpis, ctx.config.get("kpis", {}))
        return DimensionResult(
            key=self.key, score=score, status=status_for(score),
            default_weight=self.default_weight, kpis=kpis, findings=findings,
            detail={"roles_targeted": len(targets)},
        )


register(FleetCompProvider())
