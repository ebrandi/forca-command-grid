"""Import the full Static Data Export from the Fuzzwork SQLite dump.

Fuzzwork publishes the SDE as a gzipped SQLite database. We download it, read
the tables we need, and load real names (types, groups, categories, systems,
regions), system security, type base prices, and dogma-derived skill
requirements — so the UI shows real names/values instead of numeric IDs.

    manage.py import_sde_fuzzwork [--skip-dogma]
"""
from __future__ import annotations

import gzip
import os
import re
import shutil
import sqlite3
import tempfile

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.admin_audit.models import AppSetting
from apps.sde.models import (
    SdeBlueprintActivityTime,
    SdeBlueprintMaterial,
    SdeBlueprintSkill,
    SdeCategory,
    SdeCelestial,
    SdeConstellation,
    SdeDecryptor,
    SdeGroup,
    SdeInventionProduct,
    SdeRegion,
    SdeSolarSystem,
    SdeStation,
    SdeSystemJump,
    SdeType,
    SdeTypeMaterial,
    SdeTypeSkill,
)

# Decryptor invention modifiers live in dogma type attributes.
_DECRYPTOR_GROUP_ID = 1304
_ATTR_PROB_MULT = 1112     # inventionPropabilityMultiplier
_ATTR_RUN_MOD = 1124       # inventionMaxRunModifier
_ATTR_ME_MOD = 1113        # inventionMEModifier
_ATTR_TE_MOD = 1114        # inventionTEModifier

BASE = "https://www.fuzzwork.co.uk/dump/latest"
_REQUIRED_SKILL_ATTRS = {182: 277, 183: 278, 184: 279}  # skill-id attr -> level attr

# The Fuzzwork SDE is large but bounded (compressed ~100-200 MB, decompressed
# ~1.5-2 GB). These ceilings sit well above the real artefact yet stop a
# compromised/MITM'd mirror from serving a bomb that disk-exhausts the host.
_MAX_SDE_DOWNLOAD = 2 * 1024**3        # 2 GB compressed
_MAX_SDE_DECOMPRESSED = 8 * 1024**3    # 8 GB decompressed


class Command(BaseCommand):
    help = "Import the full SDE from the Fuzzwork SQLite dump."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--skip-dogma", action="store_true")
        parser.add_argument(
            "--coords-only", action="store_true",
            help="Only refresh solar-system x/y/z on existing rows (for the JF jump graph).",
        )
        parser.add_argument(
            "--map-extras", action="store_true",
            help="Only load constellations, system→constellation links and stargate jumps "
                 "(for region maps) on top of existing systems — no full rebuild.",
        )
        parser.add_argument(
            "--celestials-only", action="store_true",
            help="Only load planets/moons/asteroid belts (mapDenormalize) for the "
                 "system page — no full rebuild.",
        )
        parser.add_argument(
            "--skill-attrs-only", action="store_true",
            help="Only (re)load per-skill training attributes (dogma 180/181).",
        )
        parser.add_argument(
            "--blueprints-only", action="store_true",
            help="Only rebuild the blueprint material + manufacturing-skill tables on top "
                 "of existing types — no full SDE rebuild.",
        )
        parser.add_argument(
            "--type-materials-only", action="store_true",
            help="Only (re)load reprocessing yields (invTypeMaterials) + refresh portion_size "
                 "on existing types — for the ore-buyback valuation (4.9). No full rebuild.",
        )

    def handle(self, *args, **options) -> None:
        db_path = self._download_db()
        try:
            con = sqlite3.connect(db_path)
            try:
                if options["coords_only"]:
                    self._update_coords(con)
                    return
                if options["map_extras"]:
                    self._update_map_extras(con)
                    return
                if options["celestials_only"]:
                    self._load_celestials(con)
                    return
                if options["skill_attrs_only"]:
                    self._load_skill_attributes(con)
                    return
                if options["blueprints_only"]:
                    self._load_blueprints(con)
                    return
                if options["type_materials_only"]:
                    self._load_type_materials(con)
                    return
                self._load_categories(con)
                self._load_groups(con)
                self._load_types(con)
                self._load_map(con)
                self._load_stations(con)
                self._load_celestials(con)
                self._load_blueprints(con)
                self._load_type_materials(con)
                if not options["skip_dogma"]:
                    self._load_dogma_skills(con)
                    self._load_skill_ranks(con)
                    self._load_skill_attributes(con)
            finally:
                con.close()
        finally:
            # db_path lives inside a private mkdtemp() dir — remove the whole dir.
            shutil.rmtree(os.path.dirname(db_path), ignore_errors=True)

        AppSetting.objects.update_or_create(
            key="sde_version",
            defaults={"value": {"version": "fuzzwork-" + timezone.now().strftime("%Y%m%d")}},
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"SDE loaded: {SdeType.objects.count()} types, "
                f"{SdeSolarSystem.objects.count()} systems, "
                f"{SdeTypeSkill.objects.count()} skill requirements."
            )
        )

    def _download_db(self) -> str:
        from django.conf import settings

        from core.netcap import CappedReader, DataTooLarge

        # Identify ourselves to Fuzzwork with the same contactable UA we send to ESI,
        # rather than the default python-requests string an anonymous scraper sends.
        headers = {"User-Agent": settings.ESI_USER_AGENT}
        html = requests.get(f"{BASE}/", timeout=60, headers=headers).text
        match = re.search(r'href="(eve_[\w.]+\.db\.gz)"', html)
        if not match:
            raise CommandError("Could not find the SDE .db.gz at Fuzzwork.")
        url = f"{BASE}/{match.group(1)}"
        self.stdout.write(f"  downloading {match.group(1)} …")
        # A private 0700 dir with a random name: no fixed path in the shared temp
        # dir that a local user could pre-create a symlink at (clobber/TOCTOU), and
        # it is cleaned up wholesale by handle()'s finally.
        work = tempfile.mkdtemp(prefix="forca_sde_")
        gz_path = os.path.join(work, "sde.db.gz")
        db_path = os.path.join(work, "sde.db")
        try:
            downloaded = 0
            with requests.get(url, stream=True, timeout=900, headers=headers) as resp:
                resp.raise_for_status()
                with open(gz_path, "wb") as fh:
                    for chunk in resp.iter_content(1 << 20):
                        downloaded += len(chunk)
                        if downloaded > _MAX_SDE_DOWNLOAD:
                            raise CommandError("SDE download exceeded its size ceiling.")
                        fh.write(chunk)
            with gzip.open(gz_path, "rb") as fin, open(db_path, "wb") as fout:
                shutil.copyfileobj(CappedReader(fin, _MAX_SDE_DECOMPRESSED), fout)
        except DataTooLarge as exc:
            shutil.rmtree(work, ignore_errors=True)
            raise CommandError("SDE decompressed size exceeded its ceiling.") from exc
        except (CommandError, OSError, requests.RequestException):
            # Don't leak the private work dir if the download/decompress fails.
            shutil.rmtree(work, ignore_errors=True)
            raise
        os.remove(gz_path)
        return db_path

    @staticmethod
    def _q(con, sql, params=()):
        cur = con.cursor()
        cur.execute(sql, params)
        return cur

    def _load_categories(self, con) -> None:
        rows = self._q(con, "SELECT categoryID, categoryName FROM invCategories")
        objs = [SdeCategory(category_id=r[0], name=r[1] or "") for r in rows if r[0] is not None]
        with transaction.atomic():
            SdeCategory.objects.all().delete()
            SdeCategory.objects.bulk_create(objs, batch_size=2000, ignore_conflicts=True)
        self.stdout.write(f"  categories: {len(objs)}")

    def _load_groups(self, con) -> None:
        valid = set(SdeCategory.objects.values_list("category_id", flat=True))
        rows = self._q(con, "SELECT groupID, categoryID, groupName FROM invGroups")
        objs = [
            SdeGroup(group_id=r[0], category_id=r[1], name=r[2] or "")
            for r in rows
            if r[0] is not None and r[1] in valid
        ]
        with transaction.atomic():
            SdeGroup.objects.all().delete()
            SdeGroup.objects.bulk_create(objs, batch_size=2000, ignore_conflicts=True)
        self.stdout.write(f"  groups: {len(objs)}")

    def _load_types(self, con) -> None:
        valid = set(SdeGroup.objects.values_list("group_id", flat=True))
        rows = self._q(
            con,
            "SELECT typeID, groupID, typeName, volume, basePrice, published, portionSize FROM invTypes",
        )
        objs = []
        for tid, gid, name, volume, base, published, portion in rows:
            if tid is None or gid not in valid:
                continue
            objs.append(
                SdeType(
                    type_id=tid,
                    group_id=gid,
                    name=name or f"Type {tid}",
                    volume=volume or 0.0,
                    base_price=base,
                    published=bool(published),
                    portion_size=portion or 1,
                )
            )
        with transaction.atomic():
            SdeType.objects.all().delete()
            SdeType.objects.bulk_create(objs, batch_size=2000, ignore_conflicts=True)
        self.stdout.write(f"  types: {len(objs)}")

    def _load_type_materials(self, con) -> None:
        """Reprocessing yields (invTypeMaterials) + a portion_size refresh on existing types.
        Idempotent (clear-and-reload); safe to run standalone on top of existing types."""
        valid = set(SdeType.objects.values_list("type_id", flat=True))
        # 1) refresh portion_size on the (few) types that reprocess in batches (>1).
        portion_updates = [
            SdeType(type_id=tid, portion_size=portion)
            for tid, portion in self._q(con, "SELECT typeID, portionSize FROM invTypes WHERE portionSize > 1")
            if tid in valid and portion
        ]
        if portion_updates:
            SdeType.objects.bulk_update(portion_updates, ["portion_size"], batch_size=2000)
        # 2) reload the reprocessing-material rows.
        objs = [
            SdeTypeMaterial(type_id=tid, material_type_id=mtid, quantity=qty)
            for tid, mtid, qty in self._q(con, "SELECT typeID, materialTypeID, quantity FROM invTypeMaterials")
            if tid in valid and mtid and qty
        ]
        with transaction.atomic():
            SdeTypeMaterial.objects.all().delete()
            SdeTypeMaterial.objects.bulk_create(objs, batch_size=2000, ignore_conflicts=True)
        self.stdout.write(f"  type-materials: {len(objs)} (portion updates: {len(portion_updates)})")

    def _load_map(self, con) -> None:
        rrows = self._q(con, "SELECT regionID, regionName FROM mapRegions")
        robjs = [SdeRegion(region_id=r[0], name=r[1] or "") for r in rrows if r[0] is not None]
        with transaction.atomic():
            SdeRegion.objects.all().delete()
            SdeRegion.objects.bulk_create(robjs, batch_size=2000, ignore_conflicts=True)
        valid_regions = set(SdeRegion.objects.values_list("region_id", flat=True))

        crows = self._q(
            con, "SELECT constellationID, regionID, constellationName FROM mapConstellations"
        )
        cobjs = [
            SdeConstellation(constellation_id=c[0], region_id=c[1], name=c[2] or "")
            for c in crows
            if c[0] is not None and c[1] in valid_regions
        ]
        with transaction.atomic():
            SdeConstellation.objects.all().delete()
            SdeConstellation.objects.bulk_create(cobjs, batch_size=2000, ignore_conflicts=True)
        valid_const = set(SdeConstellation.objects.values_list("constellation_id", flat=True))

        srows = self._q(
            con,
            "SELECT solarSystemID, regionID, constellationID, solarSystemName, security, x, y, z "
            "FROM mapSolarSystems",
        )
        sobjs = [
            SdeSolarSystem(
                system_id=s[0], region_id=s[1],
                constellation_id=s[2] if s[2] in valid_const else None,
                name=s[3] or "", security=s[4] or 0.0,
                x=s[5] or 0.0, y=s[6] or 0.0, z=s[7] or 0.0,
            )
            for s in srows
            if s[0] is not None and s[1] in valid_regions
        ]
        with transaction.atomic():
            SdeSolarSystem.objects.all().delete()
            SdeSolarSystem.objects.bulk_create(sobjs, batch_size=2000, ignore_conflicts=True)

        valid_sys = set(SdeSolarSystem.objects.values_list("system_id", flat=True))
        jrows = self._q(
            con, "SELECT fromSolarSystemID, toSolarSystemID FROM mapSolarSystemJumps"
        )
        jobjs = [
            SdeSystemJump(from_system_id=j[0], to_system_id=j[1])
            for j in jrows
            if j[0] in valid_sys and j[1] in valid_sys
        ]
        with transaction.atomic():
            SdeSystemJump.objects.all().delete()
            SdeSystemJump.objects.bulk_create(jobjs, batch_size=2000, ignore_conflicts=True)
        self.stdout.write(
            f"  regions: {len(robjs)}, constellations: {len(cobjs)}, "
            f"systems: {len(sobjs)}, jumps: {len(jobjs)}"
        )

    def _update_map_extras(self, con) -> None:
        """Load constellations + system links + stargate jumps on existing systems."""
        valid_regions = set(SdeRegion.objects.values_list("region_id", flat=True))
        crows = self._q(
            con, "SELECT constellationID, regionID, constellationName FROM mapConstellations"
        )
        cobjs = [
            SdeConstellation(constellation_id=c[0], region_id=c[1], name=c[2] or "")
            for c in crows
            if c[0] is not None and c[1] in valid_regions
        ]
        with transaction.atomic():
            SdeConstellation.objects.all().delete()
            SdeConstellation.objects.bulk_create(cobjs, batch_size=2000, ignore_conflicts=True)
        valid_const = set(SdeConstellation.objects.values_list("constellation_id", flat=True))

        existing = set(SdeSolarSystem.objects.values_list("system_id", flat=True))
        sys_const = self._q(con, "SELECT solarSystemID, constellationID FROM mapSolarSystems")
        updates = [
            SdeSolarSystem(system_id=s[0], constellation_id=s[1] if s[1] in valid_const else None)
            for s in sys_const
            if s[0] in existing
        ]
        with transaction.atomic():
            SdeSolarSystem.objects.bulk_update(updates, ["constellation_id"], batch_size=2000)

        jrows = self._q(con, "SELECT fromSolarSystemID, toSolarSystemID FROM mapSolarSystemJumps")
        jobjs = [
            SdeSystemJump(from_system_id=j[0], to_system_id=j[1])
            for j in jrows
            if j[0] in existing and j[1] in existing
        ]
        with transaction.atomic():
            SdeSystemJump.objects.all().delete()
            SdeSystemJump.objects.bulk_create(jobjs, batch_size=2000, ignore_conflicts=True)
        self.stdout.write(self.style.SUCCESS(
            f"Loaded {len(cobjs)} constellations and {len(jobjs)} stargate jumps."
        ))

    def _update_coords(self, con) -> None:
        """Refresh x/y/z on existing systems only — no deletes, no other tables."""
        existing = set(SdeSolarSystem.objects.values_list("system_id", flat=True))
        rows = self._q(con, "SELECT solarSystemID, x, y, z FROM mapSolarSystems")
        updates = [
            SdeSolarSystem(system_id=r[0], x=r[1] or 0.0, y=r[2] or 0.0, z=r[3] or 0.0)
            for r in rows
            if r[0] in existing
        ]
        with transaction.atomic():
            SdeSolarSystem.objects.bulk_update(updates, ["x", "y", "z"], batch_size=2000)
        self.stdout.write(self.style.SUCCESS(f"Updated coordinates for {len(updates)} systems."))

    def _load_stations(self, con) -> None:
        """NPC stations (staStations) — id, name, and the system they orbit."""
        sysnames = dict(SdeSolarSystem.objects.values_list("system_id", "name"))
        rows = self._q(
            con, "SELECT stationID, stationName, solarSystemID FROM staStations"
        )
        objs = [
            SdeStation(
                station_id=r[0], name=r[1] or "", system_id=r[2] or 0,
                system_name=sysnames.get(r[2], ""),
            )
            for r in rows
            if r[0] is not None and r[2] is not None
        ]
        with transaction.atomic():
            SdeStation.objects.all().delete()
            SdeStation.objects.bulk_create(objs, batch_size=2000, ignore_conflicts=True)
        self.stdout.write(f"  stations: {len(objs)}")

    # mapDenormalize group ids for celestials we keep.
    _CELESTIAL_KINDS = {7: SdeCelestial.Kind.PLANET, 8: SdeCelestial.Kind.MOON,
                        9: SdeCelestial.Kind.BELT}

    def _load_celestials(self, con) -> None:
        """Planets / moons / asteroid belts (mapDenormalize) for the system page."""
        rows = self._q(
            con,
            "SELECT itemID, typeID, groupID, solarSystemID, itemName, celestialIndex, orbitID "
            "FROM mapDenormalize WHERE groupID IN (7, 8, 9)",
        )
        objs = []
        for item_id, type_id, group_id, system_id, name, cidx, orbit_id in rows:
            kind = self._CELESTIAL_KINDS.get(group_id)
            if kind is None or item_id is None or system_id is None:
                continue
            objs.append(SdeCelestial(
                item_id=item_id, system_id=system_id, kind=kind, type_id=type_id,
                name=name or "", celestial_index=cidx,
                parent_planet_id=orbit_id if kind != SdeCelestial.Kind.PLANET else None,
            ))
        with transaction.atomic():
            SdeCelestial.objects.all().delete()
            SdeCelestial.objects.bulk_create(objs, batch_size=5000, ignore_conflicts=True)
        self.stdout.write(f"  celestials: {len(objs)}")

    # EVE industry activity ids.
    _ACT_MANUFACTURING = 1
    _ACT_RESEARCH_TE = 3
    _ACT_RESEARCH_ME = 4
    _ACT_COPYING = 5
    _ACT_INVENTION = 8
    _ACT_REACTION = 11
    _ACTIVITY_LABELS = {
        1: "manufacturing", 3: "research_te", 4: "research_me",
        5: "copying", 8: "invention", 11: "reaction",
    }

    def _load_blueprints(self, con) -> None:
        type_ids = set(SdeType.objects.values_list("type_id", flat=True))
        objs: list[SdeBlueprintMaterial] = []
        objs += self._activity_materials(con, self._ACT_MANUFACTURING, "manufacturing", type_ids)
        objs += self._activity_materials(con, self._ACT_REACTION, "reaction", type_ids)
        objs += self._invention_datacores(con, type_ids)
        with transaction.atomic():
            SdeBlueprintMaterial.objects.all().delete()
            SdeBlueprintMaterial.objects.bulk_create(objs, batch_size=2000, ignore_conflicts=True)
        by_act: dict[str, int] = {}
        for o in objs:
            by_act[o.activity] = by_act.get(o.activity, 0) + 1
        self.stdout.write(f"  blueprint materials: {len(objs)} ({by_act})")
        self._load_blueprint_skills(con, type_ids)
        self._load_invention_reference(con, type_ids)
        self._load_activity_times(con, type_ids)
        self._load_decryptors(con, type_ids)

    def _mfg_product_map(self, con) -> dict[int, int]:
        """blueprint typeID -> manufactured product typeID (industry activity 1)."""
        return {
            bp: prod
            for bp, prod in self._q(
                con,
                "SELECT typeID, productTypeID FROM industryActivityProducts WHERE activityID=?",
                (self._ACT_MANUFACTURING,),
            )
        }

    def _invents_bpc_map(self, con) -> dict[int, int]:
        """T1 blueprint typeID -> invented T2/T3 BPC typeID (industry activity 8)."""
        return {
            bp: prod
            for bp, prod in self._q(
                con,
                "SELECT typeID, productTypeID FROM industryActivityProducts WHERE activityID=?",
                (self._ACT_INVENTION,),
            )
        }

    def _load_blueprint_skills(self, con, type_ids) -> None:
        """Manufacturing / reaction / invention *skill* requirements per product.

        Manufacturing & reaction skills key straight to the produced item; invention
        skills are keyed via T1 blueprint -> invented BPC -> manufactured item, so the
        invention planner can look them up by the item the pilot wants to build.
        """
        skills: list[SdeBlueprintSkill] = []
        for activity_id, label in ((self._ACT_MANUFACTURING, "manufacturing"), (self._ACT_REACTION, "reaction")):
            product_of = {
                bp: prod
                for bp, prod in self._q(
                    con,
                    "SELECT typeID, productTypeID FROM industryActivityProducts WHERE activityID=?",
                    (activity_id,),
                )
            }
            for bp, skill_id, level in self._q(
                con,
                "SELECT typeID, skillID, level FROM industryActivitySkills WHERE activityID=?",
                (activity_id,),
            ):
                product = product_of.get(bp)
                if product in type_ids and skill_id in type_ids:
                    skills.append(SdeBlueprintSkill(
                        blueprint_type_id=bp, product_type_id=product,
                        skill_type_id=skill_id, level=level or 1, activity=label,
                    ))
        # Invention skills: map t1 bp -> invented bpc -> manufactured item.
        invents_bpc = self._invents_bpc_map(con)
        mfg_product = self._mfg_product_map(con)
        seen_inv: set[tuple[int, int]] = set()
        for bp, skill_id, level in self._q(
            con,
            "SELECT typeID, skillID, level FROM industryActivitySkills WHERE activityID=?",
            (self._ACT_INVENTION,),
        ):
            item = mfg_product.get(invents_bpc.get(bp))
            if item in type_ids and skill_id in type_ids and (item, skill_id) not in seen_inv:
                seen_inv.add((item, skill_id))
                skills.append(SdeBlueprintSkill(
                    blueprint_type_id=bp, product_type_id=item,
                    skill_type_id=skill_id, level=level or 1, activity="invention",
                ))
        with transaction.atomic():
            SdeBlueprintSkill.objects.all().delete()
            SdeBlueprintSkill.objects.bulk_create(skills, batch_size=2000, ignore_conflicts=True)
        self.stdout.write(f"  blueprint skills: {len(skills)}")

    def _load_invention_reference(self, con, type_ids) -> None:
        """Invention products + base success probability (industry activity 8).

        Joins invention products (T1 bp -> T2 BPC, with per-success run count) to the
        BPC's manufactured item, and attaches the base probability. This is the
        reference data a T2 invention planner needs, keyed by the manufactured item.
        """
        mfg_product = self._mfg_product_map(con)
        # T1 bp -> (t2 bpc, runs-per-success)
        bpc_runs = {
            bp: (prod, qty or 1)
            for bp, prod, qty in self._q(
                con,
                "SELECT typeID, productTypeID, quantity FROM industryActivityProducts WHERE activityID=?",
                (self._ACT_INVENTION,),
            )
        }
        prob_of = {
            (bp, prod): p
            for bp, prod, p in self._q(
                con,
                "SELECT typeID, productTypeID, probability FROM industryActivityProbabilities WHERE activityID=?",
                (self._ACT_INVENTION,),
            )
        }
        rows: list[SdeInventionProduct] = []
        seen: set[int] = set()
        for t1_bp, (t2_bpc, runs) in bpc_runs.items():
            item = mfg_product.get(t2_bpc)
            if item not in type_ids or item in seen:
                continue
            seen.add(item)
            rows.append(SdeInventionProduct(
                t1_blueprint_type_id=t1_bp, t2_blueprint_type_id=t2_bpc,
                product_type_id=item, runs=runs,
                probability=prob_of.get((t1_bp, t2_bpc), 0) or 0,
            ))
        with transaction.atomic():
            SdeInventionProduct.objects.all().delete()
            SdeInventionProduct.objects.bulk_create(rows, batch_size=2000, ignore_conflicts=True)
        self.stdout.write(f"  invention products: {len(rows)}")

    def _load_activity_times(self, con, type_ids) -> None:
        """Base activity durations (industryActivity.time), keyed by blueprint+activity."""
        mfg_product = self._mfg_product_map(con)
        invents_bpc = self._invents_bpc_map(con)
        rows: list[SdeBlueprintActivityTime] = []
        for bp, activity_id, seconds in self._q(
            con, "SELECT typeID, activityID, time FROM industryActivity"
        ):
            label = self._ACTIVITY_LABELS.get(activity_id)
            if label is None or not seconds:
                continue
            if activity_id == self._ACT_INVENTION:
                product = mfg_product.get(invents_bpc.get(bp))
            else:
                product = mfg_product.get(bp)
            rows.append(SdeBlueprintActivityTime(
                blueprint_type_id=bp,
                product_type_id=product if product in type_ids else None,
                activity=label, time=seconds,
            ))
        with transaction.atomic():
            SdeBlueprintActivityTime.objects.all().delete()
            SdeBlueprintActivityTime.objects.bulk_create(rows, batch_size=2000, ignore_conflicts=True)
        self.stdout.write(f"  activity times: {len(rows)}")

    def _load_decryptors(self, con, type_ids) -> None:
        """Decryptor invention modifiers, from dogma attributes on group 1304 types."""
        decryptor_ids = [
            tid for (tid,) in self._q(
                con, "SELECT typeID FROM invTypes WHERE groupID=?", (_DECRYPTOR_GROUP_ID,)
            )
        ]
        names = dict(
            SdeType.objects.filter(type_id__in=decryptor_ids).values_list("type_id", "name")
        )
        attr_ids = (_ATTR_PROB_MULT, _ATTR_RUN_MOD, _ATTR_ME_MOD, _ATTR_TE_MOD)
        attrs: dict[int, dict[int, float]] = {}
        if decryptor_ids:
            placeholders = ",".join("?" * len(decryptor_ids))
            attr_placeholders = ",".join("?" * len(attr_ids))
            for tid, attr_id, vint, vfloat in self._q(
                con,
                # Interpolated parts are only "?,?" placeholder lists; all values are
                # bound parameters — not an injection vector.
                f"SELECT typeID, attributeID, valueInt, valueFloat FROM dgmTypeAttributes "  # noqa: S608
                f"WHERE typeID IN ({placeholders}) AND attributeID IN ({attr_placeholders})",
                (*decryptor_ids, *attr_ids),
            ):
                val = vfloat if vfloat is not None else (vint if vint is not None else 0)
                attrs.setdefault(tid, {})[attr_id] = val
        rows: list[SdeDecryptor] = []
        for tid in decryptor_ids:
            a = attrs.get(tid, {})
            rows.append(SdeDecryptor(
                type_id=tid, name=names.get(tid, f"Decryptor {tid}")[:128],
                probability_multiplier=round(a.get(_ATTR_PROB_MULT, 1.0), 3),
                run_modifier=int(a.get(_ATTR_RUN_MOD, 0)),
                me_modifier=int(a.get(_ATTR_ME_MOD, 0)),
                te_modifier=int(a.get(_ATTR_TE_MOD, 0)),
            ))
        with transaction.atomic():
            SdeDecryptor.objects.all().delete()
            SdeDecryptor.objects.bulk_create(rows, batch_size=500, ignore_conflicts=True)
        self.stdout.write(f"  decryptors: {len(rows)}")

    def _activity_materials(self, con, activity_id, label, type_ids) -> list[SdeBlueprintMaterial]:
        """Materials + per-run output for one deterministic build activity."""
        product_of = {}
        output_of = {}
        for bp, prod, qty in self._q(
            con,
            "SELECT typeID, productTypeID, quantity FROM industryActivityProducts WHERE activityID=?",
            (activity_id,),
        ):
            product_of[bp] = prod
            output_of[bp] = qty or 1
        rows = []
        for bp, mat, qty in self._q(
            con,
            "SELECT typeID, materialTypeID, quantity FROM industryActivityMaterials WHERE activityID=?",
            (activity_id,),
        ):
            product = product_of.get(bp)
            if product in type_ids and mat in type_ids and qty:
                rows.append(
                    SdeBlueprintMaterial(
                        blueprint_type_id=bp,
                        product_type_id=product,
                        material_type_id=mat,
                        quantity=qty,
                        output_quantity=output_of.get(bp, 1),
                        activity=label,
                    )
                )
        return rows

    def _invention_datacores(self, con, type_ids) -> list[SdeBlueprintMaterial]:
        """Datacores an item needs via invention, mapped to the produced item.

        Invention yields a T2/T3 blueprint copy, which in turn manufactures the
        item. We join invention → produced BPC → manufactured item so the
        datacore cost attaches to the item members actually want to build.
        """
        invents_bpc = {
            bp: prod
            for bp, prod in self._q(
                con,
                "SELECT typeID, productTypeID FROM industryActivityProducts WHERE activityID=?",
                (self._ACT_INVENTION,),
            )
        }
        mfg_product = {
            bp: prod
            for bp, prod in self._q(
                con,
                "SELECT typeID, productTypeID FROM industryActivityProducts WHERE activityID=?",
                (self._ACT_MANUFACTURING,),
            )
        }
        rows = []
        for bp, mat, qty in self._q(
            con,
            "SELECT typeID, materialTypeID, quantity FROM industryActivityMaterials WHERE activityID=?",
            (self._ACT_INVENTION,),
        ):
            item = mfg_product.get(invents_bpc.get(bp))
            if item in type_ids and mat in type_ids and qty:
                rows.append(
                    SdeBlueprintMaterial(
                        blueprint_type_id=bp,
                        product_type_id=item,
                        material_type_id=mat,
                        quantity=qty,
                        output_quantity=1,
                        activity="invention",
                    )
                )
        return rows

    def _load_skill_ranks(self, con) -> None:
        """Skill training multiplier (dogma attr 275 = skillTimeConstant)."""
        updates = []
        valid = set(SdeType.objects.values_list("type_id", flat=True))
        for tid, vint, vfloat in self._q(
            con,
            "SELECT typeID, valueInt, valueFloat FROM dgmTypeAttributes WHERE attributeID=275",
        ):
            rank = vint if vint is not None else (int(vfloat) if vfloat is not None else None)
            if tid in valid and rank:
                updates.append(SdeType(type_id=tid, rank=int(rank)))
        with transaction.atomic():
            SdeType.objects.bulk_update(updates, ["rank"], batch_size=2000)
        self.stdout.write(f"  skill ranks: {len(updates)}")

    def _load_skill_attributes(self, con) -> None:
        """Per-skill training attributes: dogma attr 180 (primary) / 181 (secondary).

        The values are attribute *type ids* (164-168); we store them on the skill's
        SdeType so the training-time estimate can use the pilot's real attributes.
        """
        valid = set(SdeType.objects.values_list("type_id", flat=True))
        per_type: dict[int, dict[int, int]] = {}
        for tid, attr, vint, vfloat in self._q(
            con,
            "SELECT typeID, attributeID, valueInt, valueFloat FROM dgmTypeAttributes "  # noqa: S608
            "WHERE attributeID IN (180, 181)",
        ):
            val = vint if vint is not None else (int(vfloat) if vfloat is not None else None)
            if tid in valid and val:
                per_type.setdefault(tid, {})[attr] = int(val)
        updates = [
            SdeType(type_id=tid, primary_attribute_id=attrs.get(180),
                    secondary_attribute_id=attrs.get(181))
            for tid, attrs in per_type.items()
            if attrs.get(180) and attrs.get(181)
        ]
        with transaction.atomic():
            SdeType.objects.bulk_update(
                updates, ["primary_attribute_id", "secondary_attribute_id"], batch_size=2000
            )
        self.stdout.write(f"  skill attributes: {len(updates)}")

    def _load_dogma_skills(self, con) -> None:
        rows = self._q(
            con,
            "SELECT typeID, attributeID, valueInt, valueFloat FROM dgmTypeAttributes "
            "WHERE attributeID IN (182,183,184,277,278,279)",
        )
        per_type: dict[int, dict[int, int]] = {}
        for tid, attr, vint, vfloat in rows:
            val = vint if vint is not None else (int(vfloat) if vfloat is not None else None)
            if tid is None or val is None:
                continue
            per_type.setdefault(tid, {})[attr] = int(val)

        type_ids = set(SdeType.objects.values_list("type_id", flat=True))
        objs = []
        for tid, attrs in per_type.items():
            if tid not in type_ids:
                continue
            for skill_attr, level_attr in _REQUIRED_SKILL_ATTRS.items():
                skill_id, level = attrs.get(skill_attr), attrs.get(level_attr)
                if skill_id and level and skill_id in type_ids:
                    objs.append(SdeTypeSkill(type_id=tid, skill_type_id=skill_id, level=level))
        with transaction.atomic():
            SdeTypeSkill.objects.all().delete()
            SdeTypeSkill.objects.bulk_create(objs, batch_size=2000, ignore_conflicts=True)
        self.stdout.write(f"  skill requirements: {len(objs)}")
