"""Import the CCP FSD dogma graph: ship trait bonuses + the full modifier graph + skill dogma.

Fuzzwork's SQLite (``import_sde_fuzzwork``) carries *which* effects a type has but never *what
they do*: the ``modifierInfo`` graph and skill dogma are absent. This command sources all three
from CCP's authoritative ``sde.zip`` (``dogmaAttributes.yaml`` / ``typeDogma.yaml`` /
``dogmaEffects.yaml``) in a single download+parse, then does an idempotent full replace of:

  1. ``SdeShipBonus`` — normalised per-hull trait bonuses ("+5% Medium Hybrid Turret damage per
     Caldari Battlecruiser level"), which the current fitting engine turns into ``BonusSpec``s.
     Only ``postPercent`` modifiers are imported — the only percentage form the engine's
     multiplicative bonus model represents; additive/assignment modifiers (a small minority,
     and non-DPS) are skipped honestly rather than mis-applied. Data-driven classification:
       * modifying attr ``shipBonus*`` → per-level, scaled by the hull's primary skill;
         ``eliteBonus*`` → per-level, scaled by the T2/elite skill; ``roleBonus*`` → flat.
       * modifier func → filter: ``LocationGroupModifier`` by group; the RequiredSkill modifiers
         by "the fitted item/charge requires this skill"; ``ItemModifier`` hits the ship itself.
  2. ``SdeModifier`` — the FULL modifier graph (Tocha's Lab Phase 1): every effect's every
     ``modifierInfo`` entry, verbatim and unfiltered (all funcs, all operations). This is the
     authoritative data the future generic applicator consumes; it is *not* read by the engine
     yet (Phase 1 is data-only).
  3. Skill dogma — the per-level bonus *values* (``cpuOutputBonus2``, ``rofBonus``, …) and effect
     lists of every category-16 skill, written into ``SdeTypeAttribute`` / ``SdeTypeEffect``
     (which Fuzzwork populates only for fittable categories, never skills).

ORDERING: run this AFTER ``import_sde_fuzzwork`` — that command full-wipes ``SdeTypeAttribute`` /
``SdeTypeEffect`` / ``SdeDogmaEffect`` on every run, which would drop the skill dogma written
here (the ship-bonus / ``SdeModifier`` tables it never touches). Same dependency the ship-bonus
import already relies on. The kept command name ``import_ship_bonuses`` is an alias of
``import_dogma_graph`` for backward compatibility with existing runbooks.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import zipfile

import requests
import yaml
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.admin_audit.models import AppSetting
from apps.sde.models import (
    SdeModifier,
    SdeShipBonus,
    SdeType,
    SdeTypeAttribute,
    SdeTypeEffect,
)
from core.netcap import CappedReader

# libyaml's CSafeLoader is ~8x faster on the 25MB typeDogma.yaml; both it and the pure-python
# SafeLoader are *safe* loaders (they never instantiate arbitrary objects, unlike Loader /
# FullLoader) — the branch below keeps the Loader argument a literal so the linter can see that.
_HAVE_CLOADER = hasattr(yaml, "CSafeLoader")

SDE_URL = "https://eve-static-data-export.s3-eu-west-1.amazonaws.com/tranquility/sde.zip"
_FSD = ("fsd/dogmaAttributes.yaml", "fsd/typeDogma.yaml", "fsd/dogmaEffects.yaml")
_MAX_ZIP = 512 * 1024**2          # compressed ceiling (the zip is ~110MB)
_MAX_MEMBER = 256 * 1024**2       # per-extracted-YAML ceiling

_SHIP_CATEGORY = 6
_SUBSYSTEM_CATEGORY = 32  # T3 strategic-cruiser subsystems carry per-subsystem-skill bonuses too
_SKILL_CATEGORY = 16      # skills whose dogma holds the per-level bonus values the graph scales
_MIN_HULLS = 300          # a healthy import covers ~480 hulls; refuse a degraded parse below this
_MIN_MODIFIERS = 3000     # a healthy FSD graph is ~4,850 modifiers; refuse a degraded parse below
_MIN_SKILLS = 250         # ~590 skills carry dogma; refuse a degraded parse below this
_REQ_SKILL1, _REQ_SKILL2 = 182, 183      # ship dogma attrs: primary / secondary required skill
_OP_POSTPERCENT = 6
# em / explosive / kinetic / thermal damage attributes — a hull bonus on one of these lands
# on the loaded charge (the missile), so it is scoped to the charge, not the launcher.
_CHARGE_DAMAGE_ATTRS = frozenset({114, 116, 117, 118})


class Command(BaseCommand):
    help = ("Import from CCP's FSD SDE: ship trait bonuses + the full modifier graph "
            "(SdeModifier) + skill dogma. Run after import_sde_fuzzwork.")

    def add_arguments(self, parser):
        parser.add_argument("--zip", dest="zip_path", default="",
                            help="Use a local sde.zip instead of downloading (offline/testing).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report the ship-bonus / modifier / skill-dogma counts without "
                                 "writing.")
        parser.add_argument("--force", action="store_true",
                            help="Write even if fewer than the expected hull / modifier / skill "
                                 "counts parse (use only against a deliberately small DB).")

    def handle(self, *args, **options):
        work = None
        try:
            zip_path = options["zip_path"]
            if not zip_path:
                work = tempfile.mkdtemp(prefix="forca_fsd_")
                zip_path = self._download(work)
            attrs, type_dogma, effects = self._parse(zip_path)

            rows = self._build_rows(attrs, type_dogma, effects)
            if not rows:
                raise CommandError("No ship-bonus rows produced — refusing to wipe the table.")
            hulls = len({r["ship_type_id"] for r in rows})

            modifier_rows = self._build_modifiers(effects)
            skill_ids = set(
                SdeType.objects.filter(group__category_id=_SKILL_CATEGORY)
                .values_list("type_id", flat=True)
            )
            skill_attr_rows, skill_effect_rows = self._build_skill_dogma(type_dogma, skill_ids)
            skills_with_dogma = len(
                {r["type_id"] for r in skill_attr_rows} | {r["type_id"] for r in skill_effect_rows}
            )

            if options["dry_run"]:
                self.stdout.write(self.style.SUCCESS(
                    f"Would load {len(rows)} ship-bonus rows for {hulls} hulls, "
                    f"{len(modifier_rows)} dogma modifiers, and dogma for {skills_with_dogma} "
                    f"skills ({len(skill_attr_rows)} attrs / {len(skill_effect_rows)} effects) "
                    f"— no write."))
                return

            if not options["force"]:
                if hulls < _MIN_HULLS:
                    raise CommandError(
                        f"Only {hulls} hulls parsed (expected ~480); refusing to replace the "
                        f"table with a possibly-degraded set. Re-run with --force if the DB is "
                        f"intentionally small (e.g. a partial SDE).")
                if len(modifier_rows) < _MIN_MODIFIERS:
                    raise CommandError(
                        f"Only {len(modifier_rows)} dogma modifiers parsed (expected tens of "
                        f"thousands); refusing to replace SdeModifier. Re-run with --force for a "
                        f"deliberately partial SDE.")
                if skill_ids and skills_with_dogma < _MIN_SKILLS:
                    raise CommandError(
                        f"Only {skills_with_dogma} skills got dogma (expected ~590 over "
                        f"{len(skill_ids)} category-16 types); refusing to replace skill dogma. "
                        f"Re-run with --force for a deliberately partial SDE.")

            self._write(rows)
            self._write_graph(modifier_rows, skill_attr_rows, skill_effect_rows, skill_ids)
            self.stdout.write(self.style.SUCCESS(
                f"Loaded {len(rows)} ship-bonus rows for {hulls} hulls, "
                f"{len(modifier_rows)} dogma modifiers, and dogma for {skills_with_dogma} "
                f"skills ({len(skill_attr_rows)} attrs / {len(skill_effect_rows)} effects)."))
        finally:
            if work:
                shutil.rmtree(work, ignore_errors=True)

    # -- source ------------------------------------------------------------- #
    def _download(self, work: str) -> str:
        headers = {"User-Agent": settings.ESI_USER_AGENT}
        zip_path = os.path.join(work, "sde.zip")
        self.stdout.write("  downloading CCP FSD SDE …")
        downloaded = 0
        with requests.get(SDE_URL, stream=True, timeout=900, headers=headers) as resp:
            resp.raise_for_status()
            with open(zip_path, "wb") as fh:
                for chunk in resp.iter_content(1 << 20):
                    downloaded += len(chunk)
                    if downloaded > _MAX_ZIP:
                        raise CommandError("SDE zip exceeded its size ceiling.")
                    fh.write(chunk)
        return zip_path

    def _parse(self, zip_path: str):
        """Extract + YAML-parse only the 3 dogma members (never the 150MB types.yaml)."""
        def load(zf, name):
            with zf.open(name) as raw:
                data = CappedReader(raw, _MAX_MEMBER).read()
            if _HAVE_CLOADER:
                return yaml.load(data, Loader=yaml.CSafeLoader)
            return yaml.safe_load(data)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
                missing = [n for n in _FSD if n not in names]
                if missing:
                    raise CommandError(f"SDE zip missing expected members: {missing}")
                attrs, type_dogma, effects = (load(zf, n) for n in _FSD)
        except zipfile.BadZipFile as exc:
            raise CommandError(f"Corrupt SDE zip: {exc}") from exc
        return attrs, type_dogma, effects

    # -- mapping ------------------------------------------------------------ #
    def _build_rows(self, attrs, type_dogma, effects) -> list[dict]:
        ship_ids = set(SdeType.objects.filter(
            group__category_id__in=(_SHIP_CATEGORY, _SUBSYSTEM_CATEGORY)
        ).values_list("type_id", flat=True))

        def aname(i):
            return (attrs.get(i) or {}).get("name", "")

        rows: list[dict] = []
        for tid in ship_ids:
            entry = type_dogma.get(tid)
            if not entry:
                continue
            av = {a["attributeID"]: a["value"] for a in entry.get("dogmaAttributes", [])}
            used_keys: set[str] = set()
            for e in entry.get("dogmaEffects", []):
                eid = e["effectID"]
                eff = effects.get(eid) or {}
                ename = eff.get("effectName") or str(eid)
                for idx, m in enumerate(eff.get("modifierInfo") or []):
                    row = self._row_from_modifier(tid, ename, m, av, aname)
                    if row is None:
                        continue
                    key = f"{eid}_{idx}"          # compact, stable, unique within a hull
                    while key in used_keys:
                        key += "x"
                    used_keys.add(key)
                    row["key"] = key
                    rows.append(row)
        return rows

    @staticmethod
    def _row_from_modifier(ship_id, ename, m, av, aname) -> dict | None:
        if m.get("operation") != _OP_POSTPERCENT:
            return None
        mod_attr = m.get("modifyingAttributeID")
        name = aname(mod_attr)
        # CCP convention: a bonus attribute whose name contains "Role" is a FLAT role bonus
        # (always-on, not skill-scaled) — even when it carries a shipBonus*/eliteBonus* prefix
        # (e.g. shipBonusRole1..8, eliteBonus*Role*). Those hold large flat values (up to
        # thousands of %); treating them as per-level would be catastrophically wrong.
        if name.startswith("roleBonus") or "Role" in name:
            if not name.startswith(("shipBonus", "eliteBonus", "roleBonus")):
                return None                      # "Role" only counts on a known bonus attribute
            per_level, skill = False, None
        elif name.startswith("shipBonus"):
            per_level, skill = True, av.get(_REQ_SKILL1)
        elif name.startswith("eliteBonus"):
            per_level, skill = True, av.get(_REQ_SKILL2)
        elif name.startswith("subsystemBonus"):
            # A T3 subsystem's per-level bonus scales by the subsystem's own required skill
            # (the racial subsystem skill, reqSkill1) — e.g. the Loki launcher-RoF bonus.
            per_level, skill = True, av.get(_REQ_SKILL1)
        else:
            return None                          # not a recognised hull-bonus attribute
        if per_level and not skill:
            return None                          # cannot scale without a skill
        amount = av.get(mod_attr)
        if not amount:
            return None
        target_attr = m.get("modifiedAttributeID")
        if target_attr is None:
            return None
        func = m.get("func")
        row = {"ship_type_id": int(ship_id), "target_attribute_id": int(target_attr),
               "amount": float(amount), "per_level": per_level,
               "skill_type_id": int(skill) if skill else None,
               "target_domain": "item", "match_group_ids": [], "match_required_skill_id": None,
               "label": ename[:128]}
        if func == "LocationGroupModifier":
            gid = m.get("groupID")
            if gid is None:
                return None
            row["match_group_ids"] = [int(gid)]
        elif func in ("LocationRequiredSkillModifier", "OwnerRequiredSkillModifier"):
            sid = m.get("skillTypeID")
            if sid is None:
                return None
            row["match_required_skill_id"] = int(sid)
            if target_attr in _CHARGE_DAMAGE_ATTRS:
                row["target_domain"] = "charge"
        elif func == "ItemModifier":
            row["target_domain"] = "ship"        # modifies the hull's own attribute
        else:
            return None                          # LocationModifier / unknown — skip, don't over-apply
        return row

    # -- full graph + skill dogma (Phase 1) --------------------------------- #
    @staticmethod
    def _int_or_none(v):
        return int(v) if v is not None else None

    def _build_modifiers(self, effects) -> list[dict]:
        """Every effect's every ``modifierInfo`` entry → a normalised SdeModifier row.

        Unlike the ship-bonus mapping this is verbatim and unfiltered — all funcs, all
        operations — because ``SdeModifier`` is the authoritative graph the generic applicator
        will consume; interpretation happens at apply time, not import time.
        """
        rows: list[dict] = []
        for eid, eff in effects.items():
            for m in (eff or {}).get("modifierInfo") or []:
                if not isinstance(m, dict):
                    continue
                func = m.get("func")
                if not func:
                    continue                       # a modifier with no func is not applicable
                rows.append({
                    "effect_id": int(eid),
                    "func": str(func)[:64],
                    "modified_attribute_id": self._int_or_none(m.get("modifiedAttributeID")),
                    "modifying_attribute_id": self._int_or_none(m.get("modifyingAttributeID")),
                    "operation": self._int_or_none(m.get("operation")),
                    "group_id": self._int_or_none(m.get("groupID")),
                    "skill_type_id": self._int_or_none(m.get("skillTypeID")),
                    "domain": str(m.get("domain") or "")[:32],
                })
        return rows

    def _build_skill_dogma(self, type_dogma, skill_ids) -> tuple[list[dict], list[dict]]:
        """Category-16 skills' dogma attribute values + effect lists.

        Scoped to skills that exist as ``SdeType`` in this DB (``skill_ids``) so we never insert
        an orphaned attribute for a type without a parent row (the FK would reject it).
        """
        attr_rows: list[dict] = []
        effect_rows: list[dict] = []
        for tid in skill_ids:
            entry = type_dogma.get(tid)
            if not entry:
                continue
            for a in entry.get("dogmaAttributes", []):
                aid = a.get("attributeID")
                if aid is None:
                    continue
                attr_rows.append({"type_id": int(tid), "attribute_id": int(aid),
                                  "value": float(a.get("value") or 0.0)})
            for e in entry.get("dogmaEffects", []):
                eid = e.get("effectID")
                if eid is None:
                    continue
                effect_rows.append({"type_id": int(tid), "effect_id": int(eid),
                                    "is_default": bool(e.get("isDefault", False))})
        return attr_rows, effect_rows

    @transaction.atomic
    def _write_graph(self, modifier_rows, skill_attr_rows, skill_effect_rows, skill_ids):
        SdeModifier.objects.all().delete()
        SdeModifier.objects.bulk_create([
            SdeModifier(
                effect_id=r["effect_id"], func=r["func"],
                modified_attribute_id=r["modified_attribute_id"],
                modifying_attribute_id=r["modifying_attribute_id"],
                operation=r["operation"], group_id=r["group_id"],
                skill_type_id=r["skill_type_id"], domain=r["domain"],
            ) for r in modifier_rows
        ], batch_size=5000)

        # Skill dogma: scoped replace over the skill category only, so module/ship/charge
        # attributes (owned by import_sde_fuzzwork) are never touched.
        if skill_ids:
            SdeTypeAttribute.objects.filter(type_id__in=skill_ids).delete()
            SdeTypeEffect.objects.filter(type_id__in=skill_ids).delete()
            SdeTypeAttribute.objects.bulk_create([
                SdeTypeAttribute(type_id=r["type_id"], attribute_id=r["attribute_id"],
                                 value=r["value"])
                for r in skill_attr_rows
            ], batch_size=5000, ignore_conflicts=True)
            SdeTypeEffect.objects.bulk_create([
                SdeTypeEffect(type_id=r["type_id"], effect_id=r["effect_id"],
                              is_default=r["is_default"])
                for r in skill_effect_rows
            ], batch_size=5000, ignore_conflicts=True)

        AppSetting.objects.update_or_create(
            key="dogma_graph_version",
            defaults={"value": {"version": timezone.now().strftime("%Y%m%d%H%M%S")}})

    # -- write -------------------------------------------------------------- #
    @transaction.atomic
    def _write(self, rows):
        SdeShipBonus.objects.all().delete()
        SdeShipBonus.objects.bulk_create([
            SdeShipBonus(
                ship_type_id=r["ship_type_id"], key=r["key"],
                target_attribute_id=r["target_attribute_id"], amount=r["amount"],
                per_level=r["per_level"], skill_type_id=r["skill_type_id"],
                target_domain=r["target_domain"], match_group_ids=r["match_group_ids"],
                match_required_skill_id=r["match_required_skill_id"], label=r["label"],
            ) for r in rows
        ], batch_size=2000)
        AppSetting.objects.update_or_create(
            key="ship_bonus_data_version",
            defaults={"value": {"version": timezone.now().strftime("%Y%m%d%H%M%S")}})
