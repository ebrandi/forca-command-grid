"""Load the Planetary Industry static "rulebook" (materials, planets, schematics).

Two data sources, both authoritative:

* **Bundled fixtures** (default) — ``apps/planetary/fixtures/pi_materials.json`` and
  ``pi_schematics.json``, generated from the SDE / EveRef. Self-contained: it also
  upserts the ``SdeType`` rows for PI materials so names and icons resolve even on a
  minimal SDE (tests, a fresh dev box). Run this after ``load_sde``.
* ``--from-everef`` — refresh the schematics live from EveRef reference-data
  (``ref-data.everef.net/schematics``). Use this to pick up a CCP balance pass.

Idempotent: safe to re-run. Never touches pilot plans or colonies.

    manage.py load_pi_static
    manage.py load_pi_static --from-everef
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.planetary.models import (
    PiMaterial,
    PiPlanetResource,
    PiPlanetType,
    PiSchematic,
    PiSchematicInput,
)
from apps.planetary.static_data import PLANET_RESOURCES, PLANET_TYPES
from apps.sde.models import SdeCategory, SdeGroup, SdeType

_FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"
_EVEREF_SCHEMATICS = "https://ref-data.everef.net/schematics"


class Command(BaseCommand):
    help = "Load/refresh the Planetary Industry static reference data."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--from-everef", action="store_true",
            help="Fetch schematics live from EveRef instead of the bundled fixture.")

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        materials = json.loads((_FIXTURES / "pi_materials.json").read_text())
        schematics = (
            self._fetch_everef() if options["from_everef"]
            else json.loads((_FIXTURES / "pi_schematics.json").read_text())
        )

        self._ensure_sde_rows(materials)
        n_mat = self._load_materials(materials)
        n_pt = self._load_planet_types()
        n_pr = self._load_planet_resources()
        n_sch = self._load_schematics(schematics)

        self.stdout.write(self.style.SUCCESS(
            f"PI static loaded: {n_mat} materials, {n_pt} planet types, "
            f"{n_pr} planet→resource links, {n_sch} schematics "
            f"({'EveRef' if options['from_everef'] else 'bundled fixture'})."))

    # -- SDE rows so names/icons resolve on a minimal SDE ------------------- #
    def _ensure_sde_rows(self, materials: dict) -> None:
        for c in materials["categories"]:
            SdeCategory.objects.update_or_create(
                category_id=c["category_id"], defaults={"name": c["name"]})
        for g in materials["groups"]:
            SdeGroup.objects.update_or_create(
                group_id=g["group_id"],
                defaults={"name": g["name"], "category_id": g["category_id"]})
        for m in materials["materials"]:
            SdeType.objects.update_or_create(
                type_id=m["type_id"],
                defaults={"name": m["name"], "group_id": m["group_id"],
                          "volume": m["volume"], "published": True})

    # -- PI reference tables ----------------------------------------------- #
    def _load_materials(self, materials: dict) -> int:
        for m in materials["materials"]:
            PiMaterial.objects.update_or_create(
                type_id=m["type_id"],
                defaults={"name": m["name"], "tier": m["tier"], "volume": m["volume"]})
        # Drop any stale rows no longer in the fixture.
        keep = {m["type_id"] for m in materials["materials"]}
        PiMaterial.objects.exclude(type_id__in=keep).delete()
        return len(materials["materials"])

    def _load_planet_types(self) -> int:
        for p in PLANET_TYPES:
            PiPlanetType.objects.update_or_create(
                type_id=p["type_id"],
                defaults={"slug": p["slug"], "name": p["name"], "best_for": p["best_for"],
                          "blurb": p["blurb"], "order": p["order"]})
        return len(PLANET_TYPES)

    def _load_planet_resources(self) -> int:
        by_name = {m.name: m for m in PiMaterial.objects.filter(tier="P0")}
        types = {p.slug: p for p in PiPlanetType.objects.all()}
        PiPlanetResource.objects.all().delete()
        rows = []
        for slug, names in PLANET_RESOURCES.items():
            pt = types.get(slug)
            if not pt:
                continue
            for name in names:
                mat = by_name.get(name)
                if mat is None:
                    raise CommandError(
                        f"planet→resource seed references unknown P0 '{name}' — "
                        f"is the material fixture loaded?")
                rows.append(PiPlanetResource(planet_type=pt, material=mat))
        PiPlanetResource.objects.bulk_create(rows)
        return len(rows)

    def _load_schematics(self, schematics: list) -> int:
        tiers = {m.type_id: m.tier for m in PiMaterial.objects.all()}
        material_ids = set(tiers)
        # Full replace — schematics are a closed set.
        PiSchematicInput.objects.all().delete()
        PiSchematic.objects.all().delete()
        made = 0
        for s in schematics:
            out_id = s["output_type_id"]
            if out_id not in material_ids:
                continue
            sch = PiSchematic.objects.create(
                schematic_id=s["schematic_id"], name=s["name"], output_id=out_id,
                output_quantity=s["output_quantity"], cycle_seconds=s["cycle_time"],
                tier=tiers.get(out_id, ""))
            PiSchematicInput.objects.bulk_create([
                PiSchematicInput(schematic=sch, material_id=i["type_id"], quantity=i["quantity"])
                for i in s["inputs"] if i["type_id"] in material_ids
            ])
            made += 1
        return made

    # -- EveRef live fetch -------------------------------------------------- #
    def _fetch_everef(self) -> list:
        import requests
        from django.conf import settings

        # Identify ourselves to EveRef the same way we identify to ESI — a data source
        # can (and EveRef asks to) rate-limit or block anonymous scrapers, and a
        # contactable UA is basic good-citizen behaviour.
        headers = {"User-Agent": settings.ESI_USER_AGENT}
        try:
            ids = requests.get(_EVEREF_SCHEMATICS, timeout=40, headers=headers).json()
        except Exception as exc:  # noqa: BLE001 - surface as a clean command error
            raise CommandError(f"Could not list EveRef schematics: {exc}") from exc
        if not isinstance(ids, list) or not ids:
            raise CommandError("EveRef returned no schematic ids.")
        out = []
        for sid in ids:
            d = requests.get(f"{_EVEREF_SCHEMATICS}/{sid}", timeout=40, headers=headers).json()
            products = d.get("products", {})
            if not products:
                continue
            product = next(iter(products.values()))
            out.append({
                "schematic_id": int(d["schematic_id"]),
                "name": d["name"]["en"],
                "cycle_time": int(d["cycle_time"]),
                "output_type_id": int(product["type_id"]),
                "output_quantity": int(product["quantity"]),
                "inputs": [{"type_id": int(m["type_id"]), "quantity": int(m["quantity"])}
                           for m in d.get("materials", {}).values()],
            })
        return out
