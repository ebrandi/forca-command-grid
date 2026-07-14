"""Recommendation engine (Journey 5).

Scores candidate PI products by estimated ISK/day for a single planet, honestly
labelled, and attaches badges + a plain-English explanation. It reuses the same
assumptions as the calculator so recommendations and plan estimates agree.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.utils.translation import gettext as _

from .chains import PiGraph, build_graph
from .constants import DEFAULT_FACTORY_OUTPUT_PER_DAY, TIER_ORDER
from .labels import complexity_label
from .prices import PriceProvider

_CENTS = Decimal("0.01")


def _money(v) -> float:
    return float(Decimal(v).quantize(_CENTS, rounding=ROUND_HALF_UP))


def _pct(v) -> Decimal:
    return Decimal(str(v)) / Decimal(100)


# Which tiers a goal cares about (the candidate pool).
_GOAL_TIERS = {
    "beginner": ["P1"],
    "low_effort": ["P0", "P1"],
    "p0_p1": ["P1"],
    "p0_p2": ["P2"],
    "factory": ["P2"],
    "p3_p4": ["P3", "P4"],
    "corp_supply": ["P2", "P3", "P4"],
    "max_profit": ["P1", "P2", "P3", "P4"],
}


def _complexity_for_tier(tier: str) -> str:
    idx = TIER_ORDER.index(tier) if tier in TIER_ORDER else 0
    return "Low" if idx <= 1 else "Medium" if idx <= 2 else "High"


def estimate_product(graph: PiGraph, provider: PriceProvider, config, material_id: int) -> dict | None:
    """Single-planet ISK/day estimate for producing one product, using config defaults."""
    info = graph.material(material_id)
    if info is None:
        return None
    tier = info.tier
    role = "extract" if tier in ("P0", "P1") else "factory"
    rate = Decimal(config.default_extraction_rate_per_hour)

    if tier == "P0":
        daily = rate * 24
    elif tier == "P1":
        sch = graph.schematic_for(material_id)
        if not sch or not sch.inputs:
            return None
        in_qty = next(iter(sch.inputs.values()))
        daily = rate * 24 / Decimal(in_qty) * Decimal(sch.output_qty)
    else:
        daily = Decimal(DEFAULT_FACTORY_OUTPUT_PER_DAY.get(tier, 0))

    sell = provider.sell(material_id)
    gross = sell * daily
    fees = gross * _pct(Decimal(config.default_sales_tax) + Decimal(config.default_broker_fee))

    input_cost = Decimal(0)
    customs_import = Decimal(0)
    inputs_priced = True
    if role == "factory":
        sch = graph.schematic_for(material_id)
        if sch and sch.output_qty:
            runs = daily / Decimal(sch.output_qty)
            for in_id, in_qty in sch.inputs.items():
                units = Decimal(in_qty) * runs
                input_cost += provider.sell(in_id) * units
                customs_import += provider.base_value(in_id) * units * _pct(config.default_customs_import_tax)
                inputs_priced = inputs_priced and provider.is_priced(in_id)

    customs_export = provider.base_value(material_id) * daily * _pct(config.default_customs_export_tax)
    net = gross - input_cost - customs_export - customs_import - fees

    p0_leaves = graph.raw_leaves(graph.requirements(material_id, 1)) if role else {}
    return {
        "type_id": material_id,
        "name": info.name,
        "tier": tier,
        "role": role,
        "daily_units": float(daily),
        "unit_price": _money(sell),
        "gross_day": _money(gross),
        "input_cost_day": _money(input_cost),
        "cost_day": _money(customs_export + customs_import + fees + input_cost),
        "net_day": _money(net),
        "net_month": _money(net * 30),
        "isk_per_m3": _money((net / (Decimal(str(info.volume)) * daily)) if info.volume and daily else 0),
        "priced": provider.is_priced(material_id) and inputs_priced,
        # ``complexity`` is the CODE the badge rule below compares (``== "Low"``);
        # ``complexity_label`` is the translated half the template renders.
        "complexity": _complexity_for_tier(tier),
        "complexity_label": complexity_label(_complexity_for_tier(tier)),
        "needs_planets": graph.planet_cover(list(p0_leaves)),
        "raw_leaves": list(p0_leaves),
    }


def _explain(item: dict, planet_slugs) -> str:
    parts = []
    product = {"name": item["name"], "tier": item["tier"]}
    if item["role"] == "extract":
        parts.append(
            _("An extraction planet self-refining into %(name)s (%(tier)s)") % product
        )
    else:
        parts.append(
            _("A factory planet buying inputs to make %(name)s (%(tier)s)") % product
        )
    parts.append(
        _("nets about %(isk)s ISK/day per planet at current prices")
        % {"isk": f"{item['net_day']:,.0f}"}
    )
    if item["needs_planets"]:
        planets = ", ".join(item["needs_planets"])
        if planet_slugs and set(item["needs_planets"]) <= set(planet_slugs):
            parts.append(
                _("and you already have the planets for it (%(planets)s)")
                % {"planets": planets}
            )
        else:
            parts.append(_("if you can extract from %(planets)s") % {"planets": planets})
    return ", ".join(parts) + "."


def recommend(*, config, provider: PriceProvider | None = None, graph: PiGraph | None = None,
              planet_slugs=None, goal: str | None = None, limit: int = 6) -> list[dict]:
    """Ranked, badged product recommendations for a goal / available planets."""
    graph = graph or build_graph()
    provider = provider or PriceProvider(config.default_market_region_id)
    tiers = _GOAL_TIERS.get(goal or "max_profit", _GOAL_TIERS["max_profit"])

    candidates = [m for m in graph.materials.values() if m.tier in tiers]
    priority = set(config.recommended_products or [])

    scored = []
    for mat in candidates:
        est = estimate_product(graph, provider, config, mat.type_id)
        if est is None:
            continue
        # Feasibility: can these planets self-supply the raw leaves? (factories can buy.)
        feasible = True
        if planet_slugs:
            missing = graph.missing_inputs(mat.type_id, [
                r for slug in planet_slugs for r in graph.resources_by_planet.get(slug, [])
            ])
            feasible = not missing if est["role"] == "extract" else True
            est["feasible_with_your_planets"] = not missing
        est["explanation"] = _explain(est, planet_slugs)
        est["corp_priority"] = mat.type_id in priority
        scored.append((est, feasible))

    # Rank: priced first, then net/day. Feasible ones float up when planets given.
    scored.sort(key=lambda x: (x[1], x[0]["priced"], x[0]["net_day"]), reverse=True)
    top = [e for e, _feasible in scored[:limit]]

    # Badges — assigned after ranking so "best profit" is unique.
    for i, item in enumerate(top):
        badges = []
        if item["corp_priority"]:
            badges.append((_("Corp priority"), "cyan"))
        if i == 0 and item["net_day"] > 0:
            badges.append((_("Best profit"), "gold"))
        if item["role"] == "extract" and item["net_day"] > 0:
            badges.append((_("Low effort"), "kill"))
        if item["tier"] == "P1" and item["complexity"] == "Low" and item["net_day"] > 0:
            badges.append((_("Beginner recommended"), "win"))
        item["badges"] = badges
    return top
