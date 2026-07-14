"""Leadership Capacity dimension (``leadership``) — design doc 06 §11.

Scores the corp's command depth: how many officer responsibilities are actually
filled (from ``readiness.responsibilities``), and the FC / mentor bench against
``StrategicRoleTarget`` headcount targets. Officer coverage is always measurable
from config; the bench KPIs appear only when a matching, skills-detectable role
target exists (honest score — an unmeasurable role is excluded, not zero).
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from ..engine.base import (
    DimensionResult,
    Finding,
    KpiResult,
    ReadinessContext,
    combine_kpi_scores,
    ratio_score,
    status_for,
)
from ..engine.registry import register
from .roles import role_score


def _kpi(key, value, score, detail):
    return KpiResult(key=key, value=value, score=score, status=status_for(score), detail=detail)


class LeadershipProvider:
    key = "leadership"
    label = _("Leadership Capacity")
    default_weight = 1.0
    data_sources = [_("Officer responsibilities"), _("Strategic role targets")]
    kpi_catalogue = [
        ("leadership.officer_coverage", _("Officer coverage")),
        ("leadership.fc_bench", _("FC bench")),
        ("leadership.mentor_coverage", _("Mentor coverage")),
    ]

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        from apps.readiness.models import StrategicRoleTarget

        from .. import config as config_module

        responsibilities = config_module.get("responsibilities")
        owner_tags = responsibilities.get("owner_tags") or {}
        if not owner_tags:
            return DimensionResult(
                key=self.key, score=None, status="unavailable",
                default_weight=self.default_weight,
                detail={"reason": "No officer responsibilities defined to grade."},
            )

        kpis: list[KpiResult] = []
        findings: list[Finding] = []

        # officer_coverage — share of defined owner tags that have at least one user.
        filled = sum(1 for t in owner_tags.values() if (t.get("users") or []))
        defined = len(owner_tags)
        kpis.append(_kpi(
            "leadership.officer_coverage", round(filled / defined, 2),
            ratio_score(filled, defined), {"filled": filled, "defined": defined},
        ))
        if filled < defined:
            unfilled = [t.get("label", k) for k, t in owner_tags.items() if not (t.get("users") or [])]
            findings.append(Finding(
                kind="gap", dimension_key=self.key, kpi_key="leadership.officer_coverage",
                severity="warn", weight=float(defined - filled),
                label=f"{defined - filled} officer role(s) unfilled: {', '.join(unfilled[:3])}",
                ref_type="leadership", ref_id="officer_coverage",
                task_type="other", task_title="Assign owners to unfilled officer responsibilities",
            ))

        # fc_bench / mentor_coverage — only when a skills-detectable target exists.
        for role_key, kpi_key in (("fc", "leadership.fc_bench"), ("mentor", "leadership.mentor_coverage")):
            target = StrategicRoleTarget.objects.filter(role_key=role_key, active=True).first()
            if target is None:
                continue
            qualified, score = role_score(target)
            if score is None:
                continue
            kpis.append(_kpi(
                kpi_key, qualified, score,
                {"qualified": qualified, "desired": target.desired_count},
            ))
            if qualified < target.desired_count:
                findings.append(Finding(
                    kind="gap", dimension_key=self.key, kpi_key=kpi_key,
                    severity="warn", weight=float(target.desired_count - qualified),
                    label=f"{target.label} bench {qualified}/{target.desired_count}",
                    ref_type="role", ref_id=role_key,
                    task_type="train", task_title=f"Grow the {target.label} bench",
                ))

        score = combine_kpi_scores(kpis, ctx.config.get("kpis", {}))
        return DimensionResult(
            key=self.key, score=score, status=status_for(score),
            default_weight=self.default_weight, kpis=kpis, findings=findings,
            detail={"filled_officers": filled, "defined_officers": defined},
        )


register(LeadershipProvider())
