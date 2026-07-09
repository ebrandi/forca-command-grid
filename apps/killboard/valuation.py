"""Valuation & points engine (documented, versioned).

Suitable for corp-scale killboards. Item/hull prices come from
``apps.market.pricing.price_for`` (live Jita sell → CCP adjusted → 0; never SDE
base_price). Blueprint *copies* are valued at 0. Points reward solo/small-gang via
a blob penalty. See handbooks/contributor-handbook/architecture.md §7.
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from apps.market.pricing import price_for

from .models import KillmailItem

# A price source: type_id -> unit price. Defaults to the live ``price_for`` (one
# query per call); batch callers pass ``build_price_index()`` for an in-memory
# snapshot so re-valuing the whole board doesn't issue millions of queries.
PriceLookup = Callable[[int], Decimal]

VALUATION_VERSION = 2
POINTS_VERSION = 1

# ESI killmail item ``singleton`` value marking a Blueprint Copy. A BPC is not the
# blueprint as a tradeable asset: it has no market price, and pricing it like a
# BPO (or worse, from SDE base_price) inflated losses by absurd amounts — a cargo
# of T2 BPCs read as hundreds of billions. Copies are valued at 0.
BPC_SINGLETON = 2

__all__ = ["price_for", "compute_value", "compute_points", "apply_valuation"]


def _item_unit_value(item, price_lookup: PriceLookup = price_for) -> Decimal:
    """Per-unit value for a killmail item: 0 for blueprint copies, else market price."""
    if item.singleton == BPC_SINGLETON:
        return Decimal("0")
    return price_lookup(item.item_type_id)


def compute_value(killmail, price_lookup: PriceLookup = price_for) -> dict:
    """Compute destroyed/dropped/fitted/total ISK for a saved killmail."""
    destroyed = Decimal("0")
    dropped = Decimal("0")
    items = list(killmail.items.all())
    for item in items:
        unit = _item_unit_value(item, price_lookup)
        qd = item.quantity_destroyed or 0
        qdr = item.quantity_dropped or 0
        destroyed += unit * qd
        dropped += unit * qdr
        item.unit_value = unit  # refreshed every (re)valuation, not just first-seen
    if items:
        KillmailItem.objects.bulk_update(items, ["unit_value"])
    hull = price_lookup(killmail.victim_ship_type_id)
    total = destroyed + dropped + hull
    # Fitted value approximated as destroyed module/hull value at corp scale.
    fitted = destroyed
    return {
        "destroyed_value": destroyed + hull,
        "dropped_value": dropped,
        "fitted_value": fitted,
        "total_value": total,
    }


def compute_points(killmail) -> int:
    """Simplified, versioned points: base reduced by a quadratic blob penalty.

    points = max(1, round(base / blob_penalty)) where
    blob_penalty = max(1, n * max(1, n/2)) for n attackers.
    """
    base = 10
    # Only player attackers count toward the blob penalty (NPC/structure
    # co-attackers must not demote a genuine solo kill).
    n = killmail.participants.filter(role="attacker", character_id__isnull=False).count() or 1
    blob_penalty = max(1, n * max(1, n // 2))
    return max(1, round(base / blob_penalty))


def apply_valuation(killmail, price_lookup: PriceLookup = price_for) -> None:
    values = compute_value(killmail, price_lookup)
    killmail.destroyed_value = values["destroyed_value"]
    killmail.dropped_value = values["dropped_value"]
    killmail.fitted_value = values["fitted_value"]
    killmail.total_value = values["total_value"]
    killmail.points = compute_points(killmail)
    killmail.valuation_version = VALUATION_VERSION
    killmail.points_version = POINTS_VERSION
    killmail.save(
        update_fields=[
            "destroyed_value",
            "dropped_value",
            "fitted_value",
            "total_value",
            "points",
            "valuation_version",
            "points_version",
        ]
    )
