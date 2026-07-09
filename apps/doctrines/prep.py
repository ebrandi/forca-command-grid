"""Pilot pre-fleet preparation: can I fly it, what am I missing, what to buy.

Turns a doctrine fit into a personal, actionable checklist for one pilot:
the skill verdict (reusing the readiness engine), a shopping list diffed
against what the pilot already owns (personal ESI assets, when linked), an
estimated cost, and an in-game multibuy string. Everything here is the pilot's
own data (PRD §II.5.5).
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum

from apps.market.pricing import price_for
from apps.sde.models import SdeType

from .services import character_readiness


def owned_by_type(char_ids) -> dict[int, int]:
    """Total quantity per type the pilot owns across their characters (ESI mirror)."""
    from apps.stockpile.models import Asset

    rows = (
        Asset.objects.filter(owner_type=Asset.Owner.CHARACTER, owner_id__in=list(char_ids))
        .values("type_id")
        .annotate(q=Sum("quantity"))
    )
    return {r["type_id"]: r["q"] for r in rows}


def _required_quantities(fit) -> dict[int, int]:
    """Hull + every fitted module/charge, summed by type id."""
    req: dict[int, int] = {}
    if fit.ship_type_id:
        req[fit.ship_type_id] = req.get(fit.ship_type_id, 0) + 1
    for module in fit.modules or []:
        tid = module.get("type_id")
        if tid:
            req[int(tid)] = req.get(int(tid), 0) + int(module.get("quantity", 1) or 1)
    return req


def fit_shopping(character, fit, owned: dict[int, int] | None = None) -> dict:
    """Per-fit preparation: skill verdict + diffed shopping list + cost."""
    owned = owned or {}
    req = _required_quantities(fit)
    names = dict(SdeType.objects.filter(type_id__in=list(req)).values_list("type_id", "name"))

    lines = []
    missing_cost = Decimal("0")
    for type_id, need_qty in sorted(req.items(), key=lambda kv: names.get(kv[0], "")):
        have = int(owned.get(type_id, 0))
        short = max(need_qty - have, 0)
        unit_price = price_for(type_id)
        line_cost = unit_price * short
        missing_cost += line_cost
        lines.append(
            {
                "type_id": type_id,
                "name": names.get(type_id, f"Type {type_id}"),
                "need": need_qty,
                "have": have,
                "short": short,
                "unit_price": unit_price,
                "line_cost": line_cost,
            }
        )

    readiness = character_readiness(character, fit)
    short_lines = [line for line in lines if line["short"] > 0]
    return {
        "fit": fit,
        "readiness": readiness,
        "lines": lines,
        "short_lines": short_lines,
        "missing_cost": missing_cost,
        "multibuy": "\n".join(f"{line['name']} {line['short']}" for line in short_lines),
        "all_owned": not short_lines,
    }


def doctrine_prep(character, doctrine, char_ids=None) -> list[dict]:
    """Preparation for every fit in a doctrine, for one pilot."""
    owned = owned_by_type(char_ids) if char_ids else {}
    return [fit_shopping(character, fit, owned) for fit in doctrine.fits.all()]
