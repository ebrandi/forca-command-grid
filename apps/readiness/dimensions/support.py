"""Fleet Support dimension (``support``) — Gap B4.

Scores the corp's fleet-support depth: the share of members trained to each
leadership-defined support skill (boosters, warfare-link specialists, logi
cap-transfer …), averaged across the configured skills. The skill list is curated
on the *Fleet support skills* admin page; with **no active skills the dimension is
unavailable** (it ships disabled and stays disabled until configured). Honest score:
members with no skill import are excluded from the denominator, exactly like the
doctrine/skill scan.
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


def _kpi(key, value, score, detail) -> KpiResult:
    return KpiResult(key=key, value=value, score=score, status=status_for(score), detail=detail)


class FleetSupportProvider:
    key = "support"
    label = _("Fleet Support")
    default_weight = 0.8
    data_sources = [_("Fleet support skills"), _("Character skills")]
    # Per-skill KPI keys are dynamic (one per configured skill), so there is no static
    # catalogue to list on the KPI-config page.
    kpi_catalogue: list = []

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        from apps.characters.models import CharacterSkillSnapshot
        from apps.readiness.models import FleetSupportSkill

        skills = list(FleetSupportSkill.objects.filter(active=True))
        if not skills:
            return DimensionResult(
                key=self.key, score=None, status="unavailable",
                default_weight=self.default_weight,
                detail={"reason": "No fleet-support skills configured."},
            )

        # Latest skill snapshot per corp member; members with no import are the unknown
        # set and are excluded from the denominator (honest score).
        snapshots = list(
            CharacterSkillSnapshot.objects.filter(is_latest=True, character__is_corp_member=True)
        )
        known = len(snapshots)
        kpis: list[KpiResult] = []
        findings: list[Finding] = []
        for sk in skills:
            name = sk.skill_name or str(sk.skill_type_id)
            if known:
                trained = sum(
                    1 for s in snapshots if s.trained_level(sk.skill_type_id) >= sk.min_level
                )
                score = round(100 * trained / known)
            else:
                trained, score = 0, None
            kpis.append(_kpi(
                f"support.skill_{sk.skill_type_id}",
                round(score / 100, 2) if score is not None else None, score,
                {"skill": name, "level": sk.min_level, "trained": trained, "known": known},
            ))
            if score is not None and score < 50:
                findings.append(Finding(
                    kind="gap", dimension_key=self.key,
                    kpi_key=f"support.skill_{sk.skill_type_id}", severity="warn",
                    weight=float(50 - score),
                    label=f"Thin {name} bench — {trained}/{known} at L{sk.min_level}",
                    ref_type="support_skill", ref_id=str(sk.skill_type_id),
                    task_type="train", task_title=f"Grow the {name} (L{sk.min_level}) bench",
                ))

        score = combine_kpi_scores(kpis, ctx.config.get("kpis", {}))
        return DimensionResult(
            key=self.key, score=score, status=status_for(score),
            default_weight=self.default_weight, kpis=kpis, findings=findings,
            detail={"known": known, "skills": len(skills)},
        )


register(FleetSupportProvider())
