"""Manufacturing estimate + profit analysis (standalone, SDE + market driven).

This is the calculation layer behind the Manufacturing Calculator and the profit
panels. It is deliberately separate from the saved-project BOM in ``services.py``
so the calculator works with no persisted state. Every rate is an explicit,
overridable assumption and every estimate returns the assumptions it used plus a
``warnings`` list — we never present a number as guaranteed profit.

Job cost uses the real EVE formula: EIV (estimated item value = base materials at
ME0 x CCP adjusted price) x (system cost index + facility tax). Adjusted prices are
CCP's daily figures (``MarketPrice`` ADJUSTED profile); if one is missing we fall
back to the Jita price and flag it.
"""
from __future__ import annotations

from decimal import Decimal

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.market.models import MarketPrice
from apps.market.pricing import price_for
from apps.sde.models import SdeBlueprintActivityTime, SdeBlueprintMaterial, SdeType

from . import bom

# Human labels for the SDE activity code the estimate reports. The estimate stays a plain
# dict; the value is a lazy proxy resolved in the reader's locale at render time (never a
# stored/compared code — the recipe keeps the canonical ``manufacturing``/``reaction`` code).
ACTIVITY_LABELS = {
    "manufacturing": gettext_lazy("Manufacturing"),
    "reaction": gettext_lazy("Reaction"),
    "invention": gettext_lazy("Invention"),
}

# Sensible defaults; the admin config and the calculator UI can override all of them.
DEFAULT_SYSTEM_COST_INDEX = Decimal("0.05")
DEFAULT_FACILITY_TAX = Decimal("0.0025")
DEFAULT_SALES_TAX = Decimal("0.045")
DEFAULT_BROKER_FEE = Decimal("0.015")


def adjusted_price(type_id: int) -> Decimal:
    """CCP adjusted price (EIV input); falls back to the Jita sell price.

    Both lookups are served from the 300 s in-process price snapshot (``price_for``),
    so an EIV over a whole BOM does not issue one MarketPrice query per material. Same
    resolution order as before (adjusted → Jita sell → 0)."""
    adjusted = price_for(type_id, MarketPrice.Profile.ADJUSTED)
    if adjusted:
        return adjusted
    return price_for(type_id)


def estimated_item_value(product_type_id: int, runs: int) -> Decimal:
    """EIV = sum(base ME0 material qty x adjusted price) x runs — the CCP job-cost base."""
    total = Decimal("0")
    for row in SdeBlueprintMaterial.objects.filter(
        product_type_id=product_type_id, activity=SdeBlueprintMaterial.MANUFACTURING
    ):
        total += adjusted_price(row.material_type_id) * row.quantity
    return total * max(1, runs)


def production_seconds(product_type_id: int, runs: int, te: int = 0) -> int | None:
    """Base manufacturing time x runs, reduced by TE%. ``None`` if unknown."""
    base = (
        SdeBlueprintActivityTime.objects.filter(
            product_type_id=product_type_id,
            activity=SdeBlueprintActivityTime.MANUFACTURING,
        )
        .values_list("time", flat=True)
        .first()
    )
    if not base:
        return None
    te = max(0, min(20, te))
    return int(base * max(1, runs) * (100 - te) / 100)


def _volume_map(type_ids) -> dict[int, float]:
    return dict(
        SdeType.objects.filter(type_id__in=list(type_ids)).values_list("type_id", "volume")
    )


def manufacturing_estimate(
    product_type_id: int,
    *,
    runs: int = 1,
    me: int = 0,
    te: int = 0,
    structure_bonus: float = 0.0,
    strategy: str = bom.STRATEGY_BUILD_VS_BUY,
    max_depth: int = bom.DEFAULT_MAX_DEPTH,
    system_cost_index: Decimal = DEFAULT_SYSTEM_COST_INDEX,
    facility_tax: Decimal = DEFAULT_FACILITY_TAX,
    sales_tax: Decimal = DEFAULT_SALES_TAX,
    broker_fee: Decimal = DEFAULT_BROKER_FEE,
    on_hand: dict[int, int] | None = None,
    invention_cost_per_unit: Decimal | None = None,
    price=price_for,
) -> dict:
    """Full manufacturing + profit estimate for building a product.

    ``on_hand`` (type_id -> qty) nets available stock off the buy list. If the item
    is T2 and ``invention_cost_per_unit`` is given, that BPC cost is folded into the
    total. Returns ``{"buildable": False, ...}`` when there's no recipe.
    """
    recipe = bom.buildable_recipe(product_type_id)
    if recipe is None:
        return {"buildable": False, "product_type_id": product_type_id,
                "warnings": [
                    _("No blueprint / recipe for this item in the SDE — it can only be bought.")
                ]}

    output_per_run = recipe.output_quantity or 1
    total_units = max(1, runs) * output_per_run

    result = bom.expand(product_type_id, total_units, strategy=strategy, max_depth=max_depth,
                        me=me, structure_bonus=structure_bonus)
    on_hand = on_hand or {}

    warnings: list[str] = []
    vols = _volume_map(result.leaves)
    material_cost = Decimal("0")
    hauling_volume = 0.0
    materials = []
    for tid, qty in sorted(result.leaves.items()):
        unit = price(tid)
        if unit <= 0:
            warnings.append(
                _("Missing market price for type %(type_id)s — treated as 0 ISK.")
                % {"type_id": tid}
            )
        have = int(on_hand.get(tid, 0))
        to_buy = max(0, qty - have)
        material_cost += unit * to_buy
        hauling_volume += float(vols.get(tid, 0.0)) * to_buy
        materials.append({
            "type_id": tid, "required": qty, "available": have, "to_buy": to_buy,
            "unit_cost": unit, "line_cost": unit * to_buy,
        })

    eiv = estimated_item_value(product_type_id, max(1, runs))
    install_fee = (eiv * (system_cost_index + facility_tax)).quantize(Decimal("0.01"))

    inv_cost = Decimal("0")
    if invention_cost_per_unit:
        inv_cost = (invention_cost_per_unit * total_units).quantize(Decimal("0.01"))

    total_cost = material_cost + install_fee + inv_cost

    unit_sell = price(product_type_id)
    if unit_sell <= 0:
        warnings.append(_("Missing market price for the product — revenue shown as 0."))
    revenue_gross = unit_sell * total_units
    fee_rate = sales_tax + broker_fee
    revenue_net = revenue_gross * (Decimal("1") - fee_rate)
    net_profit = revenue_net - total_cost
    margin = (net_profit / revenue_gross) if revenue_gross > 0 else None
    roi = (net_profit / total_cost) if total_cost > 0 else None
    break_even = (
        (total_cost / (total_units * (Decimal("1") - fee_rate))).quantize(Decimal("0.01"))
        if fee_rate < 1 else None
    )
    seconds = production_seconds(product_type_id, runs, te)

    return {
        "buildable": True,
        "product_type_id": product_type_id,
        "runs": runs,
        "output_per_run": output_per_run,
        "total_units": total_units,
        "activity": ACTIVITY_LABELS.get(recipe.activity, recipe.activity),
        "me": me,
        "te": te,
        "materials": materials,
        "steps": [
            {"type_id": s.type_id, "activity": s.activity, "runs": s.runs,
             "produced": s.produced, "needed": s.needed, "depth": s.depth}
            for s in result.steps
        ],
        "material_cost": material_cost,
        "install_fee": install_fee,
        "eiv": eiv,
        "invention_cost": inv_cost,
        "total_cost": total_cost,
        "revenue_gross": revenue_gross,
        "revenue_net": revenue_net,
        "net_profit": net_profit,
        "margin": margin,
        "profit_per_unit": (net_profit / total_units) if total_units else None,
        "roi": roi,
        "break_even_price": break_even,
        "hauling_volume_m3": round(hauling_volume, 2),
        "production_seconds": seconds,
        "missing_materials": [m for m in materials if m["to_buy"] > 0],
        "assumptions": {
            "system_cost_index": system_cost_index,
            "facility_tax": facility_tax,
            "sales_tax": sales_tax,
            "broker_fee": broker_fee,
            "strategy": strategy,
            # Two complete sentences per variant — never concatenate fragments, a
            # translator needs the whole sentence to reorder it.
            "note": (
                _(
                    "Prices are Jita-sell estimates; job cost uses CCP adjusted prices × "
                    "your system cost index + facility tax. A structure/rig material bonus "
                    "is applied. Manufacturing time skills are not modelled — treat as a "
                    "guide, not a guarantee."
                )
                if structure_bonus
                else _(
                    "Prices are Jita-sell estimates; job cost uses CCP adjusted prices × "
                    "your system cost index + facility tax. Structure/rig material bonuses "
                    "are not modelled. Manufacturing time skills are not modelled — treat "
                    "as a guide, not a guarantee."
                )
            ),
        },
        "warnings": warnings,
    }


def build_vs_buy(product_type_id: int, *, runs: int = 1, me: int = 0, price=price_for) -> dict:
    """One-glance build-vs-buy for a product at the given runs/ME."""
    est = manufacturing_estimate(product_type_id, runs=runs, me=me, price=price)
    if not est["buildable"]:
        return {
            "buildable": False,
            # ``decision`` stays the canonical code every caller compares against;
            # ``decision_label`` is the translated half a human reads.
            "decision": "buy",
            "decision_label": bom.decision_label("buy"),
            "reason": _("No recipe."),
        }
    buy_total = price(product_type_id) * est["total_units"]
    build_total = est["material_cost"] + est["install_fee"]
    decision = "build" if build_total < buy_total else "buy"
    return {
        "buildable": True,
        "decision": decision,
        "decision_label": bom.decision_label(decision),
        "build_cost": build_total,
        "buy_cost": buy_total,
        "saving": buy_total - build_total,
    }
