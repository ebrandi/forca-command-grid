"""Turn a pasted item list into a priced appraisal.

Accepts what the EVE client copies: multibuy lines ("Tritanium 100",
"Tritanium\t100", "Tritanium x100") and inventory paste (tab-separated, quantity
in the second column). Each line is matched to a type, valued at the Jita sell
price, and the location haircut is applied to produce the offer.

Pure logic: reads market/SDE data, writes nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from apps.market.models import MarketPrice
from apps.sde.models import SdeType

# Bounds on pasted input — a paste is a person's hangar, not a megabyte. These
# cap the work done per request (DB lookups) and, with the per-line cap, make the
# quantity regex immune to catastrophic backtracking (ReDoS).
_MAX_INPUT = 50_000   # characters accepted from the textarea
_MAX_LINES = 500      # lines parsed
_MAX_LINE = 200       # characters considered per line

# Ore mode reprocesses only the Asteroid category (raw ore + ice ores) — not manufactured
# items that also have reprocessing yields (ships/modules/ammo). See appraise().
_ORE_CATEGORY_IDS = {25}

# Quantity is digit-led with no internal whitespace, so this can't blow up the
# way an overlapping ``[\s]+ … [\d.,\s]+`` pattern would on a long space run.
_QTY_TAIL = re.compile(r"^(.+?)\s+x?(\d[\d.,]*)$", re.IGNORECASE)


# A single stack in EVE never approaches this; capping the parsed quantity keeps a
# pasted line like "Tritanium 9999…(190 digits)" from producing a Decimal that
# overflows the offer/total columns (a 500 on submit). The submit view applies the
# authoritative column-fit guard; this just keeps the appraisal maths sane.
_MAX_QTY = 10**12


def _to_int(token: str) -> int | None:
    digits = re.sub(r"[^\d]", "", token or "")
    if not digits:
        return None
    try:
        return min(int(digits), _MAX_QTY)
    except ValueError:
        return None


def parse_lines(text: str) -> list[tuple[str, int]]:
    """Extract (name, quantity) pairs from pasted text, merging duplicates."""
    merged: dict[str, int] = {}
    order: list[str] = []
    for raw in (text or "")[:_MAX_INPUT].splitlines()[:_MAX_LINES]:
        line = raw.strip()[:_MAX_LINE]
        if not line:
            continue
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
            name = parts[0]
            qty = next((_to_int(p) for p in parts[1:] if _to_int(p)), None) or 1
        else:
            m = _QTY_TAIL.match(line)
            qty = _to_int(m.group(2)) if m else None
            if m and qty:
                name = m.group(1).strip()
            else:
                name, qty = line, 1
        key = name.lower()
        if key not in merged:
            merged[key] = 0
            order.append(name)
        merged[key] += qty
    return [(name, merged[name.lower()]) for name in order]


@dataclass
class AppraisalLine:
    name: str
    type_id: int
    quantity: int
    unit_jita: Decimal
    line_jita: Decimal
    line_offer: Decimal
    volume: float
    # How the line was valued: "jita" (own sell price) or "reprocessed" (ore mode — valued
    # by its refined mineral output). Purely informational for the UI.
    basis: str = "jita"


@dataclass
class Appraisal:
    sec_band: str
    rate: Decimal
    lines: list[AppraisalLine] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    jita_total: Decimal = Decimal("0")
    offer_total: Decimal = Decimal("0")
    volume_m3: float = 0.0
    # Oldest market-price timestamp across priced items (None if none had a live
    # market price — they fell back to the SDE base price).
    priced_as_of: object | None = None

    @property
    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)

    def manifest(self) -> list[dict]:
        return [
            {
                "type_id": line.type_id,
                "name": line.name,
                "quantity": line.quantity,
                "unit_jita": str(line.unit_jita),
                "line_offer": str(line.line_offer),
                "basis": line.basis,
            }
            for line in self.lines
        ]


def _q(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _price_and_asof(type_id: int) -> tuple[Decimal, object | None]:
    """Jita sell price and when it was recorded; SDE base price (no timestamp) as
    a fallback so an un-traded item still gets a sane value."""
    mp = (
        MarketPrice.objects.filter(type_id=type_id, profile=MarketPrice.Profile.JITA_SELL)
        .order_by("-as_of")
        .first()
    )
    if mp and mp.sell_min is not None:
        return Decimal(mp.sell_min), mp.as_of
    sde = SdeType.objects.filter(type_id=type_id).first()
    if sde and sde.base_price is not None:
        return Decimal(sde.base_price), None
    return Decimal("0"), None


def appraise(text: str, *, sec_band: str, rate: Decimal, ore_mode: bool = False,
             reprocessing_pct: Decimal = Decimal("0.906")) -> Appraisal:
    """Price a pasted item list at ``rate`` × Jita sell for the given location.

    In ``ore_mode`` a reprocessable type (ore/ice/scrap — one that has reprocessing
    materials) is valued by its **refined mineral output** instead of the ore's own sell
    price: ``(Σ mineral_qty × mineral_sell) / portion_size × reprocessing_pct`` per unit.
    Non-reprocessable lines are always valued at Jita sell. ``reprocessing_pct`` is the
    corp's effective refine yield (skills + structure + rig)."""
    rate = Decimal(rate)
    # Defensive clamp: the only write path (ConfigForm) already bounds this to [0,1], but a
    # yield >1 would over-value ore and <0 would break it — never trust it blindly.
    reprocessing_pct = min(max(Decimal(reprocessing_pct), Decimal("0")), Decimal("1"))
    result = Appraisal(sec_band=sec_band, rate=rate)
    price_memo: dict[int, tuple[Decimal, object | None]] = {}

    def _price(type_id: int) -> tuple[Decimal, object | None]:
        if type_id not in price_memo:
            price_memo[type_id] = _price_and_asof(type_id)
        return price_memo[type_id]

    for name, qty in parse_lines(text):
        sde = (
            SdeType.objects.filter(name__iexact=name).select_related("group").first()
            or SdeType.objects.filter(name__iexact=name.strip()).select_related("group").first()
        )
        if not sde:
            result.unknown.append(name)
            continue
        basis = "jita"
        # Ore mode reprocesses ONLY ore/ice (the Asteroid category) — otherwise every
        # reprocessable module/ship in a mixed paste would be mis-valued by its mineral
        # content instead of its market price (review MED-1). The select_related keeps the
        # category gate query-free.
        reprocess = (
            list(sde.reprocess_materials.all())
            if ore_mode and sde.group.category_id in _ORE_CATEGORY_IDS
            else []
        )
        if reprocess:
            # Value by refined minerals: mineral output per portion × yield, per unit.
            portion = sde.portion_size or 1
            per_portion = Decimal("0")
            as_of = None
            for mat in reprocess:
                mprice, mas_of = _price(mat.material_type_id)
                per_portion += mprice * mat.quantity
                if mas_of is not None and (as_of is None or mas_of < as_of):
                    as_of = mas_of
            unit = (per_portion / portion) * reprocessing_pct
            basis = "reprocessed"
        else:
            unit, as_of = _price(sde.type_id)
        if as_of is not None and (result.priced_as_of is None or as_of < result.priced_as_of):
            result.priced_as_of = as_of
        line_jita = _q(unit * qty)
        line_offer = _q(line_jita * rate)
        volume = (sde.volume or 0.0) * qty
        result.lines.append(AppraisalLine(
            name=sde.name, type_id=sde.type_id, quantity=qty,
            unit_jita=_q(unit), line_jita=line_jita, line_offer=line_offer, volume=volume,
            basis=basis,
        ))
        result.jita_total += line_jita
        result.offer_total += line_offer
        result.volume_m3 += volume
    result.jita_total = _q(result.jita_total)
    result.offer_total = _q(result.offer_total)
    return result
