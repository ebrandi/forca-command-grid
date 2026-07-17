"""Store pricing: value a doctrine fit or a made-to-order hull.

A ready-to-fly doctrine fit is priced as the sum of its hull and every fitted
module at the Jita sell price, times the doctrine markup. A made-to-order
sub-capital hull is the hull's Jita sell price times the hull markup. Capital and
supercapital hulls are never bought off the Jita market — they're manufactured to
order — so they are priced at the estimated production cost (EVE Ref's full job
cost, falling back to the local one-level material estimate) times the per-class
profit multiplier leaders set in the store settings. Jita paths surface the oldest
price timestamp in the basket so a buyer can see how fresh the valuation is.

Reads market/SDE data (plus the cached EVE Ref build-cost lookup for capital-class
hulls), writes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.utils.translation import gettext as _

from apps.market.pricing import price_maps
from apps.sde.models import SdeType

from .models import HullClass, PriceBasis

# SDE ship group ids that mark capital hulls (stable EVE static-data values).
SUPERCAP_GROUPS = {30, 659}  # Titan, Supercarrier
CAPITAL_GROUPS = {485, 547, 883, 1538, 4594} | SUPERCAP_GROUPS  # Dread, Carrier, Rorqual, FAX, Lancer
SHIP_CATEGORY_ID = 6


def _q(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _price_and_asof(type_id: int) -> tuple[Decimal, object | None]:
    """Best market price (live Jita sell → CCP adjusted → 0) and its Jita timestamp.

    Resolves through the shared ``price_maps`` snapshot that backs ``price_for`` —
    never ``SdeType.base_price``, which is an internal SDE figure wrong by orders of
    magnitude for whole item classes and would fabricate a made-to-order deposit
    against a bogus number. ``as_of`` is the live Jita timestamp when there is one
    (None when the value came from the daily CCP adjusted reference, matching the
    prior fallback's "freshness unknown" display).
    """
    maps = price_maps()
    value = maps["jita"].get(type_id)
    if value is None:
        value = maps["adjusted"].get(type_id)
    meta = maps["meta"].get(type_id)
    as_of = meta[1] if meta else None
    return (value if value is not None else Decimal("0")), as_of


def classify_hull(ship_type_id: int) -> str:
    """Sub-capital / capital / supercapital from the ship's SDE group."""
    group_id = (
        SdeType.objects.filter(type_id=ship_type_id).values_list("group_id", flat=True).first()
    )
    if group_id in SUPERCAP_GROUPS:
        return HullClass.SUPERCAPITAL
    if group_id in CAPITAL_GROUPS:
        return HullClass.CAPITAL
    return HullClass.SUBCAP


def is_ship(type_id: int) -> bool:
    """True if the type is a ship hull (so the hull picker only offers ships)."""
    return SdeType.objects.filter(
        type_id=type_id, group__category_id=SHIP_CATEGORY_ID, published=True
    ).exists()


@dataclass
class Priced:
    ok: bool
    ship_type_id: int = 0
    ship_name: str = ""
    hull_class: str = HullClass.SUBCAP
    manifest: list[dict] = field(default_factory=list)
    unit_jita: Decimal = Decimal("0")
    unit_price: Decimal = Decimal("0")
    markup: Decimal = Decimal("1")
    priced_as_of: object | None = None
    error: str = ""
    # What unit_price was computed from; unit_cost is the per-unit build estimate
    # when the basis is BUILD (0 on the Jita basis).
    price_basis: str = PriceBasis.JITA
    unit_cost: Decimal = Decimal("0")


def _fit_required_quantities(fit) -> dict[int, int]:
    """Hull + every fitted module/charge summed by type id."""
    req: dict[int, int] = {}
    if fit.ship_type_id:
        req[fit.ship_type_id] = req.get(fit.ship_type_id, 0) + 1
    for module in fit.modules or []:
        tid = module.get("type_id")
        if tid:
            req[int(tid)] = req.get(int(tid), 0) + int(module.get("quantity", 1) or 1)
    return req


def price_doctrine_fit(fit, markup: Decimal) -> Priced:
    """Value a full ready-to-fly doctrine fit at Jita sell × markup."""
    markup = Decimal(markup)
    req = _fit_required_quantities(fit)
    if not req:
        return Priced(False, error=_("That fit has no hull or modules to price."))
    names = dict(SdeType.objects.filter(type_id__in=list(req)).values_list("type_id", "name"))
    manifest: list[dict] = []
    jita_total = Decimal("0")
    oldest = None
    for type_id, qty in req.items():
        unit, as_of = _price_and_asof(type_id)
        if as_of is not None and (oldest is None or as_of < oldest):
            oldest = as_of
        jita_total += unit * qty
        manifest.append({
            "type_id": type_id, "name": names.get(type_id, f"Type {type_id}"),
            "quantity": qty, "unit_jita": str(_q(unit)),
        })
    jita_total = _q(jita_total)
    return Priced(
        ok=True,
        ship_type_id=fit.ship_type_id,
        ship_name=names.get(fit.ship_type_id, fit.name),
        hull_class=classify_hull(fit.ship_type_id),
        manifest=manifest,
        unit_jita=jita_total,
        unit_price=_q(jita_total * markup),
        markup=markup,
        priced_as_of=oldest,
    )


def price_doctrine_fits_bulk(fits, markup) -> dict[int, tuple[Decimal, Decimal]]:
    """Price many doctrine fits with two batched queries → ``{fit_id: (unit_price, unit_jita)}``.

    Equivalent to calling :func:`price_doctrine_fit` once per fit — same live Jita sell →
    CCP adjusted → 0 resolution and the same ``_q`` quantisation, so displayed prices match
    the single-fit pricer exactly — and a **constant** number of queries regardless of how
    many fits: the shared ``price_maps`` snapshot loads once (and is usually already warm
    from the dashboard/killboard), then every lookup is an O(1) dict read. The Shipyard
    prices 200+ fits at once, where a per-fit path was ~4k queries / ~45 s (a 504).
    Fits with no hull/modules are omitted (mirrors ``price_doctrine_fit``'s ``ok=False``).
    """
    markup = Decimal(markup)
    req_by_fit = {f.id: _fit_required_quantities(f) for f in fits}
    all_ids: set[int] = set().union(*req_by_fit.values()) if req_by_fit else set()
    if not all_ids:
        return {}
    # Resolve through the same price_for snapshot as _price_and_asof (live Jita sell →
    # CCP adjusted → 0); never SDE base_price, so a fabricated number can never reach a
    # store quote or a made-to-order deposit.
    maps = price_maps()
    jita, adjusted = maps["jita"], maps["adjusted"]
    price_map: dict[int, Decimal] = {}
    for tid in all_ids:
        value = jita.get(tid)
        if value is None:
            value = adjusted.get(tid)
        if value is not None:
            price_map[tid] = value

    out: dict[int, tuple[Decimal, Decimal]] = {}
    for fid, req in req_by_fit.items():
        if not req:
            continue
        jita_total = _q(
            sum((price_map.get(tid, Decimal("0")) * qty for tid, qty in req.items()), Decimal("0"))
        )
        out[fid] = (_q(jita_total * markup), jita_total)
    return out


def price_hull(ship_type_id: int, markup: Decimal) -> Priced:
    """Value a bare hull at Jita sell × markup."""
    markup = Decimal(markup)
    sde = SdeType.objects.filter(type_id=ship_type_id).first()
    if not sde:
        return Priced(False, error=_("Unknown ship type."))
    if not is_ship(ship_type_id):
        return Priced(False, error=_("That isn't a ship hull."))
    unit, as_of = _price_and_asof(ship_type_id)
    jita = _q(unit)
    return Priced(
        ok=True,
        ship_type_id=ship_type_id,
        ship_name=sde.name,
        hull_class=classify_hull(ship_type_id),
        manifest=[{"type_id": ship_type_id, "name": sde.name, "quantity": 1, "unit_jita": str(jita)}],
        unit_jita=jita,
        unit_price=_q(jita * markup),
        markup=markup,
        priced_as_of=as_of,
    )


def production_cost_detail(ship_type_id: int) -> dict | None:
    """Estimated per-unit manufacturing cost WITH its provenance, or ``None``.

    Returns ``{"cost": Decimal, "source": "everef" | "estimate"}``. EVE Ref's full job
    cost first (ME-adjusted materials + install fee; 12h-cached, circuit-broken —
    source ``everef``), falling back to the local one-level material estimate off the
    SDE blueprint when the API can't answer (source ``estimate``). ``None`` when nothing
    can price it — the drift check treats that as ``basis_source="unknown"`` and flags
    nothing (missing data is not zero drift). Imports are lazy to mirror how the industry
    helpers are consumed elsewhere (forecast) and keep this module import-light.
    """
    from apps.industry.bom import build_cost
    from apps.industry.everef_cost import manufacturing_cost_per_unit

    cost = manufacturing_cost_per_unit(ship_type_id)
    if cost is not None:
        return {"cost": cost, "source": "everef"}
    local = build_cost(ship_type_id)
    if local is not None:
        return {"cost": Decimal(local), "source": "estimate"}
    return None


def production_cost_per_unit(ship_type_id: int) -> Decimal | None:
    """Estimated cost to manufacture one unit, or ``None`` when nothing can price it.

    Thin wrapper over :func:`production_cost_detail` — the provenance-free number the
    made-to-order hull pricer freezes. Zero behaviour change for existing callers.
    """
    detail = production_cost_detail(ship_type_id)
    return detail["cost"] if detail is not None else None


def price_hull_order(ship_type_id: int, cfg) -> Priced:
    """Price a made-to-order hull under the store's per-class policy.

    Sub-capitals keep the classic rule: Jita sell × ``cfg.hull_markup``. Capital and
    supercapital hulls are priced at the estimated build cost × the class multiplier
    (``cfg.capital_markup`` / ``cfg.supercap_markup``). When no build-cost source can
    answer, the order is refused rather than silently quoted off a market reference
    that can be wildly wrong for hulls that never trade in Jita.
    """
    hull_class = classify_hull(ship_type_id)
    if hull_class == HullClass.SUBCAP:
        return price_hull(ship_type_id, cfg.hull_markup)

    sde = SdeType.objects.filter(type_id=ship_type_id).first()
    if not sde:
        return Priced(False, error=_("Unknown ship type."))
    if not is_ship(ship_type_id):
        return Priced(False, error=_("That isn't a ship hull."))
    cost = production_cost_per_unit(ship_type_id)
    # <= 0 is as bogus as None: the local estimate sums price_for() over the blueprint
    # materials, and price_for returns 0 for every type missing from the market
    # snapshot — a cold or partially-synced snapshot must refuse, never freeze a
    # 0.00-ISK (or underquoted) capital order with a 0.00 deposit on the board.
    if cost is None or cost <= 0:
        return Priced(
            False, ship_type_id=ship_type_id, ship_name=sde.name, hull_class=hull_class,
            error=_("No build-cost estimate is available for that hull right now — try again later."),
        )
    cost = _q(cost)
    markup = Decimal(cfg.markup_for_hull(hull_class))
    unit, as_of = _price_and_asof(ship_type_id)  # Jita reference only, never the basis
    return Priced(
        ok=True,
        ship_type_id=ship_type_id,
        ship_name=sde.name,
        hull_class=hull_class,
        manifest=[{"type_id": ship_type_id, "name": sde.name, "quantity": 1, "unit_jita": str(_q(unit))}],
        unit_jita=_q(unit),
        unit_price=_q(cost * markup),
        markup=markup,
        priced_as_of=as_of,
        price_basis=PriceBasis.BUILD,
        unit_cost=cost,
    )
