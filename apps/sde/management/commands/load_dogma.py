"""Load dogma reference data (attributes/effects/type-attributes/ship-bonuses).

Usage:
    manage.py load_dogma [--file path/to/dogma.json] [--dogma-version 20260718]

The JSON is a relational projection of the CCP Static Data Export FSD dogma files —
``dogmaAttributes.yaml`` (attribute definitions), ``dogmaEffects.yaml`` (effect
definitions + modifier graph), ``typeDogma.yaml`` (per-type attribute values + effects)
— plus a FORCA-authored ``ship_bonuses`` projection of hull trait bonuses. This is the
same authoritative CCP SDE the rest of the import consumes; nothing here derives from a
third-party fitting engine's generated data. See
docs/architecture/decisions/tochas-lab-fitting-engine.md.

Keys: ``dogma_attributes``, ``dogma_effects``, ``type_attributes``, ``type_effects``,
``ship_bonuses``. Defaults to the bundled dev sample. The import is idempotent and, for
the per-type tables, staged (delete-then-load inside one transaction) so a partial run
never leaves the fitting engine reading half-updated data (§25).
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.admin_audit.models import AppSetting
from apps.sde.models import (
    SdeDogmaAttribute,
    SdeDogmaEffect,
    SdeShipBonus,
    SdeTypeAttribute,
    SdeTypeEffect,
)

_DEFAULT = Path(__file__).resolve().parent.parent.parent / "fixtures" / "dogma_sample.json"


class Command(BaseCommand):
    help = "Load dogma reference data (attributes/effects/type values/ship bonuses) from JSON."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--file", default=str(_DEFAULT))
        parser.add_argument("--dogma-version", dest="dogma_version", default="")

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        data = json.loads(Path(options["file"]).read_text())

        for a in data.get("dogma_attributes", []):
            SdeDogmaAttribute.objects.update_or_create(
                attribute_id=a["attribute_id"],
                defaults={
                    "name": a.get("name", ""),
                    "display_name": a.get("display_name", ""),
                    "unit_id": a.get("unit_id"),
                    "stackable": a.get("stackable", True),
                    "high_is_good": a.get("high_is_good"),
                    "default_value": a.get("default_value", 0.0),
                    "published": a.get("published", True),
                },
            )
        for e in data.get("dogma_effects", []):
            SdeDogmaEffect.objects.update_or_create(
                effect_id=e["effect_id"],
                defaults={
                    "name": e.get("name", ""),
                    "effect_category": e.get("effect_category", 0),
                    "is_offensive": e.get("is_offensive", False),
                    "is_assistance": e.get("is_assistance", False),
                    "discharge_attribute_id": e.get("discharge_attribute_id"),
                    "duration_attribute_id": e.get("duration_attribute_id"),
                    "range_attribute_id": e.get("range_attribute_id"),
                    "falloff_attribute_id": e.get("falloff_attribute_id"),
                    "tracking_attribute_id": e.get("tracking_attribute_id"),
                    "modifier_info": e.get("modifier_info", []),
                },
            )

        # Per-type tables: staged replace, scoped to the types present in this payload so a
        # partial/targeted import never wipes unrelated types.
        type_attr_rows = data.get("type_attributes", [])
        type_eff_rows = data.get("type_effects", [])
        bonus_rows = data.get("ship_bonuses", [])
        touched_types = {r["type_id"] for r in type_attr_rows} | {r["type_id"] for r in type_eff_rows}
        touched_ships = {r["ship_type_id"] for r in bonus_rows}

        SdeTypeAttribute.objects.filter(type_id__in=touched_types).delete()
        SdeTypeEffect.objects.filter(type_id__in=touched_types).delete()
        SdeShipBonus.objects.filter(ship_type_id__in=touched_ships).delete()

        SdeTypeAttribute.objects.bulk_create([
            SdeTypeAttribute(type_id=r["type_id"], attribute_id=r["attribute_id"],
                             value=r["value"])
            for r in type_attr_rows
        ], batch_size=2000, ignore_conflicts=True)
        SdeTypeEffect.objects.bulk_create([
            SdeTypeEffect(type_id=r["type_id"], effect_id=r["effect_id"],
                          is_default=r.get("is_default", False))
            for r in type_eff_rows
        ], batch_size=2000, ignore_conflicts=True)
        SdeShipBonus.objects.bulk_create([
            SdeShipBonus(
                ship_type_id=r["ship_type_id"], key=r["key"],
                target_attribute_id=r["target_attribute_id"], amount=r.get("amount", 0.0),
                per_level=r.get("per_level", False), skill_type_id=r.get("skill_type_id"),
                target_domain=r.get("target_domain", "item"),
                match_group_ids=r.get("match_group_ids", []),
                match_category_ids=r.get("match_category_ids", []),
                match_attr_present=r.get("match_attr_present"),
                penalised=r.get("penalised", False), label=r.get("label", ""),
            )
            for r in bonus_rows
        ], batch_size=2000, ignore_conflicts=True)

        version = options["dogma_version"] or timezone.now().strftime("%Y%m%d%H%M%S")
        AppSetting.objects.update_or_create(
            key="dogma_data_version", defaults={"value": {"version": version}}
        )
        self.stdout.write(self.style.SUCCESS(
            f"Loaded dogma: {SdeDogmaAttribute.objects.count()} attributes, "
            f"{SdeDogmaEffect.objects.count()} effects, "
            f"{SdeTypeAttribute.objects.count()} type-attributes, "
            f"{SdeShipBonus.objects.count()} ship bonuses (version {version})."
        ))
