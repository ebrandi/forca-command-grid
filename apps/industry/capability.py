"""Per-character manufacturing capability — can this pilot *build* a product?

Checks the imported blueprint **manufacturing**-skill requirements
(:class:`SdeBlueprintSkill`, distinct from the fly-it skills in ``SdeTypeSkill``)
against a character's latest skill snapshot. Returns ``None`` when the requirements
aren't known (blueprint-skill data not imported yet) so callers never *guess* a pilot
can build something — the honest-data rule.
"""
from __future__ import annotations


def manufacturing_skill_requirements(product_type_id: int, activity: str = "manufacturing") -> list[tuple[int, int]]:
    """``[(skill_type_id, level), …]`` to run the activity that yields ``product_type_id``."""
    from apps.sde.models import SdeBlueprintSkill

    return list(
        SdeBlueprintSkill.objects.filter(product_type_id=product_type_id, activity=activity)
        .values_list("skill_type_id", "level")
    )


def can_manufacture(snapshot, product_type_id: int, activity: str = "manufacturing") -> bool | None:
    """Whether the pilot can build the product.

    ``True``/``False`` once requirements are known; ``None`` if the blueprint's
    manufacturing skills haven't been imported (don't claim capability on missing data).
    """
    reqs = manufacturing_skill_requirements(product_type_id, activity)
    if not reqs:
        return None
    if snapshot is None:
        return False
    return all(snapshot.trained_level(skill_id) >= level for skill_id, level in reqs)
