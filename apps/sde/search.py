"""Type lookup helpers for member-facing pickers (autocomplete, resolve-by-name)."""
from __future__ import annotations

from .models import SdeBlueprintMaterial, SdeSolarSystem, SdeStation, SdeType


def search_stations(query: str, limit: int = 20) -> list[dict]:
    """NPC stations whose name contains ``query`` (for the freight location picker)."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    rows = list(
        SdeStation.objects.filter(name__icontains=query)
        .values("station_id", "name", "system_id", "system_name")[: limit * 4]
    )
    low = query.lower()
    rows.sort(key=lambda r: (not r["name"].lower().startswith(low), r["name"]))
    return rows[:limit]


def search_systems(query: str, limit: int = 20) -> list[dict]:
    """Solar systems whose name contains ``query`` (for the battle-report picker)."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    rows = list(
        SdeSolarSystem.objects.filter(name__icontains=query)
        .values("system_id", "name")[: limit * 4]
    )
    low = query.lower()
    rows.sort(key=lambda r: (not r["name"].lower().startswith(low), r["name"]))
    # Shape matches the type picker ({type_id,name}) so the same JS component works.
    return [{"type_id": r["system_id"], "name": r["name"]} for r in rows[:limit]]


def search_types(query: str, limit: int = 20, buildable_only: bool = False) -> list[dict]:
    """Published types whose name contains ``query`` (case-insensitive).

    ``buildable_only`` restricts to types that have a manufacturing or reaction
    recipe — used by the industry picker so members only choose things the corp
    can actually build.
    """
    query = (query or "").strip()
    if len(query) < 2:
        return []
    qs = SdeType.objects.filter(name__icontains=query, published=True)
    if buildable_only:
        product_ids = SdeBlueprintMaterial.objects.filter(
            activity__in=(SdeBlueprintMaterial.MANUFACTURING, SdeBlueprintMaterial.REACTION)
        ).values_list("product_type_id", flat=True)
        qs = qs.filter(type_id__in=product_ids)
    # Prefix matches first, then alphabetical — the intuitive autocomplete order.
    rows = list(qs.values("type_id", "name")[: limit * 4])
    low = query.lower()
    rows.sort(key=lambda r: (not r["name"].lower().startswith(low), r["name"]))
    return rows[:limit]


SHIP_CATEGORY_ID = 6  # EVE SDE category for ships


def search_ships(query: str, limit: int = 20) -> list[dict]:
    """Published *ship hulls* whose name contains ``query`` (custom-ship picker)."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    rows = list(
        SdeType.objects.filter(
            name__icontains=query, published=True, group__category_id=SHIP_CATEGORY_ID
        ).values("type_id", "name")[: limit * 4]
    )
    low = query.lower()
    rows.sort(key=lambda r: (not r["name"].lower().startswith(low), r["name"]))
    return rows[:limit]


SKILL_CATEGORY_ID = 16  # EVE SDE category for skills


def search_skills(query: str, limit: int = 20) -> list[dict]:
    """Published *skills* whose name contains ``query`` (the fleet-support picker)."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    rows = list(
        SdeType.objects.filter(
            name__icontains=query, published=True, group__category_id=SKILL_CATEGORY_ID
        ).values("type_id", "name")[: limit * 4]
    )
    low = query.lower()
    rows.sort(key=lambda r: (not r["name"].lower().startswith(low), r["name"]))
    return rows[:limit]


def resolve_type(name_or_id: str, buildable_only: bool = False) -> SdeType | None:
    """Resolve a picker submission (numeric type id, else exact name) to a type."""
    value = (name_or_id or "").strip()
    if not value:
        return None
    if value.isdigit():
        return SdeType.objects.filter(type_id=int(value)).first()
    return SdeType.objects.filter(name__iexact=value, published=True).first()
