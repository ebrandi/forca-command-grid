"""Load SDE reference data from a JSON file into the sde tables.

Usage:
    manage.py load_sde [--file path/to/sde.json]

The JSON has keys: categories, groups, types, regions, systems,
type_skills, blueprint_materials. Defaults to a bundled sample suitable for
development and tests. For production, point --file at a converted SDE export.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.admin_audit.models import AppSetting
from apps.sde.models import (
    SdeBlueprintActivityTime,
    SdeBlueprintMaterial,
    SdeBlueprintSkill,
    SdeCategory,
    SdeConstellation,
    SdeDecryptor,
    SdeGroup,
    SdeInventionProduct,
    SdeRegion,
    SdeSolarSystem,
    SdeSystemJump,
    SdeType,
    SdeTypeSkill,
)

_DEFAULT = Path(__file__).resolve().parent.parent.parent / "fixtures" / "sde_sample.json"


class Command(BaseCommand):
    help = "Load SDE reference data from JSON."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--file", default=str(_DEFAULT))
        parser.add_argument("--sde-version", dest="sde_version", default="")

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        path = Path(options["file"])
        data = json.loads(path.read_text())

        for c in data.get("categories", []):
            SdeCategory.objects.update_or_create(
                category_id=c["category_id"], defaults={"name": c["name"]}
            )
        for g in data.get("groups", []):
            SdeGroup.objects.update_or_create(
                group_id=g["group_id"],
                defaults={"name": g["name"], "category_id": g["category_id"]},
            )
        for t in data.get("types", []):
            SdeType.objects.update_or_create(
                type_id=t["type_id"],
                defaults={
                    "name": t["name"],
                    "group_id": t["group_id"],
                    "volume": t.get("volume", 0.0),
                    "base_price": t.get("base_price"),
                    "published": t.get("published", True),
                    "rank": t.get("rank"),
                },
            )
        for r in data.get("regions", []):
            SdeRegion.objects.update_or_create(
                region_id=r["region_id"], defaults={"name": r["name"]}
            )
        for c in data.get("constellations", []):
            SdeConstellation.objects.update_or_create(
                constellation_id=c["constellation_id"],
                defaults={"name": c["name"], "region_id": c["region_id"]},
            )
        for s in data.get("systems", []):
            SdeSolarSystem.objects.update_or_create(
                system_id=s["system_id"],
                defaults={
                    "name": s["name"],
                    "region_id": s["region_id"],
                    "constellation_id": s.get("constellation_id"),
                    "security": s.get("security", 0.0),
                    "x": s.get("x", 0.0),
                    "y": s.get("y", 0.0),
                    "z": s.get("z", 0.0),
                },
            )
        for j in data.get("system_jumps", []):
            SdeSystemJump.objects.get_or_create(
                from_system_id=j["from"], to_system_id=j["to"]
            )
        SdeTypeSkill.objects.all().delete()
        for ts in data.get("type_skills", []):
            SdeTypeSkill.objects.create(
                type_id=ts["type_id"],
                skill_type_id=ts["skill_type_id"],
                level=ts["level"],
            )
        SdeBlueprintMaterial.objects.all().delete()
        for bm in data.get("blueprint_materials", []):
            SdeBlueprintMaterial.objects.create(
                blueprint_type_id=bm["blueprint_type_id"],
                product_type_id=bm["product_type_id"],
                material_type_id=bm["material_type_id"],
                quantity=bm["quantity"],
                output_quantity=bm.get("output_quantity", 1),
                activity=bm.get("activity", "manufacturing"),
            )
        SdeBlueprintSkill.objects.all().delete()
        for bs in data.get("blueprint_skills", []):
            SdeBlueprintSkill.objects.create(
                blueprint_type_id=bs.get("blueprint_type_id", 0),
                product_type_id=bs["product_type_id"],
                skill_type_id=bs["skill_type_id"],
                level=bs.get("level", 1),
                activity=bs.get("activity", "manufacturing"),
            )
        SdeInventionProduct.objects.all().delete()
        for ip in data.get("invention_products", []):
            SdeInventionProduct.objects.create(
                t1_blueprint_type_id=ip.get("t1_blueprint_type_id", 0),
                t2_blueprint_type_id=ip.get("t2_blueprint_type_id", 0),
                product_type_id=ip["product_type_id"],
                runs=ip.get("runs", 1),
                probability=ip.get("probability", 0),
            )
        SdeDecryptor.objects.all().delete()
        for d in data.get("decryptors", []):
            SdeDecryptor.objects.create(
                type_id=d["type_id"],
                name=d["name"],
                probability_multiplier=d.get("probability_multiplier", 1),
                run_modifier=d.get("run_modifier", 0),
                me_modifier=d.get("me_modifier", 0),
                te_modifier=d.get("te_modifier", 0),
            )
        SdeBlueprintActivityTime.objects.all().delete()
        for at in data.get("activity_times", []):
            SdeBlueprintActivityTime.objects.create(
                blueprint_type_id=at.get("blueprint_type_id", 0),
                product_type_id=at.get("product_type_id"),
                activity=at.get("activity", "manufacturing"),
                time=at.get("time", 0),
            )

        version = options["sde_version"] or timezone.now().strftime("%Y%m%d%H%M%S")
        AppSetting.objects.update_or_create(
            key="sde_version", defaults={"value": {"version": version}}
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Loaded SDE: {SdeType.objects.count()} types, "
                f"{SdeSolarSystem.objects.count()} systems (version {version})."
            )
        )
