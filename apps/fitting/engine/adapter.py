"""The FORCA-owned engine boundary.

Everything outside :mod:`apps.fitting.engine` calls the fitting engine ONLY through
:class:`FittingEngine` here — never ``evaluator.evaluate`` directly. This is what lets the
engine implementation be swapped later without touching views, services or templates.

Responsibilities:
* :class:`ORMDataProvider` — feed the pure evaluator from the SDE dogma tables, bridging
  the pre-existing ``SdeType`` slot-count columns so hulls imported before the dogma
  layer still resolve.
* :class:`FittingEngine` — a thin facade adding a bounded, degrade-safe Redis cache keyed
  by the canonical fit/skill/profile hashes plus the engine and data versions, so a saved
  fit's numbers are reproducible and never silently drift after a data update.
"""
from __future__ import annotations

import logging

from . import attributes as A
from .bonuses import BonusSpec
from .effects import Op
from .graph import AttributeDef, DbuffDef, DbuffModifierDef, EffectDef, ModifierDef
from .types import ENGINE_VERSION, FitInput, FittingResult, OperatingProfile, SkillProfile

log = logging.getLogger("forca.fitting")

_CACHE_TTL = 3600  # 1h; the key already folds in engine+data version, so a data refresh
# (which bumps the data version) invalidates every entry without an explicit purge.


def slot_from_effects(effect_ids) -> str | None:
    """Infer a module's fitting rack ("high"/"med"/"low"/"rig"/"subsystem") from its
    slot-defining dogma effect (hiPower/medPower/loPower/rigSlot/subSystem)."""
    for eid in effect_ids:
        slot = A.SLOT_EFFECTS.get(eid)
        if slot:
            return slot
    return None


# Attributes we bridge from the legacy SdeType columns when the dogma import has not
# (yet) provided them for a hull.
_SLOT_COLUMN_ATTR = {
    "hi_slots": A.HI_SLOTS, "med_slots": A.MED_SLOTS,
    "low_slots": A.LOW_SLOTS, "rig_slots": A.RIG_SLOTS,
}

_SKILL_CATEGORY = 16

# Attribute definitions (default/stackable/highIsGood) are global, small (~2,900 rows)
# and static until a data re-import: cache them once per data version, shared across
# per-evaluation provider instances (same pattern as the skill catalogue).
_ATTR_DEF_CACHE: dict[str, dict[int, AttributeDef]] = {}
# The graph engine materialises EVERY skill (≈590) per evaluation; their type rows,
# attributes and effect lists are static per data version. Prefetching them once per
# process (3 bulk queries) turns a ~1.5 s cold evaluation into ~10 ms.
_SKILL_DATA_CACHE: dict[str, tuple[list[int], dict, dict, dict]] = {}
# EffectDefs (categories + modifier lists) — likewise static per data version.
_EFFECT_DEF_CACHE: dict[str, dict[int, EffectDef]] = {}
# WS-7 warfare buffs (dbuffCollections) — small (~271 buffs) and static per data version.
_DBUFF_CACHE: dict[str, dict[int, DbuffDef]] = {}


class ORMDataProvider:
    """A :class:`~apps.fitting.engine.graph.GraphDataProvider` backed by the SDE tables."""

    def __init__(self):
        # Per-evaluation memoization: a provider is built fresh for each engine
        # evaluation, so caching here bounds queries by DISTINCT type (a fit with six
        # identical guns reads that gun's attrs/skills once, not six times) with no
        # cross-request staleness risk.
        self._rows: dict[int, dict | None] = {}
        self._attrs: dict[int, dict[int, float]] = {}
        self._effects: dict[int, frozenset[int]] = {}
        self._skills: dict[int, list[tuple[int, int]]] = {}
        self._bonuses: dict[int, list[BonusSpec]] = {}
        self._effect_defs: dict[int, EffectDef | None] = {}
        self._subsys_slots: dict[int, int] = {}
        self.data_version = self._resolve_data_version()

    @staticmethod
    def _resolve_data_version() -> str:
        try:
            from apps.admin_audit.models import AppSetting

            # One query for all version keys (query-neutral vs the old per-key reads).
            rows = dict(AppSetting.objects.filter(
                key__in=("dogma_data_version", "sde_version", "ship_bonus_data_version",
                         "dogma_graph_version")
            ).values_list("key", "value"))

            def ver(key):
                return (rows.get(key) or {}).get("version")

            base = str(ver("dogma_data_version") or ver("sde_version") or "unknown")
            # Fold in the ship-bonus catalogue version so re-importing hull bonuses busts the
            # eval cache — otherwise warm entries serve pre-import DPS until the TTL expires.
            bonus = ver("ship_bonus_data_version")
            if bonus:
                base = f"{base}+sb{bonus}"
            # Fold in the dogma-graph version too (import_dogma_graph): re-importing the modifier
            # graph / skill dogma busts the cache once the generic applicator reads it.
            graph = ver("dogma_graph_version")
            if graph:
                base = f"{base}+dg{graph}"
            return base
        except Exception:  # noqa: BLE001 - version is advisory; never break a calc over it
            return "unknown"

    def _row(self, type_id: int) -> dict | None:
        if type_id in self._rows:
            return self._rows[type_id]
        from apps.sde.models import SdeType

        row = (
            SdeType.objects.filter(type_id=type_id)
            .values("type_id", "name", "group_id", "group__category_id", "group__name",
                    "hi_slots", "med_slots", "low_slots", "rig_slots", "mass")
            .first()
        )
        self._rows[type_id] = row
        return row

    def type_info(self, type_id: int) -> dict | None:
        info = getattr(self, "_skill_infos", {}).get(type_id)
        if info is not None:
            return info
        row = self._row(type_id)
        if not row:
            return None
        return {"name": row["name"], "group_id": row["group_id"],
                "category_id": row["group__category_id"],
                "group_name": row["group__name"]}

    def attrs(self, type_id: int) -> dict[int, float]:
        if type_id in self._attrs:
            return self._attrs[type_id]
        from apps.sde.models import SdeTypeAttribute

        d = {
            int(aid): float(val)
            for aid, val in SdeTypeAttribute.objects.filter(type_id=type_id)
            .values_list("attribute_id", "value")
        }
        # Bridge legacy slot-count columns for hulls the dogma import has not covered.
        row = self._row(type_id)
        if row:
            for column, attr in _SLOT_COLUMN_ATTR.items():
                if attr not in d and row.get(column) is not None:
                    d[attr] = float(row[column])
            # Mass (dogma attr 4) is absent from the Fuzzwork dogma export — bridge the
            # invTypes.mass column so mobility (align time / MWD velocity) has a real base.
            if A.MASS not in d and row.get("mass"):
                d[A.MASS] = float(row["mass"])
        self._attrs[type_id] = d
        return d

    def effects(self, type_id: int) -> frozenset[int]:
        if type_id in self._effects:
            return self._effects[type_id]
        from apps.sde.models import SdeTypeEffect

        effs = frozenset(
            SdeTypeEffect.objects.filter(type_id=type_id).values_list("effect_id", flat=True)
        )
        self._effects[type_id] = effs
        return effs

    def required_skills(self, type_id: int) -> list[tuple[int, int]]:
        if type_id in self._skills:
            return self._skills[type_id]
        from apps.sde.models import SdeTypeSkill

        skills = [
            (int(sid), int(lvl))
            for sid, lvl in SdeTypeSkill.objects.filter(type_id=type_id)
            .values_list("skill_type_id", "level")
        ]
        self._skills[type_id] = skills
        return skills

    # -- GraphDataProvider (engine v2 core) --------------------------------- #
    def attr_def(self, attribute_id: int) -> AttributeDef:
        defs = _ATTR_DEF_CACHE.get(self.data_version)
        if defs is None:
            from apps.sde.models import SdeDogmaAttribute

            defs = {
                int(r["attribute_id"]): AttributeDef(
                    default=float(r["default_value"] or 0.0),
                    stackable=bool(r["stackable"]),
                    # high_is_good is nullable (context-dependent); treat unknown as
                    # "high is good", the dominant convention in the data.
                    high_is_good=bool(r["high_is_good"]) if r["high_is_good"] is not None
                    else True,
                )
                for r in SdeDogmaAttribute.objects.values(
                    "attribute_id", "default_value", "stackable", "high_is_good")
            }
            _ATTR_DEF_CACHE[self.data_version] = defs
        return defs.get(attribute_id, AttributeDef())

    def effect_def(self, effect_id: int) -> EffectDef | None:
        defs = _EFFECT_DEF_CACHE.get(self.data_version)
        if defs is None:
            from apps.sde.models import SdeDogmaEffect, SdeModifier

            # Bulk-load the whole effect layer once per data version (~3.4k effects +
            # ~5.2k modifiers in two queries); per-effect lazy loads cost ~0.5 s per
            # request-scoped provider otherwise.
            mods_by_effect: dict[int, list[ModifierDef]] = {}
            for m in SdeModifier.objects.values(
                    "effect_id", "func", "domain", "operation", "modified_attribute_id",
                    "modifying_attribute_id", "group_id", "skill_type_id"):
                mods_by_effect.setdefault(int(m["effect_id"]), []).append(ModifierDef(
                    func=m["func"], domain=m["domain"], operation=m["operation"],
                    modified_attribute_id=m["modified_attribute_id"],
                    modifying_attribute_id=m["modifying_attribute_id"],
                    group_id=m["group_id"], skill_type_id=m["skill_type_id"]))
            defs = {}
            for row in SdeDogmaEffect.objects.values(
                    "effect_id", "effect_category", "duration_attribute_id",
                    "discharge_attribute_id", "range_attribute_id",
                    "falloff_attribute_id", "tracking_attribute_id"):
                eid = int(row["effect_id"])
                defs[eid] = EffectDef(
                    effect_id=eid, category=int(row["effect_category"] or 0),
                    modifiers=tuple(mods_by_effect.get(eid, ())),
                    duration_attribute_id=row["duration_attribute_id"],
                    discharge_attribute_id=row["discharge_attribute_id"],
                    range_attribute_id=row["range_attribute_id"],
                    falloff_attribute_id=row["falloff_attribute_id"],
                    tracking_attribute_id=row["tracking_attribute_id"])
            _EFFECT_DEF_CACHE[self.data_version] = defs
        return defs.get(effect_id)

    def dbuff(self, buff_id: int) -> DbuffDef | None:
        """A warfare-buff definition (WS-7 fleet boosts), or None when the buff id is not in
        the imported dbuff table. Bulk-loaded once per data version (two queries over ~271
        buffs + their modifiers), mirroring effect_def."""
        defs = _DBUFF_CACHE.get(self.data_version)
        if defs is None:
            from apps.sde.models import SdeDbuff, SdeDbuffModifier

            mods_by_buff: dict[int, list[DbuffModifierDef]] = {}
            for m in SdeDbuffModifier.objects.values(
                    "buff_id", "kind", "modified_attribute_id", "group_id", "skill_type_id"):
                mods_by_buff.setdefault(int(m["buff_id"]), []).append(DbuffModifierDef(
                    kind=m["kind"], modified_attribute_id=m["modified_attribute_id"],
                    group_id=m["group_id"], skill_type_id=m["skill_type_id"]))
            defs = {}
            for row in SdeDbuff.objects.values("buff_id", "aggregate_mode", "operation"):
                bid = int(row["buff_id"])
                defs[bid] = DbuffDef(
                    buff_id=bid, aggregate_mode=row["aggregate_mode"],
                    operation=row["operation"], modifiers=tuple(mods_by_buff.get(bid, ())))
            _DBUFF_CACHE[self.data_version] = defs
        return defs.get(buff_id)

    def subsystem_slots_for_hull(self, hull_type_id: int) -> int:
        """The number of distinct subsystem slots a Strategic Cruiser hull exposes — i.e.
        how many subsystems a complete T3C requires. Derived as the count of distinct
        ``subSystemSlot`` (1366) values across the subsystem types that declare
        compatibility with this hull via ``fitsToShipType`` (1380).

        CCP's ``maxSubSystems`` (1367) hull attribute is NOT used: it still reads 5 in the
        SDE (the pre-2016 five-subsystem era) even though every T3C has had exactly four
        subsystem slots since the subsystem consolidation, so trusting it would flag every
        correctly-fitted T3C as invalid. Returns 0 for a hull with no compatible subsystem
        catalogue (e.g. a non-T3C, or a fixture slice that omits the subsystems), which the
        validator treats as "cannot determine — do not flag"."""
        cached = self._subsys_slots.get(hull_type_id)
        if cached is not None:
            return cached
        from apps.sde.models import SdeTypeAttribute

        compatible = list(SdeTypeAttribute.objects.filter(
            attribute_id=A.FITS_TO_SHIP_TYPE, value=float(hull_type_id)
        ).values_list("type_id", flat=True))
        slots = set(SdeTypeAttribute.objects.filter(
            attribute_id=A.SUBSYSTEM_SLOT, type_id__in=compatible
        ).values_list("value", flat=True)) if compatible else set()
        self._subsys_slots[hull_type_id] = len(slots)
        return len(slots)

    def trained_skill_ids(self) -> list[int]:
        """Every skill type id in the DB (category 16) — the candidate set the graph
        engine materialises as skill entities. Bulk-prefetched (with each skill's
        info/attrs/effects) once per data version and seeded into this provider's
        per-instance caches, so a fresh per-request provider stays fast."""
        cached = _SKILL_DATA_CACHE.get(self.data_version)
        if cached is None:
            from apps.sde.models import SdeType, SdeTypeAttribute, SdeTypeEffect

            rows = list(SdeType.objects.filter(group__category_id=_SKILL_CATEGORY)
                        .values("type_id", "name", "group_id", "group__category_id",
                                "group__name"))
            ids = [r["type_id"] for r in rows]
            infos = {r["type_id"]: {"name": r["name"], "group_id": r["group_id"],
                                    "category_id": r["group__category_id"],
                                    "group_name": r["group__name"]} for r in rows}
            attrs: dict[int, dict[int, float]] = {i: {} for i in ids}
            for tid, aid, val in SdeTypeAttribute.objects.filter(type_id__in=ids) \
                    .values_list("type_id", "attribute_id", "value"):
                attrs[tid][int(aid)] = float(val)
            effs: dict[int, set] = {i: set() for i in ids}
            for tid, eid in SdeTypeEffect.objects.filter(type_id__in=ids) \
                    .values_list("type_id", "effect_id"):
                effs[tid].add(int(eid))
            cached = (ids, infos, attrs, {t: frozenset(s) for t, s in effs.items()})
            _SKILL_DATA_CACHE[self.data_version] = cached
        ids, infos, attrs, effs = cached
        for tid in ids:
            self._attrs.setdefault(tid, attrs[tid])
            self._effects.setdefault(tid, effs[tid])
        self._skill_infos = infos                # consulted by type_info() first
        return list(ids)

    def ship_bonuses(self, ship_type_id: int) -> list[BonusSpec]:
        if ship_type_id in self._bonuses:
            return self._bonuses[ship_type_id]
        from apps.sde.models import SdeShipBonus

        specs: list[BonusSpec] = []
        for b in SdeShipBonus.objects.filter(ship_type_id=ship_type_id):
            specs.append(BonusSpec(
                key=b.key, target_attr=b.target_attribute_id, amount=b.amount,
                target_domain=b.target_domain, skill_id=b.skill_type_id, per_level=b.per_level,
                match_group_ids=tuple(b.match_group_ids or ()),
                match_category_ids=tuple(b.match_category_ids or ()),
                match_attr_present=b.match_attr_present,
                match_required_skill_id=b.match_required_skill_id,
                penalised=b.penalised, op=Op.MULTIPLY, label=b.label or b.key,
            ))
        self._bonuses[ship_type_id] = specs
        return specs


class FittingEngine:
    """The single public entry point for fitting calculations."""

    def __init__(self, provider=None):
        self.provider = provider or ORMDataProvider()

    @property
    def engine_version(self) -> str:
        return ENGINE_VERSION

    @property
    def data_version(self) -> str:
        return getattr(self.provider, "data_version", "")

    def evaluate(
        self, fit: FitInput, skills: SkillProfile, op_profile: OperatingProfile | None = None
    ) -> FittingResult:
        """Compute a fit's telemetry (uncached — deterministic; used by services/tests).

        Engine v2: the generic graph evaluator (see the calculation-engine ADR) is the
        sole calculation path. The pyfa differential harness
        (scripts/tochas_lab_differential_pyfa.py) is the independent cross-check."""
        from . import evaluator

        return evaluator.evaluate(fit, skills, op_profile or OperatingProfile(), self.provider)

    def _cache_key(self, fit: FitInput, skills: SkillProfile, op: OperatingProfile) -> str:
        return (f"fit:eval:{self.engine_version}:{self.data_version}:"
                f"{fit.hash()}:{skills.hash()}:{op.hash()}")

    def evaluate_cached(
        self, fit: FitInput, skills: SkillProfile, op_profile: OperatingProfile | None = None
    ) -> dict:
        """Telemetry as a JSON-safe dict, served from a bounded Redis cache when warm.

        Degrades safely: any cache error falls through to a fresh computation. The key
        folds in the engine and data versions, so a data refresh transparently invalidates
        stale entries (no manual purge)."""
        op = op_profile or OperatingProfile()
        key = self._cache_key(fit, skills, op)
        try:
            from django.core.cache import cache

            hit = cache.get(key)
            if hit is not None:
                return hit
        except Exception:  # noqa: BLE001
            cache = None  # type: ignore[assignment]
        result = self.evaluate(fit, skills, op).to_dict()
        try:
            if cache is not None:
                cache.set(key, result, _CACHE_TTL)
        except Exception:  # noqa: BLE001
            log.warning("fitting cache set failed", exc_info=True)
        return result
