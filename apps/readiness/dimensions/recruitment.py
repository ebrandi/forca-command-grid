"""Recruitment dimension (``recruitment``) — design doc 06 §8.

Scores headcount and intake against ``readiness.recruitment`` targets: active
members vs target, new joins per month vs the minimum intake, and the dormant ratio
(lower is better). Pure provider over corp membership + the contribution/attendance
signals already collected. Honest score: no members ⇒ unavailable.
"""
from __future__ import annotations

import datetime as dt

from django.utils.translation import gettext_lazy as _

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


def _kpi(key, value, score, detail):
    return KpiResult(key=key, value=value, score=score, status=status_for(score), detail=detail)


class RecruitmentProvider:
    key = "recruitment"
    label = _("Recruitment")
    default_weight = 0.7
    data_sources = [_("Corp members"), _("Member join dates"), _("Activity signals")]
    kpi_catalogue = [
        ("recruitment.headcount_vs_target", _("Headcount vs target")),
        ("recruitment.intake_rate", _("Intake rate")),
        ("recruitment.dormant_ratio", _("Dormant ratio")),
    ]

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        from django.utils import timezone

        from apps.operations.models import OperationAttendance
        from apps.pilots.models import ContributionEvent
        from apps.sso.models import EveCharacter

        from .. import config as config_module

        members = list(EveCharacter.objects.filter(is_corp_member=True))
        total = len(members)
        if not total:
            return DimensionResult(
                key=self.key, score=None, status="unavailable",
                default_weight=self.default_weight,
                detail={"reason": "No corp members to measure recruitment for."},
            )

        cfg = config_module.get("recruitment")
        target = int(cfg["target_active_members"]) or 1
        min_intake = int(cfg["min_monthly_intake"]) or 1
        max_dormant = float(cfg["max_dormant_ratio"]) or 1.0
        now = timezone.now()
        since_30 = now - dt.timedelta(days=30)

        # active members (same definition as the activity dimension).
        user_ids = [m.user_id for m in members if m.user_id]
        active_users = set(
            OperationAttendance.objects.filter(user_id__in=user_ids, created_at__gte=since_30)
            .values_list("user_id", flat=True)
        ) | set(
            ContributionEvent.objects.filter(user_id__in=user_ids, occurred_at__gte=since_30)
            .values_list("user_id", flat=True)
        )
        active = sum(1 for m in members if m.user_id in active_users)
        intake = sum(1 for m in members if m.added_at and m.added_at >= since_30)
        dormant_ratio = (total - active) / total

        kpis = [
            _kpi("recruitment.headcount_vs_target", round(active / target, 2),
                 threshold_score(active, amber=target, red=target * 0.5),
                 {"active": active, "target": target}),
            _kpi("recruitment.intake_rate", intake,
                 threshold_score(intake, amber=min_intake, red=0),
                 {"intake_30d": intake, "min_monthly_intake": min_intake}),
            _kpi("recruitment.dormant_ratio", round(dormant_ratio, 2),
                 threshold_score(dormant_ratio, amber=max_dormant, red=min(1.0, max_dormant * 2),
                                 direction="lower_is_better"),
                 {"dormant_ratio": round(dormant_ratio, 2), "max_dormant_ratio": max_dormant}),
        ]

        findings = []
        if active < target:
            findings.append(Finding(
                kind="gap", dimension_key=self.key, kpi_key="recruitment.headcount_vs_target",
                severity="warn", weight=float(target - active),
                label=f"Active headcount {active} below target {target}",
                ref_type="recruitment", ref_id="headcount",
                task_type="other", task_title="Recruit to grow active headcount",
            ))

        score = combine_kpi_scores(kpis, ctx.config.get("kpis", {}))
        return DimensionResult(
            key=self.key, score=score, status=status_for(score),
            default_weight=self.default_weight, kpis=kpis, findings=findings,
            detail={"active": active, "target": target, "intake_30d": intake},
        )


register(RecruitmentProvider())
