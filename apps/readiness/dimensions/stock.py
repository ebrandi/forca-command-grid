"""Stock dimension (the stockpile half of the v1 ``_stock_and_logistics`` pass).

Coverage of corp stockpile targets: full marks when every target is met, decaying
with the share of shorted items. Keyed ``stock`` to match the v1 payload (the design
doc's eventual ``industrial`` rename is deferred to a later phase that may change the
score). Shares the ``stock_and_logistics`` computation with the logistics provider
via the context memo.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from ..engine.base import DimensionResult, Finding, KpiResult, ReadinessContext, status_for
from ..engine.registry import register
from .sources import get_stock_logistics

# SDE categories whose stockpile targets the module/ammo KPI grades.
_MODULE_CATEGORY = 7
_CHARGE_CATEGORY = 8


def _module_ammo_stock() -> int | None:
    """Corp-wide fill of module/ammo stockpile *targets* (Gap B7) — a display KPI; the
    dimension score stays the v1 all-types value. ``None`` when no module/ammo targets."""
    from apps.sde.models import SdeType
    from apps.stockpile.models import Stockpile, StockpileItem

    items = list(StockpileItem.objects.filter(
        stockpile__kind=Stockpile.Kind.CORP, quantity_target__isnull=False))
    if not items:
        return None
    cats = dict(
        SdeType.objects.filter(type_id__in=[i.type_id for i in items])
        .values_list("type_id", "group__category_id")
    )
    target = have = 0
    for i in items:
        if cats.get(i.type_id) in (_MODULE_CATEGORY, _CHARGE_CATEGORY):
            target += i.quantity_target
            have += min(i.quantity_current, i.quantity_target)
    if not target:
        return None
    return round(100 * have / target)


def _build_capacity() -> tuple[int | None, dict]:
    """Corp build-capacity over all doctrine hulls (Gap B8) — a display KPI.

    A hull counts buildable when the corp owns its blueprint (BPO or BPC, ``is_corp``)
    **and** at least one member has the manufacturing skills (``SdeBlueprintSkill``).
    Measured only over hulls with a known manufacturing recipe (honest denominator);
    ``(None, _)`` when none are measurable. Display-only — the dimension score is
    unchanged. Cheap-exits after two queries when no doctrine hull has recipe data
    (so the dashboard perf budget is unaffected on corps without blueprint imports).
    """
    from apps.characters.models import CharacterSkillSnapshot
    from apps.doctrines.models import DoctrineFit
    from apps.industry.models import Blueprint
    from apps.sde.models import SdeBlueprintSkill

    hull_ids = {h for h in DoctrineFit.objects.values_list("ship_type_id", flat=True) if h}
    if not hull_ids:
        return None, {}
    rows = list(SdeBlueprintSkill.objects.filter(
        product_type_id__in=hull_ids, activity=SdeBlueprintSkill.MANUFACTURING
    ).values_list("product_type_id", "blueprint_type_id", "skill_type_id", "level"))
    if not rows:
        return None, {}

    reqs: dict[int, list[tuple[int, int]]] = {}
    bp_of: dict[int, int] = {}
    for product, bp, skill, level in rows:
        reqs.setdefault(product, []).append((skill, level))
        bp_of[product] = bp
    corp_bps = set(Blueprint.objects.filter(
        is_corp=True, type_id__in=set(bp_of.values())).values_list("type_id", flat=True))
    snaps = list(CharacterSkillSnapshot.objects.filter(
        is_latest=True, character__is_corp_member=True))

    buildable = 0
    for product, needed in reqs.items():
        owns_bp = bp_of[product] in corp_bps
        skilled = any(
            all(s.trained_level(skill) >= level for skill, level in needed) for s in snaps)
        if owns_bp and skilled:
            buildable += 1
    measurable = len(reqs)
    return round(100 * buildable / measurable), {"buildable": buildable, "of": measurable}


class StockProvider:
    key = "stock"
    label = _("Stockpile Coverage")
    default_weight = 1.0
    data_sources = [_("Corp stockpiles"), _("Stockpile targets")]

    def compute(self, ctx: ReadinessContext) -> DimensionResult:
        dims, gaps = get_stock_logistics(ctx)
        score = dims["stock"]
        kpis = []
        ma = _module_ammo_stock()
        if ma is not None:
            kpis.append(KpiResult(key="stock.module_ammo_stock", value=round(ma / 100, 2),
                                  score=ma, status=status_for(ma), detail={}))
        bc, bc_detail = _build_capacity()
        if bc is not None:
            kpis.append(KpiResult(key="stock.build_capacity", value=round(bc / 100, 2),
                                  score=bc, status=status_for(bc), detail=bc_detail))
        findings = [
            Finding(
                kind=g["kind"],
                label=g["label"],
                label_key=g.get("label_key", ""),
                label_params=g.get("label_params") or {},
                weight=g["weight"],
                ref_type="stock",
                ref_id=g["ref_id"],
                task_type=g["task_type"],
                task_title=g["task_title"],
                task_title_key=g.get("task_title_key", ""),
                task_title_params=g.get("task_title_params") or {},
                dimension_key="stock",
            )
            for g in gaps
        ]
        return DimensionResult(
            key=self.key, score=score, status=status_for(score),
            default_weight=self.default_weight, kpis=kpis, findings=findings,
        )


register(StockProvider())
