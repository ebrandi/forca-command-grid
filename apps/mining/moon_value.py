"""Moon chunk value & composition estimate (4.13).

A moon extraction (``MoonExtraction``) carries only a timer — ESI never tells us what ore
the pending chunk holds. We ESTIMATE a structure's ore composition + value from its recent
mining LEDGER (what pilots have actually pulled at that refinery), valued at Jita per m³,
so miners can self-select the richest upcoming chunk. It is an estimate from observed
history, not a scan of the pending rock — the UI says so.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone


def structure_composition(structure_id: int, *, days: int = 90) -> dict | None:
    """Estimated ore composition + ISK/m³ for a moon structure, from its recent ledger.

    Returns ``None`` when we have no mining-ledger history for the structure (nothing to
    estimate from). The mining observer id is the refinery structure id, so it maps
    directly to ``MoonExtraction.structure_id``.
    """
    from apps.market.pricing import price_for
    from apps.sde.models import SdeType

    from .models import MiningLedgerEntry

    since = (timezone.now() - dt.timedelta(days=max(1, days))).date()
    agg = {
        r["type_id"]: r["qty"]
        for r in (
            MiningLedgerEntry.objects.filter(observer_id=structure_id, day__gte=since)
            .values("type_id").annotate(qty=Sum("quantity"))
        )
        if r["qty"]
    }
    if not agg:
        return None

    meta = {
        t["type_id"]: t
        for t in SdeType.objects.filter(type_id__in=list(agg)).values("type_id", "name", "volume")
    }
    rows: list[dict] = []
    total_value = Decimal("0")
    total_volume = 0.0
    priced_value = Decimal("0")  # value of ores that actually occupy m³ (review LOW-1)
    for tid, qty in agg.items():
        info = meta.get(tid, {})
        unit_vol = float(info.get("volume") or 0.0)
        value = price_for(tid) * qty
        volume = unit_vol * qty
        total_value += value
        total_volume += volume
        if unit_vol > 0:
            priced_value += value
        rows.append({"type_id": tid, "name": info.get("name") or f"Type {tid}",
                     "quantity": qty, "value": value, "volume": volume})
    if total_value <= 0:
        return None  # all-unpriced structure — no meaningful value estimate (review LOW-2)
    rows.sort(key=lambda r: -r["value"])
    for r in rows:
        r["value_share"] = round(float(r["value"] / total_value * 100), 1)
    # Only value backed by a real volume feeds the ratio, so a priced-but-volumeless ore
    # (partial-SDE anomaly) can't overstate ISK/m³.
    isk_per_m3 = (priced_value / Decimal(str(total_volume))) if total_volume > 0 else Decimal("0")
    return {
        "rows": rows[:6],
        "total_value": total_value,
        "total_volume": total_volume,
        "isk_per_m3": isk_per_m3.quantize(Decimal("0.01")),
        "days": days,
    }


def compositions_for_structures(structure_ids, *, days: int = 90) -> dict[int, dict]:
    """Batch: ``{structure_id: composition}`` for a set of structures (skips those with no
    ledger history). Used to annotate the extraction calendar without an N+1 explosion —
    one estimate per DISTINCT structure, shared across its extractions."""
    out: dict[int, dict] = {}
    for sid in set(structure_ids):
        comp = structure_composition(sid, days=days)
        if comp is not None:
            out[sid] = comp
    return out
