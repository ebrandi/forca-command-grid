"""Doctrine + skill dimensions (the v1 ``_doctrine_and_skill`` pass).

Two providers backed by one shared computation (memoised on the context, so the
expensive member×doctrine scan runs once): ``doctrine`` (weighted fleet-coverage
ratio) and ``skill`` (share of pilots who can fly *something*). Keys are the exact
v1 payload keys so the dashboard and callers are unchanged; the design doc's
eventual ``combat`` rename for the skill dimension is deferred (it would change the
payload, which Phase 0 must not).
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from ..engine.base import DimensionResult, Finding, KpiResult, ReadinessContext, status_for
from ..engine.registry import register
from ..messages import english_text
from .sources import get_doctrine_skill


def _gap_finding(gap: dict) -> Finding:
    return Finding(
        kind=gap["kind"],
        label=gap["label"],
        label_key=gap.get("label_key", ""),
        label_params=gap.get("label_params") or {},
        weight=gap["weight"],
        ref_type="doctrine",
        ref_id=gap["ref_id"],
        task_type=gap["task_type"],
        task_title=gap["task_title"],
        task_title_key=gap.get("task_title_key", ""),
        task_title_params=gap.get("task_title_params") or {},
        dimension_key="doctrine",
    )


def _config_findings(gaps: list[dict]) -> list[Finding]:
    """Extra findings from leadership's per-doctrine classification (G5; doc 07 §3.3).

    Strictly config-gated: with no ``DoctrineReadinessConfig`` rows this returns ``[]``,
    so the dimension's findings (and therefore the index and the golden v1 payload) are
    byte-identical until leadership classifies a doctrine. A *mandatory* doctrine that is
    under-crewed (already in the gap list) escalates to a high-severity finding; a
    doctrine past its *retirement date* raises a replace-it finding.
    """
    from apps.readiness.models import DoctrineReadinessConfig

    configs = list(DoctrineReadinessConfig.objects.select_related("doctrine"))
    if not configs:
        return []

    from django.utils import timezone

    today = timezone.now().date()
    by_id = {str(c.doctrine_id): c for c in configs}
    findings: list[Finding] = []
    for gap in gaps:
        cfg = by_id.get(gap["ref_id"])
        if cfg and cfg.is_mandatory:
            params = {"doctrine": cfg.doctrine.name}
            findings.append(Finding(
                kind="doctrine", dimension_key="doctrine", kpi_key="doctrine.mandatory_coverage",
                severity="high", weight=round(float(gap["weight"]) + 10, 2),
                label=english_text("doctrine.mandatory_under_crewed", params),
                label_key="doctrine.mandatory_under_crewed", label_params=params,
                ref_type="doctrine", ref_id=gap["ref_id"],
                task_type="train",
                task_title=english_text("doctrine.crew_mandatory_task", params),
                task_title_key="doctrine.crew_mandatory_task", task_title_params=params,
            ))
    for cfg in configs:
        if cfg.retirement_date and cfg.retirement_date <= today:
            params = {"doctrine": cfg.doctrine.name}
            findings.append(Finding(
                kind="doctrine", dimension_key="doctrine", kpi_key="doctrine.retirement",
                severity="warn", weight=20.0,
                label=english_text("doctrine.past_retirement", params),
                label_key="doctrine.past_retirement", label_params=params,
                ref_type="doctrine", ref_id=str(cfg.doctrine_id),
                task_type="other",
                task_title=english_text("doctrine.retire_task", params),
                task_title_key="doctrine.retire_task", task_title_params=params,
            ))
    return findings


def _coverage_over(per_doctrine: dict, ids) -> tuple[int | None, dict]:
    """Priority-weighted ready/known coverage over a subset of doctrines (the same
    formula as the dimension score, restricted to ``ids``). ``(score, detail)``."""
    total_w = acc = 0.0
    ready = known = n = 0
    for did in ids:
        d = per_doctrine.get(did)
        if not d or not d["known"]:
            continue
        w = max(d["priority"], 1)
        total_w += w
        acc += w * (d["ready"] / d["known"])
        ready += d["ready"]
        known += d["known"]
        n += 1
    score = round(100 * acc / total_w) if total_w else None
    return score, {"doctrines": n, "ready": ready, "known": known}


def _kpi(key, score, detail) -> KpiResult:
    return KpiResult(key=key, value=(round(score / 100, 2) if score is not None else None),
                     score=score, status=status_for(score), detail=detail)


def _doctrine_kpis(per_doctrine: dict) -> list[KpiResult]:
    """Display KPIs (Gap B): all / primary / upcoming coverage. These never change the
    dimension score (kept = the frozen priority-weighted value); they enrich the
    drill-down and per-KPI history. Primary/upcoming use the classification flags."""
    from apps.readiness.models import DoctrineReadinessConfig

    cfg = {c.doctrine_id: c for c in DoctrineReadinessConfig.objects.all()}
    all_ids = list(per_doctrine)
    primary_ids = [d for d in all_ids if getattr(cfg.get(d), "is_primary", False)]
    upcoming_ids = [d for d in all_ids if getattr(cfg.get(d), "is_upcoming", False)]
    kpis = [_kpi("doctrine.all_coverage", *_coverage_over(per_doctrine, all_ids))]
    if primary_ids:
        kpis.append(_kpi("doctrine.primary_coverage", *_coverage_over(per_doctrine, primary_ids)))
    if upcoming_ids:
        kpis.append(_kpi("doctrine.upcoming_coverage", *_coverage_over(per_doctrine, upcoming_ids)))
    return kpis


class DoctrineProvider:
    key = "doctrine"
    label = _("Doctrine Readiness")
    default_weight = 1.0
    data_sources = [_("Doctrines"), _("Doctrine fits"), _("Character skills")]

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        dims, coverage, gaps, per_doctrine = get_doctrine_skill(ctx)
        score = dims["doctrine"]
        return DimensionResult(
            key=self.key,
            score=score,
            status=status_for(score),
            default_weight=self.default_weight,
            kpis=_doctrine_kpis(per_doctrine),
            findings=[_gap_finding(g) for g in gaps] + _config_findings(gaps),
            # Corp-level sample coverage is measured here and surfaced by the
            # pipeline as the payload's ``coverage`` block (v1 parity).
            detail={"corp_coverage": coverage},
        )


def _combat_kpis(ctx: ReadinessContext, skill_score, coverage: dict) -> list[KpiResult]:
    """Display KPIs (Gap B): flyable members + recent-PvP participation (30 days). These
    don't change the dimension score (kept = the frozen flyable-share value)."""
    import datetime as dt

    from django.utils import timezone

    from apps.killboard.models import KillmailParticipant

    kpis = [_kpi("combat.flyable_members", skill_score,
                 {"ready_any": coverage.get("ready_any", 0), "known": coverage.get("known", 0)})]
    char_ids = [c.character_id for c in ctx.characters]
    if char_ids:
        since = timezone.now() - dt.timedelta(days=30)
        recent = (
            KillmailParticipant.objects.filter(
                character_id__in=char_ids, killmail__killmail_time__gte=since)
            .values_list("character_id", flat=True).distinct().count()
        )
        kpis.append(_kpi("combat.recent_pvp", round(100 * recent / len(char_ids)),
                         {"on_a_kill_30d": recent, "members": len(char_ids)}))
    return kpis


class SkillProvider:
    key = "skill"
    label = _("Skill Coverage")
    default_weight = 1.0
    data_sources = [_("Doctrines"), _("Character skills"), _("Killboard")]

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        dims, coverage, _gaps, _per = get_doctrine_skill(ctx)  # shared, no recompute
        score = dims["skill"]
        return DimensionResult(
            key=self.key, score=score, status=status_for(score),
            default_weight=self.default_weight,
            kpis=_combat_kpis(ctx, score, coverage),
        )


# Registration order matters: doctrine then skill, matching the v1 dimensions dict
# order and the gap-flatten order the dashboard expects.
register(DoctrineProvider())
register(SkillProvider())
