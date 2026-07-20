"""Load extracted SDE graph slices (tests/fixtures/fitting/*.json) into the test DB.

The slices are produced by scripts/tochas_lab_extract_fixture.py from a live import of
CCP's SDE through the normal pipeline — real game data, bounded to the types each
golden fit needs, so the golden tests exercise the REAL ORM provider and engine path.
Expected values in the golden tests are hand-derived from the base attributes in the
slice plus documented EVE mechanics — never read back from the engine.
"""
from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "fitting"


def load_graph_fixture(name: str, with_skills: bool = True) -> dict:
    """Insert the named slice (plus the shared skills_core slice by default);
    returns {type name -> type_id} for convenience."""
    if with_skills and name != "skills_core":
        _load_one("skills_core")
    return _load_one(name)


def _load_one(name: str) -> dict:
    from apps.admin_audit.models import AppSetting
    from apps.sde.models import (
        SdeCategory, SdeDogmaAttribute, SdeDogmaEffect, SdeGroup, SdeModifier,
        SdeType, SdeTypeAttribute, SdeTypeEffect, SdeTypeSkill,
    )

    data = json.loads((FIXTURES / f"{name}.json").read_text())
    for row in data["categories"]:
        SdeCategory.objects.get_or_create(category_id=row["category_id"],
                                          defaults={"name": row["name"]})
    for row in data["groups"]:
        SdeGroup.objects.get_or_create(group_id=row["group_id"],
                                       defaults={"category_id": row["category_id"],
                                                 "name": row["name"]})
    for row in data["types"]:
        SdeType.objects.get_or_create(type_id=row["type_id"], defaults={
            "group_id": row["group_id"], "name": row["name"],
            "published": row.get("published", True), "mass": row.get("mass")})
    SdeTypeAttribute.objects.bulk_create(
        [SdeTypeAttribute(type_id=r["type_id"], attribute_id=r["attribute_id"],
                          value=r["value"]) for r in data["type_attributes"]],
        ignore_conflicts=True)
    SdeTypeEffect.objects.bulk_create(
        [SdeTypeEffect(type_id=r["type_id"], effect_id=r["effect_id"],
                       is_default=r["is_default"]) for r in data["type_effects"]],
        ignore_conflicts=True)
    SdeTypeSkill.objects.bulk_create(
        [SdeTypeSkill(type_id=r["type_id"], skill_type_id=r["skill_type_id"],
                      level=r["level"]) for r in data["type_skills"]],
        ignore_conflicts=True)
    for r in data["dogma_effects"]:
        # update_or_create: the slice's real CCP values must win over any sample rows
        # earlier tests/migrations may have seeded (e.g. wrong effect categories).
        SdeDogmaEffect.objects.update_or_create(effect_id=r["effect_id"], defaults={
            "name": r["name"], "effect_category": r["effect_category"],
            "duration_attribute_id": r["duration_attribute_id"],
            "discharge_attribute_id": r["discharge_attribute_id"],
            "range_attribute_id": r["range_attribute_id"],
            "falloff_attribute_id": r["falloff_attribute_id"],
            "tracking_attribute_id": r["tracking_attribute_id"]})
    # SdeModifier has no natural unique key — skip effects already loaded (the shared
    # skills_core slice overlaps golden slices), or shared effects would apply twice.
    fixture_effects = {r["effect_id"] for r in data["modifiers"]}
    already = set(SdeModifier.objects.filter(effect_id__in=fixture_effects)
                  .values_list("effect_id", flat=True))
    SdeModifier.objects.bulk_create(
        [SdeModifier(effect_id=r["effect_id"], func=r["func"], domain=r["domain"],
                     operation=r["operation"],
                     modified_attribute_id=r["modified_attribute_id"],
                     modifying_attribute_id=r["modifying_attribute_id"],
                     group_id=r["group_id"], skill_type_id=r["skill_type_id"])
         for r in data["modifiers"] if r["effect_id"] not in already])
    for r in data["dogma_attributes"]:
        # update_or_create: stackable/high_is_good/default drive the maths — a stale
        # sample row with stackable=True silently disables every stacking penalty.
        SdeDogmaAttribute.objects.update_or_create(attribute_id=r["attribute_id"], defaults={
            "name": r["name"], "stackable": r["stackable"],
            "high_is_good": r["high_is_good"], "default_value": r["default_value"]})
    AppSetting.objects.update_or_create(
        key="dogma_data_version", defaults={"value": {"version": f"golden-{name}"}})
    return {row["name"]: row["type_id"] for row in data["types"]}


def evaluate_fit(ship_type_id, modules, skills=None, op=None):
    """Evaluate through the PRODUCTION path (ORM provider + engine v2)."""
    from apps.fitting.engine.adapter import FittingEngine
    from apps.fitting.engine.types import FitInput, OperatingProfile, SkillProfile

    engine = FittingEngine()
    return engine.evaluate(FitInput(ship_type_id=ship_type_id, modules=tuple(modules)),
                           skills or SkillProfile.omniscient(),
                           op or OperatingProfile(propulsion_active=False))
