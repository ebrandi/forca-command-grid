"""Store pricing: value a doctrine fit or a bare hull off the Jita sell price.

A ready-to-fly doctrine fit is priced as the sum of its hull and every fitted
module at the Jita sell price, times the doctrine markup. A made-to-order hull is
the hull's Jita sell price times the hull markup. Both surface the oldest price
timestamp in the basket so a buyer can see how fresh the valuation is.

Pure: reads market/SDE data, writes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from apps.market.pricing import price_maps
from apps.sde.models import SdeType

from .models import HullClass

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
        return Priced(False, error="That fit has no hull or modules to price.")
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
        return Priced(False, error="Unknown ship type.")
    if not is_ship(ship_type_id):
        return Priced(False, error="That isn't a ship hull.")
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
