"""Member Activity dimension (``activity``) — design doc 06 §9.

Scores how engaged the corp is from data the app already collects: the share of
members active in the last 30 days (a fleet, a contribution, or a killmail), mean
fleet participation, and the breadth of contribution kinds. Pure provider, no new
tables. Honest score: with no members it is unavailable (excluded from the index).
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
    ratio_score,
    status_for,
)
from ..engine.registry import register


def _kpi(key, value, score, detail):
    return KpiResult(key=key, value=value, score=score, status=status_for(score), detail=detail)


class ActivityProvider:
    key = "activity"
    label = _("Member Activity")
    default_weight = 1.0
    data_sources = [_("Operation attendance"), _("Contribution ledger"), _("Killboard")]
    kpi_catalogue = [
        ("activity.active_ratio", _("Active ratio")),
        ("activity.fleet_participation", _("Fleet participation")),
        ("activity.contribution_breadth", _("Contribution breadth")),
    ]

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        from django.utils import timezone

        from apps.killboard.models import KillmailParticipant
        from apps.operations.models import OperationAttendance
        from apps.pilots.models import ContributionEvent
        from apps.sso.models import EveCharacter

        members = list(EveCharacter.objects.filter(is_corp_member=True))
        total = len(members)
        if not total:
            return DimensionResult(
                key=self.key, score=None, status="unavailable",
                default_weight=self.default_weight,
                detail={"reason": "No corp members to measure activity for."},
            )

        now = timezone.now()
        since_30 = now - dt.timedelta(days=30)
        char_ids = [m.character_id for m in members]
        user_ids = [m.user_id for m in members if m.user_id]

        # Active = a fleet PAP, a contribution, or a killmail in the last 30 days.
        active_users = set(
            OperationAttendance.objects.filter(user_id__in=user_ids, created_at__gte=since_30)
            .values_list("user_id", flat=True)
        )
        active_users |= set(
            ContributionEvent.objects.filter(user_id__in=user_ids, occurred_at__gte=since_30)
            .values_list("user_id", flat=True)
        )
        active_chars = set(
            KillmailParticipant.objects.filter(
                character_id__in=char_ids, killmail__killmail_time__gte=since_30
            ).values_list("character_id", flat=True)
        )
        # Count a member active if either their account or character shows activity.
        active = sum(
            1 for m in members
            if (m.user_id in active_users) or (m.character_id in active_chars)
        )

        kpis = [
            _kpi("activity.active_ratio", round(active / total, 2),
                 ratio_score(active, total), {"active": active, "members": total}),
        ]

        # fleet_participation — mean PAPs per member over the window (capped at a
        # "healthy" 4/month → 100).
        paps = OperationAttendance.objects.filter(
            user_id__in=user_ids, created_at__gte=since_30
        ).count()
        mean_paps = paps / total
        kpis.append(_kpi(
            "activity.fleet_participation", round(mean_paps, 2),
            min(100, round(100 * mean_paps / 4)), {"paps": paps, "members": total},
        ))

        # contribution_breadth — share of members contributing across ≥2 kinds.
        kinds_by_user: dict[int, set] = {}
        for uid, kind in ContributionEvent.objects.filter(
            user_id__in=user_ids, occurred_at__gte=since_30
        ).values_list("user_id", "kind"):
            kinds_by_user.setdefault(uid, set()).add(kind)
        broad = sum(1 for ks in kinds_by_user.values() if len(ks) >= 2)
        kpis.append(_kpi(
            "activity.contribution_breadth", round(broad / total, 2),
            ratio_score(broad, total), {"broad_contributors": broad, "members": total},
        ))

        findings = []
        active_ratio = active / total
        if active_ratio < 0.5:
            findings.append(Finding(
                kind="risk", dimension_key=self.key, kpi_key="activity.active_ratio",
                severity="high", weight=round(100 * (0.5 - active_ratio)),
                label=f"Only {active}/{total} members active in the last 30 days",
                ref_type="activity", ref_id="active_ratio",
                task_type="prepare", task_title="Re-engage dormant members",
            ))

        score = combine_kpi_scores(kpis, ctx.config.get("kpis", {}))
        return DimensionResult(
            key=self.key, score=score, status=status_for(score),
            default_weight=self.default_weight, kpis=kpis, findings=findings,
            detail={"active": active, "members": total},
        )


register(ActivityProvider())
