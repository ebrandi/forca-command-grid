"""Financial Health dimension (``financial``) — design doc 06 §5.

Scores the corp wallet against leadership's ``readiness.finance`` targets: balance
vs minimum, ISK runway against the trailing net burn, reserve cover, and burn vs
target. Pure "add a provider" — no pipeline edit. Honest score: with no synced
wallet data the dimension is *unavailable* (excluded from the index), never a zero.
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

# Runway is clamped to this many months when the corp is cashflow-positive (no burn).
_RUNWAY_CAP = 99.0


def _kpi(key, value, score, detail):
    return KpiResult(key=key, value=value, score=score, status=status_for(score), detail=detail)


class FinancialProvider:
    key = "financial"
    label = _("Financial Health")
    default_weight = 1.2
    data_sources = [_("Corp wallet divisions"), _("Corp wallet journal"), _("Finance targets")]
    kpi_catalogue = [
        ("financial.wallet_vs_min", _("Wallet vs minimum")),
        ("financial.runway_months", _("Runway (months)")),
        ("financial.reserve_cover", _("Reserve cover")),
        ("financial.burn_vs_target", _("Burn vs target")),
    ]

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        import datetime as dt

        from django.db.models import Q, Sum
        from django.utils import timezone

        from apps.corporation.models import CorpWalletDivision, CorpWalletJournalEntry

        from .. import config as config_module

        cfg = config_module.get("finance")
        divisions = CorpWalletDivision.objects.all()
        scope = cfg.get("wallet_division_scope", "all")
        if scope != "all":
            divisions = divisions.filter(division=int(scope))

        # Honest score: no wallet data (scope ungranted / not synced) → unavailable.
        if not divisions.exists():
            return DimensionResult(
                key=self.key, score=None, status="unavailable",
                default_weight=self.default_weight,
                detail={"reason": "No corp wallet data — grant the wallet scope to score this."},
            )

        balance = float(divisions.aggregate(t=Sum("balance"))["t"] or 0.0)

        since = timezone.now() - dt.timedelta(days=30)
        window = CorpWalletJournalEntry.objects.filter(date__gte=since)
        agg = window.aggregate(
            income=Sum("amount", filter=Q(amount__gt=0)),
            expense=Sum("amount", filter=Q(amount__lt=0)),
        )
        income = float(agg["income"] or 0.0)
        expense = float(-(agg["expense"] or 0.0))
        net_burn = expense - income  # net monthly outflow (negative ⇒ gaining ISK)
        # An empty journal window means burn is unknown, NOT zero — scoring it as
        # perfect health would mask a lagging/broken wallet sync. The burn-derived
        # KPIs are then excluded (honest score), while the balance-derived ones stand.
        has_burn_data = window.exists()

        min_wallet = float(cfg["min_wallet"]) or 1.0
        burn_target = float(cfg["monthly_burn_target"]) or 1.0
        reserve = float(cfg["emergency_reserve"]) or 1.0

        kpis: list[KpiResult] = []
        findings: list[Finding] = []

        # wallet_vs_min — at the minimum → 100, at half → 0.
        kpis.append(_kpi(
            "financial.wallet_vs_min", round(balance / min_wallet, 2),
            threshold_score(balance, amber=min_wallet, red=min_wallet * 0.5),
            {"balance": balance, "min_wallet": min_wallet},
        ))

        # runway_months — amber 3 / red 1 (doc §5). Cashflow-positive ⇒ capped full.
        # Excluded entirely when there's no journal data to derive burn from.
        runway = _RUNWAY_CAP if net_burn <= 0 else min(balance / net_burn, _RUNWAY_CAP)
        runway_score = threshold_score(runway, amber=3, red=1) if has_burn_data else None
        kpis.append(_kpi(
            "financial.runway_months", round(runway, 1) if has_burn_data else None, runway_score,
            {"balance": balance, "net_monthly_burn": net_burn, "has_burn_data": has_burn_data},
        ))
        if runway_score is not None and runway < 1:
            findings.append(Finding(
                kind="risk", dimension_key=self.key, kpi_key="financial.runway_months",
                severity="critical", weight=40.0,
                label=f"Corp runway is {runway:.1f} months — below 1 month of cover",
                ref_type="financial", ref_id="runway_months",
                task_type="other", task_title="Shore up the corp wallet — runway critical",
            ))

        # reserve_cover — liquid vs the emergency reserve.
        kpis.append(_kpi(
            "financial.reserve_cover", round(balance / reserve, 2),
            threshold_score(balance, amber=reserve, red=reserve * 0.5),
            {"balance": balance, "emergency_reserve": reserve},
        ))

        # burn_vs_target — actual net burn vs target (lower is better). Excluded when
        # there's no journal data (an empty window is unknown burn, not zero burn).
        burn_score = threshold_score(
            max(net_burn, 0.0), amber=burn_target, red=burn_target * 1.5,
            direction="lower_is_better",
        ) if has_burn_data else None
        kpis.append(_kpi(
            "financial.burn_vs_target",
            round(max(net_burn, 0.0) / burn_target, 2) if has_burn_data else None, burn_score,
            {"net_monthly_burn": net_burn, "monthly_burn_target": burn_target},
        ))
        if burn_score is not None and net_burn > burn_target:
            findings.append(Finding(
                kind="risk", dimension_key=self.key, kpi_key="financial.burn_vs_target",
                severity="high", weight=20.0,
                label=f"Monthly burn ({net_burn/1e9:.1f}B) is over target ({burn_target/1e9:.1f}B)",
                ref_type="financial", ref_id="burn_vs_target",
                task_type="other", task_title="Review corp expenses — over burn target",
            ))

        score = combine_kpi_scores(kpis, ctx.config.get("kpis", {}))
        return DimensionResult(
            key=self.key, score=score, status=status_for(score),
            default_weight=self.default_weight, kpis=kpis, findings=findings,
            detail={"balance": balance, "income_30d": income, "expense_30d": expense},
        )


register(FinancialProvider())
