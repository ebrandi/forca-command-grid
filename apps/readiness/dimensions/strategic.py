"""Strategic Assets dimension (``strategic``) — design doc 06 §10.

Scores the corp's strategic depth: the share of pilots who own their leadership-
defined mandatory ships, and the capital / cyno bench against ``StrategicRoleTarget``
headcount targets. Reads ``MandatoryShip`` + ``StrategicRoleTarget`` (Phase-6a tables)
and the personal-asset mirror. Honest score: each KPI is included only when its
input exists — no mandatory ships and no role targets ⇒ the dimension is unavailable.
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

# Strategic role keys whose bench this dimension grades (capitals + cyno/scout).
_BENCH_ROLES = ("dread", "carrier", "fax", "super", "titan", "cyno", "scout")
_CYNO_ROLES = ("cyno", "scout")  # graded as their own KPI (Gap B9)


def _kpi(key, value, score, detail):
    return KpiResult(key=key, value=value, score=score, status=status_for(score), detail=detail)


def _mandatory_ship_coverage(members):
    """Share of members owning each active hull-based MandatoryShip, averaged.

    Only ship-hull mandatory entries are auto-checkable against the personal-asset
    mirror; doctrine-fit entries (no single ``ship_type_id``) need a fit-completeness
    check and are deferred. Returns ``(score, detail)`` or ``(None, _)`` if nothing
    to check.
    """
    from apps.readiness.models import MandatoryShip
    from apps.stockpile.models import Asset

    # Hull-based, corp-wide mandatory ships (blank applies_to_role). Role-specific
    # entries are graded by the bench KPIs instead; doctrine-fit entries need a
    # fit-completeness check and are deferred.
    ships = [
        s for s in MandatoryShip.objects.filter(active=True, ship_type_id__isnull=False)
        if not s.applies_to_role
    ]
    if not ships or not members:
        return None, {}

    char_ids = [m.character_id for m in members]
    ratios = []
    detail_rows = []
    for ship in ships:
        owners = set(
            Asset.objects.filter(
                owner_type=Asset.Owner.CHARACTER, owner_id__in=char_ids,
                type_id=ship.ship_type_id, quantity__gte=ship.required_quantity,
            ).values_list("owner_id", flat=True)
        )
        ratio = len(owners) / len(members)
        ratios.append(ratio)
        detail_rows.append({"ship": ship.label, "owned_by": len(owners), "of": len(members)})
    mean = sum(ratios) / len(ratios)
    return round(100 * mean), {"ships": detail_rows}


class StrategicProvider:
    key = "strategic"
    label = _("Strategic Assets")
    default_weight = 0.8
    data_sources = [_("Mandatory ships"), _("Strategic role targets"), _("Personal assets")]
    kpi_catalogue = [
        ("strategic.mandatory_ship_coverage", _("Mandatory ship coverage")),
        ("strategic.capital_bench", _("Capital bench")),
        ("strategic.cyno_coverage", _("Cyno / scout coverage")),
    ]

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        from apps.readiness.models import StrategicRoleTarget
        from apps.sso.models import EveCharacter

        members = list(EveCharacter.objects.filter(is_corp_member=True))
        kpis: list[KpiResult] = []
        findings: list[Finding] = []

        # mandatory_ship_coverage — pilots owning their mandatory hulls.
        ms_score, ms_detail = _mandatory_ship_coverage(members)
        if ms_score is not None:
            kpis.append(_kpi("strategic.mandatory_ship_coverage",
                             round(ms_score / 100, 2), ms_score, ms_detail))
            if ms_score < 60:
                findings.append(Finding(
                    kind="gap", dimension_key=self.key,
                    kpi_key="strategic.mandatory_ship_coverage", severity="warn",
                    weight=float(60 - ms_score),
                    label=f"Only {ms_score}% mandatory-ship coverage across the corp",
                    ref_type="strategic", ref_id="mandatory_ship_coverage",
                    task_type="buy", task_title="Get pilots into their mandatory ships",
                ))

        # capital_bench / cyno_coverage — qualified pilots vs StrategicRoleTarget.
        cap_scores, cyno_scores = [], []
        for target in StrategicRoleTarget.objects.filter(role_key__in=_BENCH_ROLES, active=True):
            qualified, score = role_score(target)
            if score is None:
                continue
            (cyno_scores if target.role_key in _CYNO_ROLES else cap_scores).append(score)
            if qualified < target.desired_count:
                findings.append(Finding(
                    kind="gap", dimension_key=self.key, kpi_key=f"strategic.{target.role_key}_bench",
                    severity="warn", weight=float(target.desired_count - qualified),
                    label=f"{target.label} bench {qualified}/{target.desired_count}",
                    ref_type="role", ref_id=target.role_key,
                    task_type="train", task_title=f"Grow the {target.label} bench",
                ))
        if cap_scores:
            kpis.append(_kpi("strategic.capital_bench", None,
                             round(sum(cap_scores) / len(cap_scores)), {"roles_scored": len(cap_scores)}))
        if cyno_scores:  # cyno/scout split out (Gap B9)
            kpis.append(_kpi("strategic.cyno_coverage", None,
                             round(sum(cyno_scores) / len(cyno_scores)), {"roles_scored": len(cyno_scores)}))

        if not kpis:
            return DimensionResult(
                key=self.key, score=None, status="unavailable",
                default_weight=self.default_weight,
                detail={"reason": "No mandatory ships or strategic role targets configured."},
            )

        score = combine_kpi_scores(kpis, ctx.config.get("kpis", {}))
        return DimensionResult(
            key=self.key, score=score, status=status_for(score),
            default_weight=self.default_weight, kpis=kpis, findings=findings,
            detail={"members": len(members)},
        )


register(StrategicProvider())
