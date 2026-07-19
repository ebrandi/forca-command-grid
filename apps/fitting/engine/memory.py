"""In-memory :class:`~apps.fitting.engine.dogma.DataProvider`.

Backs the engine with a plain dict of type data — no database. Used by the engine
test-suite (with original, hand-authored fixture data) and available for a future
offline "sandbox" mode. The ORM-backed provider lives in
:mod:`apps.fitting.engine.adapter`.
"""
from __future__ import annotations

from .bonuses import BonusSpec


class MemoryDataProvider:
    """``types[type_id] = {name, group_id, category_id, attrs:{attr_id:value}, skills:[(sid,lvl)]}``."""

    def __init__(self, types: dict[int, dict], data_version: str = "memory"):
        self._types = types
        self._ship_bonuses: dict[int, list[BonusSpec]] = {}
        self.data_version = data_version

    def add_ship_bonus(self, ship_type_id: int, spec: BonusSpec) -> None:
        self._ship_bonuses.setdefault(ship_type_id, []).append(spec)

    def type_info(self, type_id: int) -> dict | None:
        t = self._types.get(type_id)
        if not t:
            return None
        return {
            "name": t.get("name", f"Type {type_id}"),
            "group_id": t.get("group_id"),
            "category_id": t.get("category_id"),
        }

    def attrs(self, type_id: int) -> dict[int, float]:
        return dict(self._types.get(type_id, {}).get("attrs", {}))

    def effects(self, type_id: int) -> frozenset[int]:
        return frozenset(self._types.get(type_id, {}).get("effects", ()))

    def required_skills(self, type_id: int) -> list[tuple[int, int]]:
        return list(self._types.get(type_id, {}).get("skills", []))

    def ship_bonuses(self, ship_type_id: int) -> list[BonusSpec]:
        return list(self._ship_bonuses.get(ship_type_id, []))
