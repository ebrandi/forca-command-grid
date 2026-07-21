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

from .types import BoostInput, FitInput, ModuleState, SkillProfile, SlotKind

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

# WS-6 projected effects. A hostile module projected onto our ship applies its
# effect-category-2 (target) modifiers whose domain is ``targetID`` onto the hull. The
# effect category at which a projected source is considered running:
_STATE_TARGET = 2                             # SDE effectCategory 2 = target (projected)
# Incoming-EWAR resistance: which of the TARGET's (our hull's) resistance attributes
# scales each projected stat effect. Keyed by the family's default effect id; values are
# CCP dogma attribute ids (verified live 2026-07-21, all default 1.0 — a no-op for an
# unresisted victim, and CCP uses ~1e-6, never exactly 0, for full immunity). CCP also
# ships resistanceAttributeID on the effect, but inconsistently (pyfa reads it only for
# neut/nos/ECM — eos/modifiedAttributeDict.py getResistance); this documented map keyed by
# the family's default effect id is the authoritative, DB-verified source. neut/nos and
# remote-rep resistances are applied in the evaluator (evaluator._capacitor / _defence),
# not here, because those families carry no dogma modifier — their numbers are read
# directly off the module.
_PROJECTED_RESISTANCE_BY_EFFECT = {
    6426: 2115,  # remoteWebifierFalloff    → stasisWebifierResistance
    6425: 2114,  # remoteTargetPaintFalloff → targetPainterResistance
    6422: 2112,  # remoteSensorDampFalloff  → sensorDampenerResistance
}

# ---------------------------------------------------------------------------
# WS-7 fleet boosts (warfare buffs from friendly command bursts)
# ---------------------------------------------------------------------------
# A burst charge names up to four warfare buffs: warfareBuffNID says WHICH buff (a
# dbuffCollections id), warfareBuffNMultiplier its strength. The burst MODULE carries the
# base warfareBuffNValue (1.0 for T1, 1.25 for T2); its default effect chargeBonusWarfareCharge
# (6737) postMultiplies that base by the charge's multiplier (verified SdeModifier rows,
# 2026-07-21). So an UNBONUSED T1 burst yields effective strength = 1.0 × multiplier = the
# charge's multiplier — the data-derived default this engine applies (documented v1
# simplification: a real command ship's warfare-strength bonuses + specialist skill are not
# modelled; strength_pct overrides the default for that scenario).
_BUFF_ID_ATTRS = (2468, 2470, 2472, 2536)      # warfareBuff{1..4}ID
_BUFF_MULTIPLIER_ATTRS = (2596, 2597, 2598, 2599)  # warfareBuff{1..4}Multiplier
_UNBONUSED_BURST_BASE = 1.0                    # a T1 command burst module's warfareBuffNValue
# CCP dbuff operationName → the dogma operator the buff applies with. Every value observed
# in the current dbuffCollections.yaml is covered (2026-07-21). The stacking penalty is NOT
# encoded here — it falls out of the TARGET attribute's stackable flag in pass 3, exactly as
# for a fitted module bonus (verified: every pyfa warfare-buff penalty choice matches the
# attribute's stackable flag; pyfa eos/saveddata/fit.py:619-730 boostItemAttr stackingPenalties
# + modifiedAttributeDict.py:404-430 default penalty group shared with local modules).
_DBUFF_OPERATION = {
    "PreAssignment": OP_PRE_ASSIGN,
    "PostPercent": OP_POST_PERCENT,
    "PostMul": OP_POST_MUL,
    "ModAdd": OP_MOD_ADD,
    "PostAssignment": OP_POST_ASSIGN,
}


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


@dataclass(frozen=True)
class DbuffModifierDef:
    kind: str                      # item | location | locationGroup | locationRequiredSkill
    modified_attribute_id: int
    group_id: int | None = None
    skill_type_id: int | None = None


@dataclass(frozen=True)
class DbuffDef:
    buff_id: int
    aggregate_mode: str            # Maximum | Minimum
    operation: str                 # PostPercent | PostMul | ModAdd | Post/PreAssignment
    modifiers: tuple[DbuffModifierDef, ...] = ()


class GraphDataProvider(Protocol):
    """What the generic evaluator needs from the data layer (superset of the legacy
    provider protocol; see adapter.ORMDataProvider)."""

    def type_info(self, type_id: int) -> dict | None: ...
    def attrs(self, type_id: int) -> dict[int, float]: ...
    def effects(self, type_id: int) -> frozenset[int]: ...
    def attr_def(self, attribute_id: int) -> AttributeDef: ...
    def effect_def(self, effect_id: int) -> EffectDef | None: ...
    def dbuff(self, buff_id: int) -> DbuffDef | None: ...


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------
@dataclass
class _Application:
    """One collected modifier application onto (target entity, target attribute)."""
    operation: int
    source: Entity
    source_attr: int
    min_state: int                 # source must be in >= this state
    penalisable_source: bool       # source category NOT exempt from stacking
    # WS-6: when set, this is a PROJECTED application and the source attribute value is
    # scaled by the target hull's resistance attribute (this id) before the operator runs
    # — modelling incoming-ewar resistance. None for every normal (self) application.
    resistance_attr: int | None = None
    # WS-7: a fleet-boost application carries its already-computed warfare-buff value here
    # (the aggregated, override-resolved strength) instead of reading a source attribute —
    # the buff value is not a dogma attribute on any fitted entity. None for every attribute
    # -sourced application (modules, skills, projected).
    literal_value: float | None = None


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
    parent: Entity | None = None  # charge -> its module
    charge: Entity | None = None  # module -> its charge
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
    # WS-12: fighter squadrons launched by this fit (one entity per squadron; quantity =
    # squadron count). Materialised exactly like drones and included in the "located on the
    # ship" set so carrier hull traits + fighter skills (all OwnerRequiredSkillModifier rows
    # onto the Fighters skill) reach them through the ordinary pass-2/3 machinery.
    fighters: list[Entity] = field(default_factory=list)
    # A tactical destroyer's active mode (T3D "Ship Modifiers" type), or None. Materialised
    # like an always-on module; its effects apply only when it is valid for the hull.
    mode: Entity | None = None
    mode_valid: bool = True
    # WS-6: hostile modules projected ONTO this ship (one entity per unit; a quantity-N
    # projected input expands to N sources so a stacking chain is evaluated correctly).
    # These are never on our ship — they are pure sources whose target is the hull.
    projected: list[Entity] = field(default_factory=list)
    # WS-7: friendly fleet command bursts boosting this fit (the raw inputs, carried so the
    # boost pass can read each burst charge's warfare-buff attributes) + a per-boost record
    # of what each one applied (charge_type_id + resolved buffs), which pass 4 renders as the
    # ``boosts`` telemetry section and turns into ``boost_unknown_buff`` diagnostics.
    boost_inputs: tuple[BoostInput, ...] = ()
    boosts_applied: list[dict] = field(default_factory=list)
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


def _apply_overrides(entity: Entity, module_input) -> None:
    """WS-11: fold a mutated module's rolled attribute overrides onto its base attributes.

    The override REPLACES the provider's base value for that attribute (and ADDS the
    attribute when the base type carries none — an abyssal SdeType stores only structural
    attrs, its rolled damageMultiplier/etc. live entirely in the override). Everything
    downstream — skills, module/ship bonuses, stacking, validations, telemetry — reads the
    merged base through the normal pass-3 machinery, so no other special-casing is needed:
    a mutated gyro's overridden damageMultiplier flows through the same LocationGroupModifier
    chain (and stacking penalty) a normal gyro's does."""
    overrides = getattr(module_input, "attr_overrides", ())
    if overrides:
        entity.base.update(module_input.overrides_map())


# Tactical destroyers (and CCP's one-off "Anhinga") are the only hulls that carry a mode.
# CCP ships NO data link between a hull and its modes — the mode type has no
# fitsToShipType / canFitShipType* / canFitShipGroup* / requiredSkill attribute, and the
# hull enumerates no mode ids (verified against the imported SDE, 2026-07-21). The only tie
# is the one pyfa relies on (eos/saveddata/ship.py:123-139, "race is not reliable"): a mode
# lives in group "Ship Modifiers" and its NAME begins with the hull's name ("Confessor
# Defense Mode" ↔ "Confessor"). Since modes exist only for mode-carrying hulls and are named
# after them, the group + name-prefix pair is both sufficient and necessary.
_MODE_GROUP_NAME = "Ship Modifiers"


def mode_valid_for_ship(provider, ship_type_id: int, mode_type_id: int) -> bool:
    """Whether ``mode_type_id`` is a tactical mode belonging to ``ship_type_id``'s hull."""
    mode = provider.type_info(mode_type_id) or {}
    if mode.get("group_name") != _MODE_GROUP_NAME:
        return False                            # not a mode type at all
    ship = provider.type_info(ship_type_id) or {}
    ship_name = (ship.get("name") or "").strip().lower()
    mode_name = (mode.get("name") or "").strip().lower()
    return bool(ship_name) and mode_name.startswith(ship_name)


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
        # Materialise EVERY candidate skill, including level 0: per-level bonuses are
        # encoded in data as a pre-multiplication of the bonus attribute by attr 280,
        # so an untrained skill must exist (with 280 = 0) to zero its bonus. Without
        # the entity, a hull trait would apply at its raw (level-1) strength untrained.
        s = _new_entity(provider, sid, "skill", STATE_OVERLOADED)
        s.base[ATTR_SKILL_LEVEL] = float(skills.level(sid))
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
                _apply_overrides(mod, m)
                ev.modules.append(mod)
                continue
            mod = _new_entity(provider, m.type_id, "module", rank,
                              slot=m.slot, module_state=m.state)
            _apply_overrides(mod, m)
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
            _apply_overrides(d, m)
            ev.drones.append(d)
        elif m.slot in (SlotKind.IMPLANT, SlotKind.BOOSTER):
            imp = _new_entity(provider, m.type_id, "implant", STATE_OVERLOADED,
                              slot=m.slot, module_state=m.state)
            ev.implants.append(imp)
        # CARGO: inert. A stray slot="fighter" ModuleInput is also ignored here — real
        # squadrons ride fit.fighters (materialised below), not the module list.

    # WS-12: materialise each fighter squadron as an ACTIVE entity (like a drone), carrying
    # the squadron count as ``quantity``. It joins the "located on the ship" set (see
    # _location_entities) so the carrier's fighter-damage hull trait and the Fighters /
    # racial-fighter / Drone-Interfacing skills — all OwnerRequiredSkillModifier rows filtered
    # by a fighter's required skill — apply to its fighterAbility* damage multipliers through
    # the ordinary pass-2/3 pipeline, with no fighter-specific modifier code.
    for f in fit.fighters:
        ev.fighters.append(_new_entity(provider, f.type_id, "fighter", STATE_ACTIVE,
                                       quantity=max(1, f.count), slot=SlotKind.FIGHTER,
                                       module_state=ModuleState.ACTIVE))

    # A tactical destroyer's mode is materialised as an always-on entity (ACTIVE covers its
    # passive/online/active effect categories). Its effects are plain dogma (ItemModifier /
    # LocationRequiredSkillModifier onto the ship), so once it is a source they flow through
    # the normal pipeline — category 7 type, so NOT stacking-exempt, exactly like a module.
    # An invalid mode is still recorded (for the diagnostic + UI echo) but contributes no
    # effects, so a mismatched mode never silently rewrites the hull's numbers.
    if fit.mode_type_id:
        ev.mode = _new_entity(provider, fit.mode_type_id, "mode", STATE_ACTIVE)
        ev.mode_valid = mode_valid_for_ship(provider, fit.ship_type_id, fit.mode_type_id)

    # WS-6: materialise each projected hostile module as a bare source entity (its own
    # BASE attributes only — an "unbonused attacker": no skills, ship bonuses, overheat or
    # rigs of the attacker are modelled; documented simplification). A quantity-N input
    # expands to N independent sources so a stacking-penalised chain (e.g. two webs) is
    # evaluated by the normal pass-3 machinery. Overloaded is treated as active for gating.
    for p in fit.projected:
        rank = STATE_OVERLOADED if p.state == ModuleState.OVERHEATED else STATE_ACTIVE
        for _ in range(max(1, p.quantity)):
            ev.projected.append(_new_entity(provider, p.type_id, "projected", rank,
                                            module_state=p.state))
    # WS-7: fleet boosts need no entity of their own (their buff value is derived from the
    # burst charge's attributes, not evaluated as a fitted item); carry the raw inputs so
    # _collect_boosts can read each charge and apply the buff onto our ship/modules.
    ev.boost_inputs = tuple(fit.boosts)
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
    """Entities "located on the ship": hull, modules, charges, drones and fighters."""
    out = [ev.ship]
    for m in ev.modules:
        out.append(m)
        if m.charge is not None:
            out.append(m.charge)
    out.extend(ev.drones)
    out.extend(ev.fighters)
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
    sources: list[Entity] = [ev.ship, *ev.skills, *ev.implants, *ev.drones, *ev.fighters]
    for m in ev.modules:
        sources.append(m)
        if m.charge is not None:
            sources.append(m.charge)
    if ev.mode is not None and ev.mode_valid:
        sources.append(ev.mode)             # a mismatched mode applies nothing

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

    _collect_projected(ev, provider)
    _collect_boosts(ev, provider)


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


def _collect_projected(ev: EvaluatedFit, provider) -> None:
    """WS-6: attach each projected hostile module's target-domain modifiers onto the hull.

    A projected source contributes only its effect-category-2 (target) effects, and within
    those only ``ItemModifier`` rows whose domain is ``targetID`` / ``target`` — the ones
    that write onto the target ship (us). This is where the synthesised web / painter /
    dampener modifiers (import_ship_bonuses._CLIENT_INTERNAL_EFFECTS) and the warp
    scrambler's real graph land. Other modifier funcs on a projected effect
    (LocationRequiredSkillModifier / EffectStopper — e.g. the scrambler's MWD-block) act on
    the attacker's fleet or gate onlining and have no target-side meaning in v1, so they are
    skipped. Range/falloff are ignored: v1 applies full strength (as-if at optimal). Each
    application carries the family's resistance attribute (``_PROJECTED_RESISTANCE_BY_
    EFFECT``) so pass 3 scales the incoming value by our evaluated resistance. A projected
    source is category 7 (Module) → NOT stacking-exempt, so multiple of the same module
    penalise exactly like fitted modules would.
    """
    for src in ev.projected:
        for eid in src.effect_ids:
            edef = provider.effect_def(eid)
            if edef is None or edef.category != _STATE_TARGET:
                continue                       # only target (projected) effects apply to us
            resist_attr = _PROJECTED_RESISTANCE_BY_EFFECT.get(edef.effect_id)
            for mod in edef.modifiers:
                if mod.func != "ItemModifier" or mod.domain not in ("targetID", "target"):
                    continue
                op = mod.operation
                tgt_attr = mod.modified_attribute_id
                src_attr = mod.modifying_attribute_id
                if op not in OPERATOR_ORDER or tgt_attr is None or src_attr is None:
                    continue
                _add_application(ev.ship, tgt_attr, _Application(
                    operation=op, source=src, source_attr=src_attr,
                    min_state=STATE_ACTIVE, penalisable_source=True,
                    resistance_attr=resist_attr))


def _boost_strengths(charge_attrs: dict[int, float],
                     strength_pct: float | None) -> list[tuple[int, float]]:
    """The (buff_id, strength) pairs a burst charge grants at the unbonused default, with an
    optional override.

    For each populated warfare-buff slot (warfareBuffNID > 0) the default strength is the
    charge's warfareBuffNMultiplier (= 1.0 × multiplier on a T1 burst — see
    _UNBONUSED_BURST_BASE). ``strength_pct``, when given, replaces the PRIMARY (slot-1)
    buff's strength; any secondary buffs the same charge grants are scaled by the same ratio
    so the charge's internal buff proportions are preserved (a real command ship scales all
    of a burst's buffs uniformly). A zero primary default cannot define a ratio, so
    secondaries then keep their own defaults.
    """
    slots: list[tuple[int, float]] = []
    for id_attr, mul_attr in zip(_BUFF_ID_ATTRS, _BUFF_MULTIPLIER_ATTRS, strict=True):
        buff_id = int(charge_attrs.get(id_attr, 0) or 0)
        if buff_id <= 0:
            continue
        slots.append((buff_id, _UNBONUSED_BURST_BASE * charge_attrs.get(mul_attr, 0.0)))
    if strength_pct is None or not slots:
        return slots
    primary_default = slots[0][1]
    if primary_default:
        ratio = strength_pct / primary_default
        return [(bid, s * ratio) for bid, s in slots]
    return [(slots[0][0], strength_pct), *slots[1:]]


def _boost_targets(ev: EvaluatedFit, mod: DbuffModifierDef) -> list[Entity]:
    """The entities a warfare-buff modifier applies to (mirrors CCP's dbuff modifier kinds).

    ``item`` → the boosted ship itself; ``location`` → the ship and everything located on it
    (modules, charges, drones); ``locationGroup`` → located items of the modifier's group;
    ``locationRequiredSkill`` → located items that require the modifier's skill.
    """
    if mod.kind == "item":
        return [ev.ship]
    located = _location_entities(ev)
    if mod.kind == "location":
        return located
    if mod.kind == "locationGroup":
        return [e for e in located if e.group_id == mod.group_id]
    if mod.kind == "locationRequiredSkill":
        if mod.skill_type_id is None:
            return []
        return [e for e in located if _required_skill_matches(e, mod.skill_type_id)]
    return []


def _collect_boosts(ev: EvaluatedFit, provider) -> None:
    """WS-7: apply friendly fleet command bursts (warfare buffs) onto this fit.

    Each boost is a burst CHARGE naming up to four warfare buffs (see _boost_strengths). Per
    buff id the strongest single instance across all boosts wins — Maximum → the max value,
    Minimum → the min (CCP's aggregateMode; bursts do NOT sum, so two identical boosts equal
    one). The winning value is applied via the buff's operator onto every attribute its dbuff
    modifiers name, on the resolved targets, as a PENALISABLE source: the stacking penalty
    then falls out of the target attribute's stackable flag exactly like a fitted module bonus
    (a resonance/scan-res/sig buff is penalised and shares the chain with local modules; an
    HP/capacity buff is not). A buff id with no imported SdeDbuff is recorded applied=False
    (→ boost_unknown_buff in pass 4) and changes nothing.
    """
    if not ev.boost_inputs:
        return
    # A synthetic always-on source so each application's state gate is satisfied; fresh per
    # fit (never a shared singleton), read-only, its base/values never touched because a
    # boost application carries its value literally.
    boost_source = Entity(type_id=0, kind="boost", state=STATE_OVERLOADED)

    strengths_by_buff: dict[int, list[float]] = {}
    dbuff_by_buff: dict[int, DbuffDef | None] = {}
    for b in ev.boost_inputs:
        charge_attrs = provider.attrs(b.charge_type_id)
        record: dict = {"charge_type_id": b.charge_type_id, "buffs": []}
        for buff_id, strength in _boost_strengths(charge_attrs, b.strength_pct):
            if buff_id not in dbuff_by_buff:
                dbuff_by_buff[buff_id] = provider.dbuff(buff_id)
            applied = dbuff_by_buff[buff_id] is not None
            if applied:
                strengths_by_buff.setdefault(buff_id, []).append(strength)
            record["buffs"].append({"buff_id": buff_id,
                                    "strength_pct": round(strength, 3), "applied": applied})
        ev.boosts_applied.append(record)

    for buff_id, strengths in strengths_by_buff.items():
        dbuff = dbuff_by_buff[buff_id]
        op = _DBUFF_OPERATION.get(dbuff.operation)
        if op is None:
            ev.diagnostics.append(f"unknown_dbuff_operation:{dbuff.operation}:buff{buff_id}")
            continue
        value = max(strengths) if dbuff.aggregate_mode == "Maximum" else min(strengths)
        for mod in dbuff.modifiers:
            for target in _boost_targets(ev, mod):
                _add_application(target, mod.modified_attribute_id, _Application(
                    operation=op, source=boost_source, source_attr=0,
                    min_state=STATE_OFFLINE, penalisable_source=True,
                    literal_value=value))


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
                # WS-7 boosts carry their (already aggregated) buff value literally; every
                # other application reads it off the source entity's attribute.
                raw = (app.literal_value if app.literal_value is not None
                       else ev.value(app.source, app.source_attr))
                if app.resistance_attr is not None:
                    # WS-6 projected effect: the incoming value is reduced by the target
                    # hull's resistance for this family (default 1.0 → no-op). Scaling the
                    # source value scales the operator's delta correctly for the postPercent
                    # families this path carries (web/painter/damp).
                    raw *= ev.value(ev.ship, app.resistance_attr)
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


def inject_penalised_percent(ev: EvaluatedFit, target: Entity, attribute_id: int,
                             source: Entity, percent: float,
                             min_state: int = STATE_ACTIVE) -> None:
    """Add a synthetic postPercent modifier of ``percent`` (500.0 == +500%) onto
    ``attribute_id`` of ``target``, stacking-penalised like any fitted/projected percentage
    modifier — then let :func:`_calculate` combine it.

    This exists for CCP's *client-internal* module bonuses: the ones shipped with an EMPTY
    ``modifierInfo`` so the ordinary pass-2 collection never sees them. The MWD signature
    bloom (``moduleBonusMicrowarpdrive`` / effect 6730 — no modifiers) is the motivating case:
    the caller (evaluator._mobility) computes the effective bloom percent, but it MUST join
    the same stacking-penalised chain the graph already applies to every other percentage
    modifier of the (non-stackable) ``signatureRadius`` — rig sig penalties, a projected
    painter, … — instead of being multiplied on separately (which escapes the penalty and
    understates a MWD-plus-rig fit's signature). Feeding it in as a real application means
    ``_calculate`` does the joint sort-and-penalise in pass 3 with NO second penalty
    implementation. The value is carried literally (it is already the effective, evaluated
    percent), so ``source`` is used only for the state gate. Any cached value of the target
    attribute is invalidated so the next read recomputes with the injected source.
    """
    _add_application(target, attribute_id, _Application(
        operation=OP_POST_PERCENT, source=source, source_attr=0,
        min_state=min_state, penalisable_source=True, literal_value=float(percent)))
    target.values.pop(attribute_id, None)


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
