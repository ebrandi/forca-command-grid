"""Seed a doctrine fit from a killmail's fitting + cargo.

A loss already lists everything that was aboard — fitted modules *and* whatever
sat in the cargo hold (ammo, scripts, cap boosters …). For a doctrine we want all
of it (the consumables are part of flying the doctrine), so this reconstructs an
EFT block from the killmail that a director then fine-tunes before saving.
"""
from __future__ import annotations

from apps.sde.models import SdeType


def _name(type_id: int, names: dict[int, str]) -> str:
    return names.get(type_id) or f"TypeID:{type_id}"


def eft_from_killmail(killmail) -> str:
    """Reconstruct an EFT block (ship header + one line per item, quantities
    summed across destroyed + dropped) from a killmail's items.

    The output is intentionally a flat module list — the EFT parser doesn't need
    slots — so the director can edit it freely (add missing ammo, drop wrecked
    rigs, rename) on the fine-tune page before the doctrine is created.
    """
    items = list(killmail.items.all())
    type_ids = {killmail.victim_ship_type_id}
    type_ids.update(it.item_type_id for it in items)
    names = dict(
        SdeType.objects.filter(type_id__in=type_ids).values_list("type_id", "name")
    )

    ship_name = _name(killmail.victim_ship_type_id, names)
    lines = [f"[{ship_name}, {ship_name}]"]

    agg: dict[int, int] = {}
    order: list[int] = []
    for it in items:
        qty = (it.quantity_destroyed or 0) + (it.quantity_dropped or 0)
        if qty <= 0:
            continue
        if it.item_type_id not in agg:
            order.append(it.item_type_id)
        agg[it.item_type_id] = agg.get(it.item_type_id, 0) + qty

    for tid in order:
        qty = agg[tid]
        name = _name(tid, names)
        lines.append(f"{name} x{qty}" if qty > 1 else name)
    return "\n".join(lines)
