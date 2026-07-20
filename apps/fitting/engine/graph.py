"""The generic dogma evaluator — passes 1-3 of the fitting calculation.

Evaluates every fitted entity's attributes from the imported CCP dogma modifier graph
(``SdeModifier`` via the provider), instead of a hand-enumerated effect list. This is the
engine-v2 core selected by the calculation-engine ADR
(docs/architecture/decisions/tochas-lab-calculation-engine.md):

* **Pass 1** builds entities — the character (holding one entity per trained skill, with
  dogma attribute 280 = trained level), the hull, each module (with its loaded charge as a
  child), drones, implants and boosters — seeded with their base dogma attributes.
* **Pass 2** collects every dogma effect each entity carries, resolves the effect's
  modifiers (func + domain + operation) onto concrete (target entity, target attribute)
  pairs, and gates them by the source entity's state (offline < online < active <
  overloaded, mapped from the effect's category).
* **Pass 3** computes attribute values lazily and recursively: operators apply in the
  canonical order (preAssign, preMul, preDiv, modAdd, modSub, postMul, postDiv,
  postPercent, postAssign); multiplicative operators on a non-``stackable`` attribute are
  stacking-penalised (factor exp(-(i/2.67)^2), positive and negative chains penalised
  separately) unless the SOURCE entity's category is exempt (ship, charge, skill, implant,
  subsystem); assignment conflicts resolve by the attribute's ``high_is_good`` direction.

The semantics implemented here are CCP's public dogma data model (operation codes,
modifier functions and domains are CCP data; per-level skill scaling is encoded by CCP as
modifiers whose modifying attribute is 280/skillLevel). They were cross-checked against
the MIT-licensed EVEShipFit dogma-engine (commit e8e536b) as a reference; this
implementation is original and written for FORCA's own data model — see the ADR's
provenance note.

Anything the evaluator cannot interpret (unknown func, unknown operation, unresolvable
domain) is surfaced in ``EvaluatedFit.diagnostics`` — never silently dropped.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

from .types import FitInput, ModuleState, SkillProfile, SlotKind

# ---------------------------------------------------------------------------
# Constants (CCP dogma data model)
# ---------------------------------------------------------------------------
ATTR_SKILL_LEVEL = 280
ATTR_MASS = 4
# (requiredSkillN, requiredSkillNLevel) — see attributes.REQUIRED_SKILLS; the engine
# matches RequiredSkill modifiers against the *skill-id* attributes only.
REQUIRED_SKILL_ATTRS = (182, 183, 184, 1285, 1289, 1290)

# Source categories exempt from the stacking penalty: Ship, Charge, Skill, Implant
# (which includes boosters), Subsystem.
EXEMPT_PENALTY_CATEGORIES = frozenset({6, 8, 16, 20, 32})

# Effect categories → the minimum entity state at which the effect applies.
# 0 passive, 4 online, 1 active, 5 overload. Target(2)/area(3)/dungeon(6)/system(7)
# effects act on other ships or the environment — not applied locally.
STATE_OFFLINE, STATE_ONLINE, STATE_ACTIVE, STATE_OVERLOADED = 0, 1, 2, 3
_CATEGORY_STATE = {0: STATE_OFFLINE, 4: STATE_ONLINE, 1: STATE_ACTIVE, 5: STATE_OVERLOADED}
_NONLOCAL_CATEGORIES = frozenset({2, 3, 6, 7})

_MODULE_STATE_RANK = {
    ModuleState.OFFLINE: None,               # collects nothing
    ModuleState.ONLINE: STATE_ONLINE,
    ModuleState.ACTIVE: STATE_ACTIVE,
    ModuleState.OVERHEATED: STATE_OVERLOADED,
}

# Operator codes (CCP modifierInfo.operation) in canonical application order.
OP_PRE_ASSIGN, OP_PRE_MUL, OP_PRE_DIV = -1, 0, 1
OP_MOD_ADD, OP_MOD_SUB = 2, 3
OP_POST_MUL, OP_POST_DIV, OP_POST_PERCENT, OP_POST_ASSIGN = 4, 5, 6, 7
OP_SKILL_LEVEL = 9                            # skill-points→level; irrelevant to fits
OPERATOR_ORDER = (OP_PRE_ASSIGN, OP_PRE_MUL, OP_PRE_DIV, OP_MOD_ADD, OP_MOD_SUB,
                  OP_POST_MUL, OP_POST_DIV, OP_POST_PERCENT, OP_POST_ASSIGN)
_PENALISABLE_OPS = frozenset({OP_PRE_MUL, OP_PRE_DIV, OP_POST_MUL, OP_POST_DIV,
                              OP_POST_PERCENT})
# exp(-(1/2.67)^2) — same constant the validated stacking module derives; kept inline so
# the evaluator is self-contained (see stacking.py for the reproduced published table).
_PENALTY_FACTOR = math.exp(-((1.0 / 2.67) ** 2))

_FUNCS = ("ItemModifier", "LocationModifier", "LocationGroupModifier",
          "LocationRequiredSkillModifier", "OwnerRequiredSkillModifier")
_FUNC_EFFECT_STOPPER = "EffectStopper"        # gates onlining, changes no attribute


# ---------------------------------------------------------------------------
# Provider contract
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AttributeDef:
    default: float = 0.0
    stackable: bool = True
    high_is_good: bool = True


@dataclass(frozen=True)
class ModifierDef:
    func: str
    domain: str                    # shipID | charID | itemID | otherID | targetID | ...
    operation: int | None
    modified_attribute_id: int | None
    modifying_attribute_id: int | None
    group_id: int | None = None
    skill_type_id: int | None = None


@dataclass(frozen=True)
class EffectDef:
    effect_id: int
    category: int                  # SDE effectCategory
    modifiers: tuple[ModifierDef, ...] = ()
    duration_attribute_id: int | None = None
    discharge_attribute_id: int | None = None
    range_attribute_id: int | None = None
    falloff_attribute_id: int | None = None
    tracking_attribute_id: int | None = None


class GraphDataProvider(Protocol):
    """What the generic evaluator needs from the data layer (superset of the legacy
    provider protocol; see adapter.ORMDataProvider)."""

    def type_info(self, type_id: int) -> dict | None: ...
    def attrs(self, type_id: int) -> dict[int, float]: ...
    def effects(self, type_id: int) -> frozenset[int]: ...
    def attr_def(self, attribute_id: int) -> AttributeDef: ...
    def effect_def(self, effect_id: int) -> EffectDef | None: ...


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------
@dataclass
class _Application:
    """One collected modifier application onto (target entity, target attribute)."""
    operation: int
    source: "Entity"
    source_attr: int
    min_state: int                 # source must be in >= this state
    penalisable_source: bool       # source category NOT exempt from stacking


@dataclass
class Entity:
    type_id: int
    kind: str                      # ship|char|skill|module|charge|drone|implant|booster
    state: int                     # STATE_* rank this entity is in
    category_id: int | None = None
    group_id: int | None = None
    quantity: int = 1
    base: dict[int, float] = field(default_factory=dict)
    applications: dict[int, list[_Application]] = field(default_factory=dict)
    values: dict[int, float] = field(default_factory=dict)
    parent: "Entity | None" = None  # charge -> its module
    charge: "Entity | None" = None  # module -> its charge
    effect_ids: frozenset[int] = frozenset()
    slot: SlotKind | None = None
    module_state: ModuleState | None = None


@dataclass
class EvaluatedFit:
    """Pass-3 output: every entity with resolvable attribute values."""
    ship: Entity
    char: Entity
    skills: list[Entity]
    modules: list[Entity]          # racked modules incl. subsystems/rigs (not drones)
    drones: list[Entity]
    implants: list[Entity]
    diagnostics: list[str] = field(default_factory=list)
    _provider: GraphDataProvider | None = None
    _stack: list[tuple[int, int]] = field(default_factory=list)

    def value(self, entity: Entity, attribute_id: int) -> float:
        """The evaluated value of ``attribute_id`` on ``entity`` (lazy, cached)."""
        cached = entity.values.get(attribute_id)
        if cached is not None:
            return cached
        v = _calculate(self, entity, attribute_id)
        entity.values[attribute_id] = v
        return v

    def ship_value(self, attribute_id: int) -> float:
        return self.value(self.ship, attribute_id)


# ---------------------------------------------------------------------------
# Pass 1 — entity construction
# ---------------------------------------------------------------------------
_CHAR_TYPE_ID = 1373  # generic capsuleer type; carries no fit-relevant base attrs


def _new_entity(provider, type_id: int, kind: str, state: int, **kw) -> Entity:
    info = provider.type_info(type_id) or {}
    return Entity(
        type_id=type_id, kind=kind, state=state,
        category_id=info.get("category_id"), group_id=info.get("group_id"),
        base=dict(provider.attrs(type_id)),
        effect_ids=provider.effects(type_id),
        **kw,
    )


def build_entities(fit: FitInput, skills: SkillProfile, provider,
                   skill_ids: list[int] | None = None) -> EvaluatedFit:
    """Pass 1: create all entities with base attributes.

    ``skill_ids`` is the trained-skill id list to materialise as entities. For an
    ``all_five`` profile the caller must pass the full catalogue of skill ids (the
    profile itself cannot enumerate them)."""
    ship = _new_entity(provider, fit.ship_type_id, "ship", STATE_OVERLOADED)
    char = Entity(type_id=_CHAR_TYPE_ID, kind="char", state=STATE_OVERLOADED,
                  # Multiplier identity — never let a missing dogma default zero it.
                  base={_ATTR_CHAR_MISSILE_MULT: 1.0})
    ev = EvaluatedFit(ship=ship, char=char, skills=[], modules=[], drones=[],
                      implants=[], _provider=provider)

    for sid in (skill_ids or []):
        level = skills.level(sid)
        if level <= 0:
            continue
        s = _new_entity(provider, sid, "skill", STATE_OVERLOADED)
        s.base[ATTR_SKILL_LEVEL] = float(level)
        ev.skills.append(s)

    for m in fit.modules:
        if m.slot in (SlotKind.HIGH, SlotKind.MED, SlotKind.LOW, SlotKind.RIG,
                      SlotKind.SUBSYSTEM, SlotKind.SERVICE):
            rank = _MODULE_STATE_RANK[m.state]
            if rank is None:
                # Offline: present for slot accounting, applies no effects.
                mod = _new_entity(provider, m.type_id, "module", STATE_OFFLINE,
                                  slot=m.slot, module_state=m.state)
                mod.effect_ids = frozenset()
                ev.modules.append(mod)
                continue
            mod = _new_entity(provider, m.type_id, "module", rank,
                              slot=m.slot, module_state=m.state)
            if m.charge_type_id:
                ch = _new_entity(provider, m.charge_type_id, "charge", STATE_ACTIVE)
                ch.parent = mod
                mod.charge = ch
            ev.modules.append(mod)
        elif m.slot == SlotKind.DRONE:
            if m.state == ModuleState.OFFLINE:
                continue                        # drone in bay: inert
            d = _new_entity(provider, m.type_id, "drone", STATE_ACTIVE,
                            quantity=m.quantity, slot=m.slot, module_state=m.state)
            ev.drones.append(d)
        elif m.slot in (SlotKind.IMPLANT, SlotKind.BOOSTER):
            imp = _new_entity(provider, m.type_id, "implant", STATE_OVERLOADED,
                              slot=m.slot, module_state=m.state)
            ev.implants.append(imp)
        # CARGO / FIGHTER: inert for attribute evaluation (fighters unsupported v2.0).
    return ev


# ---------------------------------------------------------------------------
# Pass 2 — effect collection
# ---------------------------------------------------------------------------
# Builtin character effect (documented data patch, mirrors the client-internal
# missile-damage chain): Ballistic Control Systems pre-multiply the CHARACTER's
# missileDamageMultiplier (attr 212, dogma default 1.0); the client then applies the
# char's 212 to every missile requiring Missile Launcher Operation (3319). CCP ships no
# dogma for the second half, so the engine carries it as a builtin effect on the char.
_ATTR_CHAR_MISSILE_MULT = 212
_SKILL_MISSILE_LAUNCHER_OP = 3319
_CHAR_BUILTIN_EFFECTS = (
    EffectDef(
        effect_id=-1, category=0,
        modifiers=tuple(
            ModifierDef(func="OwnerRequiredSkillModifier", domain="charID",
                        operation=OP_POST_MUL, modified_attribute_id=attr,
                        modifying_attribute_id=_ATTR_CHAR_MISSILE_MULT,
                        skill_type_id=_SKILL_MISSILE_LAUNCHER_OP)
            for attr in (114, 116, 117, 118)),  # em/expl/kin/therm damage
    ),
)
def _location_entities(ev: EvaluatedFit) -> list[Entity]:
    """Entities "located on the ship": hull, modules, charges and drones."""
    out = [ev.ship]
    for m in ev.modules:
        out.append(m)
        if m.charge is not None:
            out.append(m.charge)
    out.extend(ev.drones)
    return out


def _add_application(target: Entity, attr_id: int, app: _Application) -> None:
    target.applications.setdefault(attr_id, []).append(app)


def _required_skill_matches(entity: Entity, skill_type_id: int) -> bool:
    for attr in REQUIRED_SKILL_ATTRS:
        if entity.base.get(attr) == float(skill_type_id):
            return True
    return False


def collect_effects(ev: EvaluatedFit, provider) -> None:
    """Pass 2: attach every applicable modifier to its target attribute."""
    sources: list[Entity] = [ev.ship, *ev.skills, *ev.implants, *ev.drones]
    for m in ev.modules:
        sources.append(m)
        if m.charge is not None:
            sources.append(m.charge)

    for edef in _CHAR_BUILTIN_EFFECTS:
        for mod in edef.modifiers:
            _collect_one(ev, ev.char, edef, mod, STATE_OFFLINE, exempt=True)

    for src in sources:
        exempt = src.category_id in EXEMPT_PENALTY_CATEGORIES
        for eid in src.effect_ids:
            edef = provider.effect_def(eid)
            if edef is None:
                ev.diagnostics.append(f"unknown_effect:{eid}:type{src.type_id}")
                continue
            if edef.category in _NONLOCAL_CATEGORIES:
                continue                       # projected/environment: not applied locally
            min_state = _CATEGORY_STATE.get(edef.category)
            if min_state is None:
                ev.diagnostics.append(f"unknown_effect_category:{edef.category}:{eid}")
                continue
            if src.state < min_state:
                continue                       # source can never reach this state now
            for mod in edef.modifiers:
                _collect_one(ev, src, edef, mod, min_state, exempt)


def _collect_one(ev: EvaluatedFit, src: Entity, edef: EffectDef, mod: ModifierDef,
                 min_state: int, exempt: bool) -> None:
    if mod.func == _FUNC_EFFECT_STOPPER:
        return                                  # affects onlining rules, not attributes
    if mod.func not in _FUNCS:
        ev.diagnostics.append(f"unknown_modifier_func:{mod.func}:{edef.effect_id}")
        return
    op = mod.operation
    if op == OP_SKILL_LEVEL:
        return                                  # skill-points bookkeeping, not a fit input
    if op not in (OP_PRE_ASSIGN, OP_PRE_MUL, OP_PRE_DIV, OP_MOD_ADD, OP_MOD_SUB,
                  OP_POST_MUL, OP_POST_DIV, OP_POST_PERCENT, OP_POST_ASSIGN):
        ev.diagnostics.append(f"unknown_operation:{op}:{edef.effect_id}")
        return
    tgt_attr = mod.modified_attribute_id
    src_attr = mod.modifying_attribute_id
    if tgt_attr is None or src_attr is None:
        ev.diagnostics.append(f"incomplete_modifier:{edef.effect_id}")
        return

    app = _Application(operation=op, source=src, source_attr=src_attr,
                       min_state=min_state, penalisable_source=not exempt)

    if mod.func == "ItemModifier":
        target = _resolve_domain(ev, src, mod.domain)
        if target is None:
            # A module with no charge loaded legitimately has no "other" — skip quietly.
            # Anything else unresolved is a data/coverage problem and must be visible.
            if not (mod.domain == "otherID" and src.kind == "module" and src.charge is None):
                ev.diagnostics.append(f"unresolved_domain:{mod.domain}:{edef.effect_id}")
            return
        _add_application(target, tgt_attr, app)
        return

    if mod.func == "LocationModifier":
        for ent in _location_entities(ev):
            _add_application(ent, tgt_attr, app)
        return

    if mod.func == "LocationGroupModifier":
        if mod.group_id is None:
            ev.diagnostics.append(f"incomplete_modifier:{edef.effect_id}")
            return
        for ent in _location_entities(ev):
            if ent.group_id == mod.group_id:
                _add_application(ent, tgt_attr, app)
        return

    # RequiredSkill modifiers: apply to every located entity that REQUIRES the skill.
    # skillTypeID -1 means "the skill this effect belongs to" (IfSkillRequired).
    skill_id = mod.skill_type_id
    if skill_id is None:
        ev.diagnostics.append(f"incomplete_modifier:{edef.effect_id}")
        return
    if skill_id == -1:
        skill_id = src.type_id
    for ent in _location_entities(ev):
        if _required_skill_matches(ent, skill_id):
            _add_application(ent, tgt_attr, app)


def _resolve_domain(ev: EvaluatedFit, src: Entity, domain: str) -> Entity | None:
    if domain == "shipID":
        return ev.ship
    if domain == "charID":
        return ev.char
    if domain == "itemID":
        return src
    if domain == "otherID":
        if src.kind == "module":
            return src.charge                  # may be None (no charge loaded) → skip
        if src.kind == "charge":
            return src.parent
        return None
    return None                                # targetID/structureID: projected, v2 skip


# ---------------------------------------------------------------------------
# Pass 3 — lazy recursive attribute resolution
# ---------------------------------------------------------------------------
def _calculate(ev: EvaluatedFit, entity: Entity, attribute_id: int) -> float:
    provider = ev._provider
    adef = provider.attr_def(attribute_id)
    key = (id(entity), attribute_id)
    if key in ev._stack:
        # A dependency cycle in the data — evaluate from base to stay deterministic.
        ev.diagnostics.append(f"modifier_cycle:{attribute_id}:type{entity.type_id}")
        return entity.base.get(attribute_id, adef.default)
    apps = entity.applications.get(attribute_id, ())
    current = entity.base.get(attribute_id, adef.default)
    if not apps:
        return current

    ev._stack.append(key)
    try:
        for op in OPERATOR_ORDER:
            plain: list[float] = []
            pen_pos: list[float] = []
            pen_neg: list[float] = []
            assigns: list[float] = []
            for app in apps:
                if app.operation != op:
                    continue
                if app.source.state < app.min_state:
                    continue
                raw = ev.value(app.source, app.source_attr)
                if op in (OP_PRE_ASSIGN, OP_POST_ASSIGN):
                    assigns.append(raw)
                    continue
                if op in (OP_MOD_ADD, OP_MOD_SUB):
                    plain.append(raw if op == OP_MOD_ADD else -raw)
                    continue
                # Multiplicative family — normalise to a fractional delta v (×(1+v)).
                if op in (OP_PRE_MUL, OP_POST_MUL):
                    v = raw - 1.0
                elif op in (OP_PRE_DIV, OP_POST_DIV):
                    if raw == 0:
                        ev.diagnostics.append(
                            f"div_by_zero:{attribute_id}:type{entity.type_id}")
                        continue
                    v = 1.0 / raw - 1.0
                else:                          # OP_POST_PERCENT
                    v = raw / 100.0
                penalised = (not adef.stackable and app.penalisable_source
                             and op in _PENALISABLE_OPS)
                if penalised:
                    (pen_pos if v >= 0 else pen_neg).append(v)
                else:
                    plain.append(v)

            if not (plain or pen_pos or pen_neg or assigns):
                continue
            if op in (OP_PRE_ASSIGN, OP_POST_ASSIGN):
                current = (max(assigns, key=abs) if adef.high_is_good
                           else min(assigns, key=abs))
            elif op in (OP_MOD_ADD, OP_MOD_SUB):
                current += sum(plain)
            else:
                for v in plain:
                    current *= 1.0 + v
                for chain in (pen_pos, pen_neg):
                    chain.sort(key=abs, reverse=True)
                    for i, v in enumerate(chain):
                        current *= 1.0 + v * (_PENALTY_FACTOR ** (i * i))
    finally:
        ev._stack.pop()
    return current


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def evaluate_attributes(fit: FitInput, skills: SkillProfile, provider,
                        skill_ids: list[int] | None = None) -> EvaluatedFit:
    """Run passes 1-3 and return the evaluated entity graph (telemetry — pass 4 — is
    derived by the caller; see evaluator.py)."""
    ev = build_entities(fit, skills, provider, skill_ids=skill_ids)
    collect_effects(ev, provider)
    return ev
