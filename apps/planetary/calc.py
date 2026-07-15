"""Profit calculation for PI plans (Journey 5 / the calculator).

Design principle: the *chain, price, tax and hauling maths are exact*; the *physical
throughput is a labelled estimate* the pilot can override. Every assumption is
surfaced in the result so nobody mistakes an estimate for a promise.

Costing model (transparent, per-planet):
* Extraction planets export what they refine — their P0 feedstock is self-extracted
  (no input cost), and daily output derives from the extraction-rate assumption.
* Factory planets buy their inputs at market and export the higher tier — daily
  output is a labelled per-tier estimate unless the pilot enters a real number.
* Customs (POCO) tax is charged on the CCP base value, per the configured %.
* Selling on the market adds sales tax + broker fee; corp buyback pays a % of Jita.

Nothing here is persisted as truth: ``plan_economics`` returns a plain dict the caller
may snapshot, but it is always recomputed from live prices.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.utils.translation import gettext as _

from .chains import PiGraph, build_graph
from .constants import DEFAULT_FACTORY_OUTPUT_PER_DAY, TIER_ORDER
from .models import PiExportStrategy, PiPlanetRole
from .prices import PriceProvider

_CENTS = Decimal("0.01")


def _money(value) -> float:
    return float(Decimal(value).quantize(_CENTS, rounding=ROUND_HALF_UP))


def _pct(value) -> Decimal:
    return Decimal(str(value)) / Decimal(100)


def _dec(value) -> Decimal:
    """Coerce to Decimal. Model DecimalField defaults are floats in memory until a
    DB round-trip, so a freshly-built plan can carry floats — normalise them here."""
    return Decimal(str(value))


# --------------------------------------------------------------------------- #
# Per-planet output estimate
# --------------------------------------------------------------------------- #
def estimate_daily_output(plan, planet, graph: PiGraph) -> tuple[Decimal, str]:
    """(units/day, basis). Basis ∈ override | extraction | factory_estimate | none."""
    if planet.output_override:
        return Decimal(planet.output_override), "override"
    mat = planet.primary_material
    if mat is None:
        return Decimal(0), "none"
    info = graph.material(mat.type_id)
    tier = info.tier if info else mat.tier
    rate = Decimal(plan.extraction_rate_per_hour)

    if planet.role == PiPlanetRole.EXTRACT:
        if tier == "P0":
            return rate * 24, "extraction"
        if tier == "P1":
            sch = graph.schematic_for(mat.type_id)
            if sch and sch.inputs:
                in_qty = next(iter(sch.inputs.values()))
                p0_per_day = rate * 24
                return (p0_per_day / Decimal(in_qty) * Decimal(sch.output_qty)), "extraction"
    return Decimal(DEFAULT_FACTORY_OUTPUT_PER_DAY.get(tier, 0)), "factory_estimate"


# --------------------------------------------------------------------------- #
# Per-planet economics
# --------------------------------------------------------------------------- #
def planet_economics(plan, planet, graph: PiGraph, provider: PriceProvider) -> dict:
    mat = planet.primary_material
    daily, basis = estimate_daily_output(plan, planet, graph)
    row = {
        "planet_type": planet.planet_type.name,
        "planet_slug": planet.planet_type.slug,
        "role": planet.role,
        "role_label": planet.get_role_display(),
        "product_id": mat.type_id if mat else None,
        "product": mat.name if mat else "—",
        "tier": (graph.material(mat.type_id).tier if mat and graph.material(mat.type_id) else
                 (mat.tier if mat else "")),
        "daily_units": float(daily),
        "basis": basis,
        "priced": True,
        "gross_day": 0.0, "input_cost_day": 0.0, "customs_day": 0.0,
        "hauling_day": 0.0, "fees_day": 0.0, "net_day": 0.0,
        # ``warnings`` is translated prose (rendered); ``missing_prices`` is the parallel
        # machine-readable list of unpriced item names — the aggregate below keys off this,
        # never off the (localised) warning text.
        "unit_price": 0.0, "warnings": [], "missing_prices": [],
    }
    if mat is None:
        row["warnings"].append(_("No product selected for this planet."))
        return row

    unit_price = provider.sell(mat.type_id)
    row["unit_price"] = _money(unit_price)
    row["priced"] = provider.is_priced(mat.type_id)
    if not row["priced"]:
        row["missing_prices"].append(mat.name)
        row["warnings"].append(
            _("No market price for %(product)s — revenue shown as 0.") % {"product": mat.name})

    gross = unit_price * daily
    strategy = plan.export_strategy
    fees = Decimal(0)
    if strategy == PiExportStrategy.CORP_BUYBACK:
        gross = gross * _pct(plan.corp_buyback_rate)
    elif strategy in (PiExportStrategy.SELL_LOCAL, PiExportStrategy.HAUL_HUB):
        fees = gross * _pct(plan.sales_tax + plan.broker_fee)
    # FEED_CHAIN: value is internal — no market fees, gross shown as market reference.

    # Factory planets buy their inputs; extraction planets self-supply.
    input_cost = Decimal(0)
    customs_import = Decimal(0)
    if planet.role == PiPlanetRole.FACTORY:
        sch = graph.schematic_for(mat.type_id)
        if sch and sch.output_qty:
            runs = daily / Decimal(sch.output_qty)
            for in_id, in_qty in sch.inputs.items():
                units = Decimal(in_qty) * runs
                input_cost += provider.sell(in_id) * units
                customs_import += provider.base_value(in_id) * units * _pct(plan.customs_import_tax)
                if not provider.is_priced(in_id):
                    imat = graph.material(in_id)
                    in_name = imat.name if imat else str(in_id)
                    row["missing_prices"].append(in_name)
                    row["warnings"].append(
                        _("No price for input %(input)s.") % {"input": in_name})

    customs_export = provider.base_value(mat.type_id) * daily * _pct(plan.customs_export_tax)
    info = graph.material(mat.type_id)
    volume = Decimal(str(info.volume if info else 0)) * daily
    hauling = volume * _dec(plan.hauling_cost_per_m3) if strategy == PiExportStrategy.HAUL_HUB else Decimal(0)

    net = gross - input_cost - customs_export - customs_import - hauling - fees
    row.update({
        "gross_day": _money(gross),
        "input_cost_day": _money(input_cost),
        "customs_day": _money(customs_export + customs_import),
        "hauling_day": _money(hauling),
        "fees_day": _money(fees),
        "net_day": _money(net),
        "volume_day": float(volume),
    })
    return row


# --------------------------------------------------------------------------- #
# Whole-plan economics
# --------------------------------------------------------------------------- #
def plan_economics(plan, graph: PiGraph | None = None, provider: PriceProvider | None = None) -> dict:
    graph = graph or build_graph()
    provider = provider or PriceProvider(plan.market_region_id)
    planets = list(plan.planets.select_related("planet_type", "primary_material"))

    rows = [planet_economics(plan, p, graph, provider) for p in planets]

    gross = sum(Decimal(str(r["gross_day"])) for r in rows)
    input_cost = sum(Decimal(str(r["input_cost_day"])) for r in rows)
    customs = sum(Decimal(str(r["customs_day"])) for r in rows)
    hauling = sum(Decimal(str(r["hauling_day"])) for r in rows)
    fees = sum(Decimal(str(r["fees_day"])) for r in rows)
    net = sum(Decimal(str(r["net_day"])) for r in rows)

    n_planets = len(rows) or 1
    # Detect missing prices via the machine-readable per-row list — NEVER by substring-
    # matching the (now-localised) warning prose, which would silently break for every
    # non-English pilot. The names are EVE item nouns and stay raw.
    missing = sorted({name for r in rows for name in r["missing_prices"]})

    warnings: list[str] = []
    if not planets:
        warnings.append(_("Add at least one planet to estimate profit."))
    if missing:
        warnings.append(_("Some products or inputs are unpriced — profit is understated. "
                          "Refresh prices, or the daily market sync will fill them in."))
    if net < 0 and planets:
        warnings.append(_("This setup is currently unprofitable at these prices and taxes."))
    total_tax_pct = float(plan.customs_export_tax) + float(plan.sales_tax) + float(plan.broker_fee)
    if total_tax_pct >= 15:
        warnings.append(
            _("High tax load (%(pct)s%% before hauling) — check your customs standings "
              "and whether hauling to a cheaper hub helps.") % {"pct": f"{total_tax_pct:.0f}"})

    bases = {r["basis"] for r in rows}
    assumptions = [
        _("Prices from %(region)s; customs %(export)s%% export / %(import)s%% import; "
          "sales %(sales)s%% + broker %(broker)s%%.") % {
              "region": plan.market_region_name,
              "export": plan.customs_export_tax,
              "import": plan.customs_import_tax,
              "sales": plan.sales_tax,
              "broker": plan.broker_fee,
          },
    ]
    if "extraction" in bases:
        assumptions.append(
            _("Extraction planets assume a steady %(rate)s/hour per planet — tune this or "
              "enter your real output for accuracy.") % {"rate": f"{plan.extraction_rate_per_hour:,}"})
    if "factory_estimate" in bases:
        assumptions.append(
            _("Factory output uses a per-tier planning estimate; enter your measured units/day "
              "on each factory planet to refine it."))

    return {
        "region": {"id": plan.market_region_id, "name": plan.market_region_name},
        "planets": rows,
        "totals": {
            "gross_day": _money(gross),
            "input_cost_day": _money(input_cost),
            "customs_day": _money(customs),
            "hauling_day": _money(hauling),
            "fees_day": _money(fees),
            "tax_burden_day": _money(customs + fees),
            "net_day": _money(net),
            "net_week": _money(net * 7),
            "net_month": _money(net * 30),
            "net_per_planet_day": _money(net / n_planets),
        },
        "complexity": _complexity(rows),
        "confidence": _confidence(rows, bool(missing)),
        "warnings": warnings,
        "assumptions": assumptions,
        "missing_prices": missing,
    }


def _complexity(rows: list[dict]) -> str:
    if not rows:
        return "—"
    max_tier = max((TIER_ORDER.index(r["tier"]) for r in rows if r.get("tier") in TIER_ORDER),
                   default=0)
    factories = sum(1 for r in rows if r["role"] == PiPlanetRole.FACTORY)
    score = len(rows) + max_tier + factories
    return "Low" if score <= 3 else "Medium" if score <= 7 else "High"


def _confidence(rows: list[dict], has_missing: bool) -> str:
    if not rows or has_missing:
        return "Low"
    if any(r["basis"] == "override" for r in rows):
        return "High"
    return "Medium"


# --------------------------------------------------------------------------- #
# Sell-vs-refine comparison (buy-vs-build for one more tier)
# --------------------------------------------------------------------------- #
def refine_vs_sell(type_id: int, provider: PriceProvider, graph: PiGraph) -> dict | None:
    """Is it worth refining one more tier, or selling the inputs as-is?

    Compares, per cycle: the market value of the inputs vs the market value of the
    output they'd become. Exact and price-driven (no throughput assumption).
    """
    sch = graph.schematic_for(int(type_id))
    if sch is None:
        return None
    out = graph.material(sch.output_id)
    input_rows = []
    input_value = Decimal(0)
    for in_id, qty in sch.inputs.items():
        imat = graph.material(in_id)
        unit = provider.sell(in_id)
        input_value += unit * qty
        input_rows.append({
            "type_id": in_id, "name": imat.name if imat else str(in_id),
            "tier": imat.tier if imat else "", "quantity": qty,
            "unit_price": _money(unit), "value": _money(unit * qty),
            "priced": provider.is_priced(in_id),
        })
    output_value = provider.sell(sch.output_id) * sch.output_qty
    delta = output_value - input_value
    priced = provider.is_priced(sch.output_id) and all(r["priced"] for r in input_rows)
    return {
        "output": {"type_id": sch.output_id, "name": out.name if out else "",
                   "tier": out.tier if out else "", "quantity": sch.output_qty,
                   "unit_price": _money(provider.sell(sch.output_id))},
        "inputs": input_rows,
        "input_value": _money(input_value),
        "output_value": _money(output_value),
        "delta": _money(delta),
        "better": "refine" if delta > 0 else "sell_inputs",
        "priced": priced,
        "margin_pct": (float(delta / input_value * 100) if input_value > 0 else None),
    }
