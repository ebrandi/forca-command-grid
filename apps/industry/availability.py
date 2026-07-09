"""Material availability: "do I already have these?" from the ESI asset mirror.

Thin, read-only helpers over :class:`apps.stockpile.models.Asset` (the ESI-synced
corp/character asset snapshot) plus the manual corp stockpile. Returns plain
``{type_id: quantity}`` maps the calculator/chain services net buy lists against.
No ESI calls happen here — it reads the already-synced mirror, so it's safe in the
request path.
"""
from __future__ import annotations

from django.db.models import Sum

from apps.stockpile.models import Asset


def _sum_assets(owner_type: str, owner_id: int, type_ids=None) -> dict[int, int]:
    qs = Asset.objects.filter(owner_type=owner_type, owner_id=owner_id)
    if type_ids is not None:
        qs = qs.filter(type_id__in=list(type_ids))
    rows = qs.values("type_id").annotate(total=Sum("quantity"))
    return {r["type_id"]: int(r["total"] or 0) for r in rows}


def character_on_hand(character_id: int, type_ids=None) -> dict[int, int]:
    """type_id -> quantity in a character's ESI-mirrored assets."""
    return _sum_assets(Asset.Owner.CHARACTER, character_id, type_ids)


def corp_on_hand(corp_id: int, type_ids=None) -> dict[int, int]:
    """type_id -> quantity in the corporation's ESI-mirrored assets."""
    return _sum_assets(Asset.Owner.CORPORATION, corp_id, type_ids)


def combined_on_hand(*, character_id: int | None = None, corp_id: int | None = None,
                     type_ids=None) -> dict[int, int]:
    """Union of character + corp assets (summed) for availability checks."""
    out: dict[int, int] = {}
    if character_id:
        for tid, qty in character_on_hand(character_id, type_ids).items():
            out[tid] = out.get(tid, 0) + qty
    if corp_id:
        for tid, qty in corp_on_hand(corp_id, type_ids).items():
            out[tid] = out.get(tid, 0) + qty
    return out
