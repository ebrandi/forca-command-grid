"""The FORCA-owned engine boundary.

Everything outside :mod:`apps.fitting.engine` calls the fitting engine ONLY through
:class:`FittingEngine` here — never ``dogma.evaluate`` directly. This is what lets the
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
from . import dogma
from .bonuses import BonusSpec
from .effects import Op
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
_OP_POSTPERCENT = 6
# The full data-driven skill catalogue (hand-coded + graph-derived) is static until the dogma
# graph is re-imported, so build it once and memoise by data version (which folds the graph
# version). Module-level so it is shared across the per-evaluation provider instances.
_SKILL_CATALOGUE_CACHE: dict[str, list[BonusSpec]] = {}


def _graph_skills_enabled() -> bool:
    """Whether to data-drive the skill catalogue from the dogma graph (Phase 2 cutover flag)."""
    try:
        from django.conf import settings
        return bool(getattr(settings, "FITTING_GRAPH_SKILLS", False))
    except Exception:  # noqa: BLE001 - a missing/ broken setting must never break a calc
        return False


class ORMDataProvider:
    """A :class:`~apps.fitting.engine.dogma.DataProvider` backed by the SDE tables."""

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
            # Fold in the graph-skills flag (Phase 2): toggling FITTING_GRAPH_SKILLS changes the
            # computed numbers, so it must change the cache key or warm entries would serve the
            # pre-cutover values until the TTL expires.
            if _graph_skills_enabled():
                base = f"{base}+gs1"
            return base
        except Exception:  # noqa: BLE001 - version is advisory; never break a calc over it
            return "unknown"

    def _row(self, type_id: int) -> dict | None:
        if type_id in self._rows:
            return self._rows[type_id]
        from apps.sde.models import SdeType

        row = (
            SdeType.objects.filter(type_id=type_id)
            .values("type_id", "name", "group_id", "group__category_id",
                    "hi_slots", "med_slots", "low_slots", "rig_slots", "mass")
            .first()
        )
        self._rows[type_id] = row
        return row

    def type_info(self, type_id: int) -> dict | None:
        row = self._row(type_id)
        if not row:
            return None
        return {"name": row["name"], "group_id": row["group_id"],
                "category_id": row["group__category_id"]}

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

    def skill_bonuses_all(self) -> list[BonusSpec] | None:
        """The skill-bonus catalogue the evaluator should use.

        Returns ``None`` (→ the evaluator falls back to the hand-coded ``STANDARD_SKILL_BONUSES``)
        when the Phase-2 flag is off or the dogma graph is not imported. When on, returns the
        hand-coded set MERGED with graph-derived specs for every *other* skill — so all trained
        skills apply, with zero change to the skills the hand-coded set already covers. Built once
        and memoised by data version (see ``_SKILL_CATALOGUE_CACHE``)."""
        if not _graph_skills_enabled():
            return None
        cached = _SKILL_CATALOGUE_CACHE.get(self.data_version)
        if cached is not None:
            return cached
        specs = self._build_skill_catalogue()
        _SKILL_CATALOGUE_CACHE[self.data_version] = specs
        return specs

    @staticmethod
    def _build_skill_catalogue() -> list[BonusSpec]:
        from apps.sde.models import SdeModifier, SdeType, SdeTypeAttribute, SdeTypeEffect

        from .bonuses import STANDARD_SKILL_BONUSES
        from .modifiers import build_skill_bonus_specs

        skill_ids = list(
            SdeType.objects.filter(group__category_id=_SKILL_CATEGORY)
            .values_list("type_id", flat=True)
        )
        if not skill_ids:                       # graph/skill dogma not imported → hand-coded only
            return list(STANDARD_SKILL_BONUSES)

        skill_attrs: dict[int, dict[int, float]] = {}
        for tid, aid, val in (
            SdeTypeAttribute.objects.filter(type_id__in=skill_ids)
            .values_list("type_id", "attribute_id", "value")
        ):
            skill_attrs.setdefault(int(tid), {})[int(aid)] = float(val)

        effect_ids_by_skill: dict[int, list[int]] = {}
        effect_ids: set[int] = set()
        for tid, eid in (
            SdeTypeEffect.objects.filter(type_id__in=skill_ids)
            .values_list("type_id", "effect_id")
        ):
            effect_ids_by_skill.setdefault(int(tid), []).append(int(eid))
            effect_ids.add(int(eid))

        modifiers_by_effect: dict[int, list] = {}
        for m in SdeModifier.objects.filter(effect_id__in=effect_ids, operation=_OP_POSTPERCENT):
            modifiers_by_effect.setdefault(m.effect_id, []).append(m)

        labels = dict(
            SdeType.objects.filter(type_id__in=skill_ids).values_list("type_id", "name")
        )
        exclude = frozenset(s.skill_id for s in STANDARD_SKILL_BONUSES if s.skill_id)
        data_specs = build_skill_bonus_specs(
            skill_attrs, effect_ids_by_skill, modifiers_by_effect, labels, exclude)
        return [*STANDARD_SKILL_BONUSES, *data_specs]


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
        """Compute a fit's telemetry (uncached — deterministic; used by services/tests)."""
        return dogma.evaluate(fit, skills, op_profile or OperatingProfile(), self.provider)

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
