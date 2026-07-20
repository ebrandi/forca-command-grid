"""Extract a minimal, self-contained SDE graph slice for golden-fit tests.

    docker compose run --rm -T web python scripts/tochas_lab_extract_fixture.py \
        "Rifter" "150mm Light AutoCannon II" "Republic Fleet EMP S" > tests/fixtures/fitting/rifter_ac.json

Dumps, for the named types (plus every skill any of them requires, transitively, and
every skill referenced by their effects' modifiers): the SdeType/group/category rows,
type attributes, type effects, the dogma-effect definitions + modifier rows for those
effects, the dogma-attribute definitions for every attribute mentioned anywhere, and
required-skill rows. The slice is loaded verbatim by tests/_fitting_graph_utils.py.

Source: the live dev DB (imported through the normal pipeline from CCP's SDE). The
extracted values are CCP game data (see LICENSE notes in the repo docs); fixtures are
bounded slices used for validation only.
"""
import json
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from apps.sde.models import (  # noqa: E402
    SdeCategory, SdeDogmaAttribute, SdeDogmaEffect, SdeGroup, SdeModifier,
    SdeType, SdeTypeAttribute, SdeTypeEffect, SdeTypeSkill,
)

SKILL_LEVEL_SCALER_ATTRS = (275, 276, 280)


def main(names: list[str]) -> None:
    type_ids: set[int] = set()
    if names == ["--all-skills"]:
        # The shared skills-core slice: every category-16 skill with its dogma, so any
        # golden fixture + skills_core covers the full trained-skill effect surface.
        type_ids = set(SdeType.objects.filter(group__category_id=16)
                       .values_list("type_id", flat=True))
    for n in names:
        if n.startswith("--"):
            continue
        tid = SdeType.objects.filter(name__iexact=n).values_list("type_id", flat=True).first()
        if tid is None:
            raise SystemExit(f"type not found: {n}")
        type_ids.add(tid)

    # Transitive closure over required skills + skills referenced by modifiers.
    frontier = set(type_ids)
    while frontier:
        new: set[int] = set()
        for tid, sid in SdeTypeSkill.objects.filter(type_id__in=frontier).values_list(
                "type_id", "skill_type_id"):
            if sid not in type_ids:
                new.add(sid)
        effect_ids = set(SdeTypeEffect.objects.filter(type_id__in=frontier)
                         .values_list("effect_id", flat=True))
        mod_skills = SdeModifier.objects.filter(
            effect_id__in=effect_ids, skill_type_id__gt=0
        ).values_list("skill_type_id", flat=True)
        for sid in mod_skills:
            if sid not in type_ids:
                new.add(sid)
        type_ids |= new
        frontier = new

    effect_ids = set(SdeTypeEffect.objects.filter(type_id__in=type_ids)
                     .values_list("effect_id", flat=True))
    attr_ids = set(SdeTypeAttribute.objects.filter(type_id__in=type_ids)
                   .values_list("attribute_id", flat=True))
    for row in SdeModifier.objects.filter(effect_id__in=effect_ids).values(
            "modified_attribute_id", "modifying_attribute_id"):
        for v in row.values():
            if v:
                attr_ids.add(v)
    attr_ids.update(SKILL_LEVEL_SCALER_ATTRS)

    group_ids = set(SdeType.objects.filter(type_id__in=type_ids)
                    .values_list("group_id", flat=True))
    category_ids = set(SdeGroup.objects.filter(group_id__in=group_ids)
                       .values_list("category_id", flat=True))
    out = {
        "categories": list(SdeCategory.objects.filter(category_id__in=category_ids)
                           .values("category_id", "name")),
        "groups": list(SdeGroup.objects.filter(group_id__in=group_ids)
                       .values("group_id", "category_id", "name")),
        "types": list(SdeType.objects.filter(type_id__in=type_ids)
                      .values("type_id", "group_id", "name", "published", "mass")),
        "type_attributes": list(SdeTypeAttribute.objects.filter(type_id__in=type_ids)
                                .values("type_id", "attribute_id", "value")),
        "type_effects": list(SdeTypeEffect.objects.filter(type_id__in=type_ids)
                             .values("type_id", "effect_id", "is_default")),
        "type_skills": list(SdeTypeSkill.objects.filter(type_id__in=type_ids)
                            .values("type_id", "skill_type_id", "level")),
        "dogma_effects": list(SdeDogmaEffect.objects.filter(effect_id__in=effect_ids)
                              .values("effect_id", "name", "effect_category",
                                      "duration_attribute_id", "discharge_attribute_id",
                                      "range_attribute_id", "falloff_attribute_id",
                                      "tracking_attribute_id")),
        "modifiers": list(SdeModifier.objects.filter(effect_id__in=effect_ids)
                          .values("effect_id", "func", "domain", "operation",
                                  "modified_attribute_id", "modifying_attribute_id",
                                  "group_id", "skill_type_id")),
        "dogma_attributes": list(SdeDogmaAttribute.objects.filter(
            attribute_id__in=attr_ids).values(
            "attribute_id", "name", "stackable", "high_is_good", "default_value")),
    }
    json.dump(out, sys.stdout, indent=0, sort_keys=True)


if __name__ == "__main__":
    main(sys.argv[1:])
