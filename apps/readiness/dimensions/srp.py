"""SRP Health dimension (``srp``) — design doc 06 §6.

Scores the ship-replacement queue against leadership's ``readiness.srp`` bounds:
pending backlog, average approval wait, oldest open claim, and budget burn. Honest
score: if the corp runs no active SRP programme the dimension is *unavailable*
(excluded from the index). A KPI with no data (e.g. nothing decided yet) is also
excluded rather than scored zero.
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
    threshold_score,
)
from ..engine.registry import register


def _kpi(key, value, score, detail):
    return KpiResult(key=key, value=value, score=score, status=status_for(score), detail=detail)


class SrpProvider:
    key = "srp"
    label = _("SRP Health")
    default_weight = 0.8
    data_sources = [_("SRP claims"), _("SRP programme"), _("SRP budget"), _("SRP thresholds")]
    kpi_catalogue = [
        ("srp.pending_backlog", _("Pending backlog")),
        ("srp.avg_wait", _("Average wait")),
        ("srp.oldest_claim", _("Oldest claim")),
        ("srp.budget_health", _("Budget health")),
    ]

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        import datetime as dt

        from django.utils import timezone

        from apps.srp.models import SrpBudget, SrpClaim, SrpProgram

        from .. import config as config_module

        # Honest score: corp doesn't run SRP → not applicable, excluded from the index.
        if not SrpProgram.objects.filter(is_active=True).exists():
            return DimensionResult(
                key=self.key, score=None, status="unavailable",
                default_weight=self.default_weight,
                detail={"reason": "No active SRP programme — nothing to grade."},
            )

        cfg = config_module.get("srp")
        max_pending = int(cfg["max_pending_claims"]) or 1
        max_wait = int(cfg["max_avg_wait_hours"]) or 1
        max_age = int(cfg["max_claim_age_days"]) or 1
        now = timezone.now()

        kpis: list[KpiResult] = []
        findings: list[Finding] = []

        # pending_backlog — open claims (awaiting a decision OR awaiting payout after
        # approval — both are an unsettled obligation; doc §6 "open claim"). Lower is
        # better.
        open_statuses = [SrpClaim.Status.SUBMITTED, SrpClaim.Status.APPROVED]
        pending = list(SrpClaim.objects.filter(status__in=open_statuses))
        backlog = len(pending)
        kpis.append(_kpi(
            "srp.pending_backlog", backlog,
            threshold_score(backlog, amber=max_pending, red=max_pending * 2, direction="lower_is_better"),
            {"pending": backlog, "max_pending_claims": max_pending},
        ))
        if backlog > max_pending:
            findings.append(Finding(
                kind="gap", dimension_key=self.key, kpi_key="srp.pending_backlog",
                severity="high", weight=float(backlog),
                label=f"{backlog} SRP claims pending (max {max_pending})",
                ref_type="srp", ref_id="pending_backlog",
                task_type="other", task_title=f"Clear the SRP backlog ({backlog} pending)",
            ))

        # avg_wait — mean hours from claim to decision over the last 30 days.
        since = now - dt.timedelta(days=30)
        decided = list(
            SrpClaim.objects.filter(decided_at__isnull=False, decided_at__gte=since)
            .values_list("created_at", "decided_at")
        )
        if decided:
            avg_wait = sum((d - c).total_seconds() for c, d in decided) / len(decided) / 3600.0
            kpis.append(_kpi(
                "srp.avg_wait", round(avg_wait, 1),
                threshold_score(avg_wait, amber=max_wait, red=max_wait * 2, direction="lower_is_better"),
                {"avg_wait_hours": round(avg_wait, 1), "max_avg_wait_hours": max_wait},
            ))

        # oldest_claim — age of the oldest pending claim in days (lower is better).
        oldest_age = max(((now - c.created_at).days for c in pending), default=0)
        kpis.append(_kpi(
            "srp.oldest_claim", oldest_age,
            threshold_score(oldest_age, amber=max_age, red=max_age * 2, direction="lower_is_better"),
            {"oldest_age_days": oldest_age, "max_claim_age_days": max_age},
        ))
        if oldest_age > max_age:
            findings.append(Finding(
                kind="gap", dimension_key=self.key, kpi_key="srp.oldest_claim",
                severity="high", weight=float(oldest_age),
                label=f"Oldest SRP claim is {oldest_age}d old (max {max_age}d)",
                ref_type="srp", ref_id="oldest_claim",
                task_type="other", task_title="Decide the oldest pending SRP claim",
            ))

        # budget_health — this period's spend vs allocation (lower is better). Spend is
        # derived live from PAID claims (SrpBudget stores only the allocation); reading
        # the old never-written ``spent`` column always scored a false-healthy 0.
        period = now.strftime("%Y-%m")
        budget = SrpBudget.objects.filter(period=period).first()
        if budget and budget.allocated:
            from apps.srp.services import spent_for_period

            spent = float(spent_for_period(period))
            ratio = spent / float(budget.allocated)
            kpis.append(_kpi(
                "srp.budget_health", round(ratio, 2),
                threshold_score(ratio, amber=1.0, red=1.5, direction="lower_is_better"),
                {"spent": spent, "allocated": float(budget.allocated)},
            ))

        score = combine_kpi_scores(kpis, ctx.config.get("kpis", {}))
        return DimensionResult(
            key=self.key, score=score, status=status_for(score),
            default_weight=self.default_weight, kpis=kpis, findings=findings,
            detail={"pending": backlog, "period": period},
        )


register(SrpProvider())
