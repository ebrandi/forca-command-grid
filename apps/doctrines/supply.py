"""Doctrine supply chain (PRD Module M).

Turns "keep N complete sets of this doctrine in stock" into an executable plan:
per required type, target vs on-hand (corp stockpile + corp ESI assets), the
shortfall, and a per-line buy-vs-build recommendation (reusing the industry
BOM engine). Each shortfall can be fanned out into a claimable task.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum
from django.utils.translation import gettext as _

from apps.industry.bom import decide_build_or_buy
from apps.sde.models import SdeType

from .models import Doctrine


def _required_per_set(doctrine: Doctrine) -> dict[int, int]:
    """Max quantity per type across the doctrine's fits (hull + modules).

    Taking the max means stocking enough to build any of the doctrine's fits.
    """
    req: dict[int, int] = {}
    for fit in doctrine.fits.all():
        per_fit: dict[int, int] = {}
        if fit.ship_type_id:
            per_fit[fit.ship_type_id] = per_fit.get(fit.ship_type_id, 0) + 1
        for module in fit.modules or []:
            tid = module.get("type_id")
            if tid:
                per_fit[int(tid)] = per_fit.get(int(tid), 0) + int(module.get("quantity", 1) or 1)
        for tid, qty in per_fit.items():
            req[tid] = max(req.get(tid, 0), qty)
    return req


def corp_on_hand(type_ids) -> dict[int, int]:
    """On-hand per type: corp manual stockpile current + corp ESI assets."""
    from apps.stockpile.models import Asset, Stockpile, StockpileItem

    ids = list(type_ids)
    on_hand: dict[int, int] = {}
    for row in (
        StockpileItem.objects.filter(stockpile__kind=Stockpile.Kind.CORP, type_id__in=ids)
        .values("type_id")
        .annotate(q=Sum("quantity_current"))
    ):
        on_hand[row["type_id"]] = (on_hand.get(row["type_id"], 0) or 0) + (row["q"] or 0)
    for row in (
        Asset.objects.filter(owner_type=Asset.Owner.CORPORATION, type_id__in=ids)
        .values("type_id")
        .annotate(q=Sum("quantity"))
    ):
        on_hand[row["type_id"]] = (on_hand.get(row["type_id"], 0) or 0) + (row["q"] or 0)
    return on_hand


def supply_plan(doctrine: Doctrine, sets: int) -> dict:
    """Plan to keep ``sets`` complete sets of a doctrine in corp stock."""
    required = _required_per_set(doctrine)
    on_hand = corp_on_hand(required)
    names = dict(SdeType.objects.filter(type_id__in=list(required)).values_list("type_id", "name"))

    lines = []
    total_buy = Decimal("0")
    total_recommended = Decimal("0")
    for type_id, per_set in sorted(required.items(), key=lambda kv: names.get(kv[0], "")):
        target = per_set * sets
        have = int(on_hand.get(type_id, 0))
        need = max(target - have, 0)
        line = {
            "type_id": type_id,
            "name": names.get(type_id) or _("Type %(type_id)s") % {"type_id": type_id},
            "target": target,
            "have": have,
            "need": need,
        }
        if need > 0:
            decision = decide_build_or_buy(type_id, need)
            line.update(decision)
            total_buy += decision["buy_cost"] or Decimal("0")
            if decision["decision"] == "build" and decision["build_cost"] is not None:
                total_recommended += decision["build_cost"]
            else:
                total_recommended += decision["buy_cost"] or Decimal("0")
        lines.append(line)

    short = [line for line in lines if line["need"] > 0]
    return {
        "doctrine": doctrine,
        "sets": sets,
        "lines": lines,
        "short": short,
        "total_buy": total_buy,
        "total_recommended": total_recommended,
        "ready": not short,
    }


def corp_priority_list(sets: int = 10, limit: int = 30) -> list[dict]:
    """Aggregate shortfalls across all active doctrines, ranked by ISK to close.

    Batches the on-hand and name lookups across *all* doctrines into a handful of
    queries (previously ~3 per doctrine), and leans on the cached price/recipe
    lookups in ``decide_build_or_buy`` — so the whole aggregation is a few queries
    instead of thousands.
    """
    doctrines = list(
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .prefetch_related("fits")
        .order_by("-priority", "name")
    )
    per_doctrine = [(d, _required_per_set(d)) for d in doctrines]
    all_ids: set[int] = set()
    for _d, req in per_doctrine:
        all_ids |= set(req)
    if not all_ids:
        return []

    on_hand = corp_on_hand(all_ids)  # one batched stockpile + asset lookup
    names = dict(SdeType.objects.filter(type_id__in=list(all_ids)).values_list("type_id", "name"))

    agg: dict[int, dict] = {}
    for _doctrine, required in per_doctrine:
        for type_id, per_set in required.items():
            need = max(per_set * sets - int(on_hand.get(type_id, 0)), 0)
            if need <= 0:
                continue
            decision = decide_build_or_buy(type_id, need)  # cached price/recipe → no queries
            entry = agg.setdefault(
                type_id,
                {"type_id": type_id,
                 "name": names.get(type_id) or _("Type %(type_id)s") % {"type_id": type_id},
                 "need": 0, "cost": Decimal("0")},
            )
            entry["need"] += need
            entry["cost"] += decision["buy_cost"] or Decimal("0")
    rows = sorted(agg.values(), key=lambda r: -r["cost"])
    return rows[:limit]
