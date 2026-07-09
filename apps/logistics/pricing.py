"""Freight quote engine — the standard EVE courier pricing model.

A quote is built from per-warp rates, the freighter long-route bump, collateral
multiplier tiers, the jump-freighter additive model, a rush surcharge and a
minimum reward — then multiplied by the rate card's price multiplier so leadership
can set their margin in one place. The structure matches what haulers across New
Eden already expect, so quotes are easy to compare.

Pure functions: no DB writes, no ESI. Callers pass a RateCard and route facts.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .models import RateCard, ShipClass

ISK = Decimal


def _q(value) -> Decimal:
    return Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


# Collateral multiplier tiers applied to the jump cost, per ship class.
# (ceiling_isk, multiplier); the last tier is the cap.
_FREIGHTER_COLLATERAL_TIERS = [
    (1_500_000_000, Decimal("1")),
    (3_000_000_000, Decimal("2")),
    (5_000_000_000, Decimal("4")),
]
_DST_COLLATERAL_TIERS = [
    (1_500_000_000, Decimal("1")),
    (2_000_000_000, Decimal("2")),
    (2_500_000_000, Decimal("2.5")),
    (5_000_000_000, Decimal("5")),
    (10_000_000_000, Decimal("7")),
]
# JF collateral is additive (flat fee on top of the route cost), not a multiplier.
_JF_COLLATERAL_TIERS = [
    (5_000_000_000, 0),
    (10_000_000_000, 50_000_000),
    (15_000_000_000, 150_000_000),
    (20_000_000_000, 250_000_000),
    (30_000_000_000, 450_000_000),
    (40_000_000_000, 650_000_000),
    (50_000_000_000, 850_000_000),
]


@dataclass(frozen=True)
class Quote:
    ok: bool
    reward: Decimal
    breakdown: dict
    error: str = ""


def _collateral_multiplier(tiers, collateral: Decimal) -> Decimal:
    for ceiling, mult in tiers:
        if collateral <= ceiling:
            return mult
    return tiers[-1][1]


def _jf_collateral_fee(collateral: Decimal) -> int:
    for ceiling, fee in _JF_COLLATERAL_TIERS:
        if collateral <= ceiling:
            return fee
    return _JF_COLLATERAL_TIERS[-1][1]


def caps_for(card: RateCard, ship_class: str) -> tuple[int, int]:
    """(max_volume_m3, max_collateral_isk) for a ship class."""
    if ship_class == ShipClass.DST:
        return card.dst_max_m3, card.dst_max_collateral
    if ship_class == ShipClass.JF:
        return card.jf_max_m3, card.jf_max_collateral
    return card.freighter_max_m3, card.freighter_max_collateral


def quote(
    card: RateCard,
    *,
    ship_class: str,
    jumps: int,
    lowsec_jumps: int = 0,
    jump_hops: int | None = None,
    volume_m3: float,
    collateral,
    sec_band: str = "highsec",
    rush: bool = False,
) -> Quote:
    """Price one courier job. Returns a Quote with a transparent breakdown."""
    collateral = ISK(collateral or 0)
    volume_m3 = float(volume_m3 or 0)
    jumps = max(int(jumps or 0), 0)

    max_m3, max_coll = caps_for(card, ship_class)
    if volume_m3 <= 0 or jumps <= 0:
        return Quote(False, ISK(0), {}, "Enter a route (jumps) and a volume.")
    if volume_m3 > max_m3:
        return Quote(
            False, ISK(0), {},
            f"Volume {volume_m3:,.0f} m³ exceeds the {max_m3:,} m³ limit for this ship class.",
        )
    if collateral > max_coll:
        return Quote(False, ISK(0), {}, f"Collateral exceeds the {max_coll/1e9:,.0f}b limit for this ship class.")

    discount = ISK(card.discount)
    is_lowsec = sec_band in ("lowsec", "nullsec")
    lines: list[dict] = []

    if ship_class == ShipClass.JF:
        # Cyno jumps come from the proximity-graph route; fall back to the manual
        # jump count when no graphed route was supplied (Option E in the research).
        hops = jump_hops if jump_hops is not None else lowsec_jumps
        hops = max(int(hops or 0), 0)
        base = card.jf_base
        per_jump = card.jf_per_jump * hops
        coll_fee = _jf_collateral_fee(collateral)
        rush_fee = card.rush_fee_jf if rush else 0
        gross = base + per_jump + coll_fee + rush_fee
        lines = [
            {"label": "Jump freighter base", "isk": base},
            {"label": f"Cyno jumps × {hops}", "isk": per_jump},
            {"label": "Collateral fee", "isk": coll_fee},
        ]
    else:
        warps = jumps + 1
        if ship_class == ShipClass.DST:
            rate = card.dst_lowsec_rate_per_warp if is_lowsec else card.dst_rate_per_warp
            mult = _collateral_multiplier(_DST_COLLATERAL_TIERS, collateral)
        else:  # freighter
            rate = (
                card.freighter_rate_per_warp_long
                if warps >= card.freighter_long_threshold
                else card.freighter_rate_per_warp
            )
            mult = _collateral_multiplier(_FREIGHTER_COLLATERAL_TIERS, collateral)
        jump_cost = ISK(rate * warps)
        with_coll = jump_cost * mult
        rush_fee = card.rush_fee_hs if rush else 0
        gross = with_coll + ISK(rush_fee)
        lines = [
            {"label": f"Route · {warps} warps", "isk": int(jump_cost)},
            {"label": f"Collateral cover ×{mult.normalize()}", "isk": int(with_coll - jump_cost)},
        ]

    if rush:
        lines.append({"label": "Rush priority", "isk": int(rush_fee)})

    gross = ISK(gross)
    discounted = _q(gross * discount)
    reward = max(discounted, ISK(card.min_reward))
    min_applied = reward != discounted

    # Show the customer line items that reconcile to the final reward: scale each
    # component by the price multiplier and absorb rounding in the last line.
    # (No competitor framing — the customer just sees what makes up their reward.)
    visible = [ln for ln in lines if ln["isk"]]
    display: list[dict] = []
    if min_applied:
        display = [{"label": "Minimum reward", "isk": int(reward)}]
    else:
        acc = 0
        for i, ln in enumerate(visible):
            if i < len(visible) - 1:
                v = int(_q(ISK(ln["isk"]) * discount))
                display.append({"label": ln["label"], "isk": v})
                acc += v
            else:
                display.append({"label": ln["label"], "isk": int(discounted) - acc})

    breakdown = {
        "ship_class": ship_class,
        "jumps": jumps,
        "lowsec_jumps": lowsec_jumps,
        "jump_hops": (jump_hops if jump_hops is not None else lowsec_jumps) if ship_class == ShipClass.JF else 0,
        "sec_band": sec_band,
        "rush": rush,
        "lines": display,
        "base_price": int(gross),  # internal record (pre-multiplier); never shown
        "multiplier": float(discount),
        "min_reward_applied": min_applied,
        "reward": int(reward),
    }
    return Quote(True, reward, breakdown)
