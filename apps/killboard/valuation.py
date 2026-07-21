"""Valuation & points engine (documented, versioned).

Suitable for corp-scale killboards. Item/hull prices come from
``apps.market.pricing.price_for`` (live Jita sell → CCP adjusted → 0; never SDE
base_price). Blueprint *copies* are valued at 0. Points reward solo/small-gang via
a blob penalty. See handbooks/contributor-handbook/architecture.md §7.
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from django.db.models import DecimalField, F
from django.db.models.functions import Coalesce

from apps.market.pricing import price_for

from .models import KillmailItem

# A price source: type_id -> unit price. Defaults to the live ``price_for`` (one
# query per call); batch callers pass ``build_price_index()`` for an in-memory
# snapshot so re-valuing the whole board doesn't issue millions of queries.
PriceLookup = Callable[[int], Decimal]

# VALUATION_VERSION 3 (KB-35): valuation is now split into "then" and "now". ``total_value``
# (and the destroyed/dropped/fitted figures) track the *live* market and are refreshed by the
# daily re-value; ``value_at_kill`` captures the total at the prices *on the day the ship died*
# and is written once (ingest / backfill), never by the re-value. Rankings read the at-kill
# value where known (see ``at_kill_value_expr``) so a pilot's ISK-destroyed/lost is fair
# regardless of later price moves.
VALUATION_VERSION = 3
POINTS_VERSION = 1

# ESI killmail item ``singleton`` value marking a Blueprint Copy. A BPC is not the
# blueprint as a tradeable asset: it has no market price, and pricing it like a
# BPO (or worse, from SDE base_price) inflated losses by absurd amounts — a cargo
# of T2 BPCs read as hundreds of billions. Copies are valued at 0.
BPC_SINGLETON = 2

__all__ = [
    "price_for", "compute_value", "compute_points", "apply_valuation",
    "at_kill_value_expr", "stamp_value_at_kill", "at_kill_destroyed_value",
    "at_kill_hull_value",
]


def at_kill_value_expr(prefix: str = ""):
    """``Coalesce(value_at_kill, total_value)`` — the fair "value" for rankings.

    Prefer the price on the day of the kill; fall back to the live total for mails not
    yet backfilled (``value_at_kill`` NULL). ``prefix`` reaches the field across a join
    (e.g. ``"killmail__"`` from a participant row).
    """
    return Coalesce(
        F(f"{prefix}value_at_kill"), F(f"{prefix}total_value"),
        output_field=DecimalField(max_digits=20, decimal_places=2),
    )


def _item_unit_value(item, price_lookup: PriceLookup = price_for) -> Decimal:
    """Per-unit value for a killmail item: 0 for blueprint copies, else market price."""
    if item.singleton == BPC_SINGLETON:
        return Decimal("0")
    return price_lookup(item.item_type_id)


def compute_value(
    killmail, price_lookup: PriceLookup = price_for, *, persist_items: bool = True
) -> dict:
    """Compute destroyed/dropped/fitted/total ISK for a saved killmail.

    ``persist_items=False`` computes the figures WITHOUT writing each item's ``unit_value``
    — used by the at-kill and lazy "value now" paths, which must not clobber the stored
    live per-item breakdown shown on the detail page.
    """
    destroyed = Decimal("0")
    dropped = Decimal("0")
    items = list(killmail.items.all())
    for item in items:
        unit = _item_unit_value(item, price_lookup)
        qd = item.quantity_destroyed or 0
        qdr = item.quantity_dropped or 0
        destroyed += unit * qd
        dropped += unit * qdr
        if persist_items:
            item.unit_value = unit  # refreshed every (re)valuation, not just first-seen
    if items and persist_items:
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
    # NB: value_at_kill / value_source are deliberately NOT written here. This function is the
    # daily "value now" re-value; the at-kill figure is stamped once by ``stamp_value_at_kill``
    # (ingest / backfill) and must survive every later re-value.
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


def stamp_value_at_kill(killmail, *, historical: bool = False, fetch: bool | None = None) -> str:
    """Set ``value_at_kill`` + ``value_source`` for one killmail. Returns the source label.

    ``historical=False`` (ingest of a fresh mail): at-kill ≈ live, so the current live
    total is captured cheaply with source ``live`` — no network, no re-pricing.

    ``historical=True`` (backfill of old mails): re-price every item and the hull at the
    market on the kill date via :class:`apps.market.historical.HistoricalPriceLookup`
    (EVE Ref day history + oracle routing for high-value/PLEX), and label the killmail with
    the dominant source. ``fetch`` forwards to the lookup (``False`` = read local history
    only, never download).
    """
    from apps.market.historical import SOURCE_LIVE, HistoricalPriceLookup

    if not historical:
        killmail.value_at_kill = killmail.total_value
        killmail.value_source = SOURCE_LIVE
        killmail.save(update_fields=["value_at_kill", "value_source"])
        return SOURCE_LIVE

    lookup = HistoricalPriceLookup(killmail.killmail_time, fetch=fetch)
    values = compute_value(killmail, lookup, persist_items=False)
    killmail.value_at_kill = values["total_value"]
    killmail.value_source = lookup.dominant_source()
    killmail.save(update_fields=["value_at_kill", "value_source"])
    return killmail.value_source


def at_kill_hull_value(killmail, *, fetch: bool | None = None) -> Decimal:
    """The hull's price on the kill date (SRP ``at_kill`` basis, hull-only programmes)."""
    from apps.market.historical import price_at

    return price_at(killmail.victim_ship_type_id, killmail.killmail_time, fetch=fetch).amount


def at_kill_destroyed_value(killmail, *, fetch: bool | None = None) -> Decimal:
    """Hull + destroyed modules priced on the kill date (SRP ``at_kill`` actual-loss basis).

    Mirrors ``compute_value``'s destroyed figure (BPCs zeroed) but at period-accurate prices,
    computed on demand — SRP claims are low-volume, so a per-claim historical re-price is fine.
    """
    from apps.market.historical import HistoricalPriceLookup

    lookup = HistoricalPriceLookup(killmail.killmail_time, fetch=fetch)
    return compute_value(killmail, lookup, persist_items=False)["destroyed_value"]
