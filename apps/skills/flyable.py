"""Which hulls a character's trained skills let them board.

Derived purely from the SDE (``SdeTypeSkill`` — every published ship's direct skill
requirements) and a skill snapshot. Deliberately NOT from assets: "can fly" is a statement
about the pilot, and it is the one question every recruiter and FC asks first.

A ship counts as flyable when every one of its listed requirements is trained to at least the
required level. Direct requirements suffice — a skill cannot be trained without its own
prerequisites, so meeting the listed ones implies the whole tree. Hulls with no requirement rows
(capsules, rookie ships, unpublished NPC hulls) are skipped so the list is not padded with noise.
"""
from __future__ import annotations

from collections import defaultdict

SHIP_CATEGORY_ID = 6


def _requirements() -> dict[int, tuple[str, str, list[tuple[int, int]]]]:
    """``{ship_type_id: (name, group_name, [(skill_id, level), …])}`` for every published hull
    with at least one requirement. One query per call (a few thousand rows); callers memoise
    the *result* per snapshot rather than this table, so an SDE re-import is picked up
    without a process restart."""
    from apps.sde.models import SdeTypeSkill

    rows = (
        SdeTypeSkill.objects.filter(
            type__group__category_id=SHIP_CATEGORY_ID, type__published=True
        )
        .values_list("type_id", "type__name", "type__group__name", "skill_type_id", "level")
        .order_by("type__group__name", "type__name")
    )
    out: dict[int, tuple[str, str, list[tuple[int, int]]]] = {}
    for type_id, name, group, skill_id, level in rows:
        entry = out.get(type_id)
        if entry is None:
            entry = (name, group or "", [])
            out[type_id] = entry
        entry[2].append((int(skill_id), int(level)))
    return out


def flyable_ships(snapshot) -> list[dict]:
    """``[{"group": str, "ships": [{"id", "name"}], "count": int}, …]`` for ``snapshot``.

    Groups (Frigate, Cruiser, …) are ordered alphabetically, ships alphabetically within them.
    Empty when there is no snapshot or nothing is flyable.
    """
    if snapshot is None or not snapshot.skills:
        return []
    trained: dict[int, int] = {}
    for sid, info in snapshot.skills.items():
        try:
            trained[int(sid)] = int((info or {}).get("trained_level", 0))
        except (TypeError, ValueError):
            continue
    groups: dict[str, list[dict]] = defaultdict(list)
    for type_id, (name, group, reqs) in _requirements().items():
        if all(trained.get(skill_id, 0) >= level for skill_id, level in reqs):
            groups[group].append({"id": type_id, "name": name})
    return [
        {"group": group, "ships": sorted(ships, key=lambda s: s["name"]), "count": len(ships)}
        for group, ships in sorted(groups.items())
    ]
