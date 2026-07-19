"""Import per-hull ship trait bonuses from CCP's FSD SDE (the modifierInfo graph).

Fuzzwork's SQLite (``import_sde_fuzzwork``) does NOT carry the FSD modifier graph, so ship
trait bonuses — "+5% Medium Hybrid Turret damage per Caldari Battlecruiser level" — are
sourced here from CCP's authoritative ``sde.zip`` and normalised into ``SdeShipBonus`` rows,
which the fitting engine turns into ``BonusSpec``s. Idempotent full replace (supersedes the
bundled dev sample). Only ``postPercent`` modifiers are imported — the only percentage form
the engine's multiplicative bonus model represents; additive/assignment modifiers (a small
minority, and non-DPS) are skipped honestly rather than mis-applied.

The classification is data-driven, not name-parsed per hull:
  * modifying attribute ``shipBonus*`` → per-level, scaled by the hull's primary skill;
    ``eliteBonus*`` → per-level, scaled by the T2/elite skill; ``roleBonus*`` → flat.
  * modifier function → filter: ``LocationGroupModifier`` by group; the RequiredSkill
    modifiers by "the fitted item/charge requires this skill"; ``ItemModifier`` hits the
    ship's own attribute.
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
from apps.sde.models import SdeShipBonus, SdeType
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
_MIN_HULLS = 300          # a healthy import covers ~480 hulls; refuse a degraded parse below this
_REQ_SKILL1, _REQ_SKILL2 = 182, 183      # ship dogma attrs: primary / secondary required skill
_OP_POSTPERCENT = 6
# em / explosive / kinetic / thermal damage attributes — a hull bonus on one of these lands
# on the loaded charge (the missile), so it is scoped to the charge, not the launcher.
_CHARGE_DAMAGE_ATTRS = frozenset({114, 116, 117, 118})


class Command(BaseCommand):
    help = "Import per-hull ship trait bonuses (SdeShipBonus) from CCP's FSD SDE modifierInfo."

    def add_arguments(self, parser):
        parser.add_argument("--zip", dest="zip_path", default="",
                            help="Use a local sde.zip instead of downloading (offline/testing).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report the row/hull count without writing.")
        parser.add_argument("--force", action="store_true",
                            help="Write even if fewer than the expected number of hulls parse "
                                 "(use only against a deliberately small DB).")

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
            if options["dry_run"]:
                self.stdout.write(self.style.SUCCESS(
                    f"Would load {len(rows)} ship-bonus rows for {hulls} hulls (no write)."))
                return
            if hulls < _MIN_HULLS and not options["force"]:
                raise CommandError(
                    f"Only {hulls} hulls parsed (expected ~480); refusing to replace the table "
                    f"with a possibly-degraded set. Re-run with --force if the DB is intentionally "
                    f"small (e.g. a partial SDE).")
            self._write(rows)
            self.stdout.write(self.style.SUCCESS(
                f"Loaded {len(rows)} ship-bonus rows for {hulls} hulls."))
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
