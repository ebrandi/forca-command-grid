"""Map a ship hull to a broad class (frigate / cruiser / …) for filtering.

EVE's SDE splits ships into many fine-grained groups (Assault Frigate, Heavy
Assault Cruiser, Logistics …). Pilots want to filter by the broad hull size, so we
fold those groups into a handful of familiar classes. Group ids are stable EVE
static-data values.
"""
from __future__ import annotations

from apps.sde.models import SdeType

# Broad class → the SDE ship group ids that belong to it.
_CLASS_GROUPS: dict[str, set[int]] = {
    "Frigate": {25, 324, 830, 831, 834, 893, 1283, 1527, 237, 31},
    "Destroyer": {420, 541, 1305, 1534},
    "Cruiser": {26, 358, 832, 894, 906, 963, 833, 1972},
    "Battlecruiser": {419, 540, 1201},
    "Battleship": {27, 898, 900},
    "Industrial": {28, 380, 463, 543, 941, 1202},
    "Freighter": {513, 902},
    "Capital": {547, 485, 1538, 883, 659, 30, 4594, 4595},
}
_GROUP_TO_CLASS: dict[int, str] = {
    gid: cls for cls, gids in _CLASS_GROUPS.items() for gid in gids
}

# Display order for the filter chips.
CLASS_ORDER = [
    "Frigate", "Destroyer", "Cruiser", "Battlecruiser", "Battleship",
    "Industrial", "Capital", "Freighter", "Other",
]


def hull_class_for_group(group_id: int | None) -> str:
    return _GROUP_TO_CLASS.get(group_id or 0, "Other")


def hull_meta(ship_type_ids) -> dict[int, dict]:
    """``{type_id: {group, group_name, hull_class}}`` for a batch of hulls."""
    rows = (
        SdeType.objects.filter(type_id__in=list(ship_type_ids))
        .values_list("type_id", "group_id", "group__name")
    )
    out: dict[int, dict] = {}
    for type_id, group_id, group_name in rows:
        out[type_id] = {
            "group_id": group_id,
            "group_name": group_name or "",
            "hull_class": hull_class_for_group(group_id),
        }
    return out
