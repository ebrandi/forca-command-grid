"""Tocha's Lab domain services.

The orchestration layer between the workspace views and (a) the calculation engine and
(b) the FORCA subsystems Tocha's Lab integrates with — skills/readiness, market pricing,
Shipyard/stock availability, doctrines and supply. Everything here reuses the existing
authorities (see handbooks/contributor-handbook/decision-log.md TL1/TL2); nothing is
re-implemented.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal

from django.db import transaction

from apps.sde.models import SdeDogmaAttribute, SdeType, SdeTypeAttribute, SdeTypeEffect
from core.audit import audit_log

from .engine import attributes as A
from .engine.adapter import FittingEngine
from .engine.types import (
    BoostInput,
    DamageProfileInput,
    FighterInput,
    FitInput,
    ModuleInput,
    ModuleState,
    OperatingProfile,
    ProjectedInput,
    SkillProfile,
    SlotKind,
    TargetProfile,
)
from .models import Fit, FitRevision, Visibility

_MAX_LINES = 500  # mirrors apps/doctrines/fitparser — bound a pathological paste
_QTY_RE = re.compile(r"\sx(\d+)\s*$", re.IGNORECASE)
_CATEGORY_CHARGE = 8
_CATEGORY_DRONE = 18
_CATEGORY_FIGHTER = 87

# WS-11 mutated (abyssal) modules — EFT interchange (pyfa's mutation-block syntax).
# --------------------------------------------------------------------------------
# pyfa (service/port/eft.py + service/port/muta.py, studied under GPL, implemented
# independently) renders a mutated module's rack line with a trailing ``[N]`` reference and
# emits, after all racks, one block per mutant:
#     [N] <base item name>
#       <mutaplasmid name>
#       <attrName value>, <attrName2 value2>
# (firstPrefix ``[N] ``, prefix two spaces, attributes sorted by name, values prettified).
# FORCA models a mutation purely as attribute overrides on the fitted type — it does NOT
# track the mutaplasmid identity — so on export the mutaplasmid line is a fixed placeholder,
# and on import it is parsed but ignored. Round-trip fidelity: FORCA→FORCA preserves every
# override exactly (identical FitInput.hash); pyfa→FORCA preserves the base item + overrides
# (mutaplasmid identity dropped); FORCA→pyfa LOSES the overrides, because pyfa needs a real
# mutaplasmid name on the middle line to keep them (documented in the mechanics handbook).
_MUTAPLASMID_PLACEHOLDER = "Unknown Mutaplasmid"
_MUT_REF_RE = re.compile(r"\s*\[(\d+)\]\s*$")          # trailing " [N]" on a rack/drone line
_MUT_HEADER_RE = re.compile(r"^\[(\d+)\](?P<tail>.*)")  # a mutation-block header "[N] BaseName"
_MAX_OVERRIDES = 32  # mirrors apps.fitting.views._MAX_OVERRIDES — bound overrides per module


def _resolve_attr_names(names: set[str]) -> dict[str, int]:
    """Case-insensitive dogma-attribute name → attribute_id (one query). Attribute names are
    exact camelCase tokens (e.g. ``damageMultiplier``); an unknown name maps to nothing so the
    caller can surface it as an unresolved import warning."""
    out: dict[str, int] = {}
    wanted = {n.strip() for n in names if n and n.strip()}
    if not wanted:
        return out
    for aid, name in SdeDogmaAttribute.objects.filter(
        name__in=list(wanted)
    ).values_list("attribute_id", "name"):
        out[name.lower()] = aid
    missing = [n for n in wanted if n.lower() not in out]
    for n in missing:
        row = SdeDogmaAttribute.objects.filter(name__iexact=n).values_list(
            "attribute_id", "name").first()
        if row:
            out[n.lower()] = row[0]
    return out


def _overrides_of(item: dict) -> dict[int, float]:
    """The mutated attribute overrides on an items entry as ``{attr_id: value}`` (keys may be
    str from persisted JSON or int)."""
    ov = item.get("attr_overrides") or {}
    out: dict[int, float] = {}
    for k, v in ov.items():
        try:
            out[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def _fmt_mut_value(v: float) -> str:
    """Compact, round-trip-safe rendering of a mutated attribute value: an integral value
    prints without a decimal, everything else via ``repr`` so ``float(_fmt_mut_value(v)) == v``
    (FORCA→FORCA export/import preserves the exact float)."""
    f = float(v)
    return str(int(f)) if f == int(f) else repr(f)


def _parse_mutation_attrs(line: str, resolver: dict[str, int],
                          unresolved: list[str]) -> dict[int, float]:
    """Parse a mutation block's attribute line (``attrName value, attrName2 value2``) into
    ``{attr_id: value}`` (bounded at _MAX_OVERRIDES). Mirrors pyfa's parseMutantAttrs: split
    on commas, each pair split on whitespace into exactly a name + a float; an unresolvable
    attribute name is recorded in ``unresolved`` (consistent with unresolved module names)."""
    out: dict[int, float] = {}
    for pair in line.split(","):
        parts = pair.split()
        if len(parts) != 2:
            continue
        name, raw = parts
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        aid = resolver.get(name.strip().lower())
        if aid is None:
            unresolved.append(name.strip())
            continue
        if len(out) < _MAX_OVERRIDES:
            out[aid] = value
    return out


# --------------------------------------------------------------------------- #
# Skills
# --------------------------------------------------------------------------- #
def pilot_skill_profile(character, label: str = "current") -> SkillProfile:
    """A :class:`SkillProfile` from a pilot's latest imported skill snapshot.

    Reuses the canonical skill store (``CharacterSkillSnapshot``); never re-reads ESI or
    invents a skills table. An un-imported pilot yields an all-zero profile (honest: the
    engine then reports every skill as missing)."""
    snap = character.skill_snapshots.filter(is_latest=True).first() if character else None
    if not snap or not snap.skills:
        return SkillProfile.from_dict({}, label=label)
    levels = {}
    for sid, entry in snap.skills.items():
        try:
            levels[int(sid)] = int((entry or {}).get("trained_level", 0))
        except (TypeError, ValueError):
            continue
    return SkillProfile.from_dict(levels, label=label)


def skill_readout(ship_type_id: int, items: list[dict], skills: SkillProfile,
                  limit: int = 14) -> list[dict]:
    """The skills that actually drive this fit's numbers, each with the active profile's
    trained level — so a pilot can see WHICH skills are being applied (and at what level),
    not just which are missing. Relevant skills = the hull's + every fitted module's
    prerequisites, plus the engine's standard damage/tank/navigation bonus skills."""
    from .engine.adapter import ORMDataProvider
    from .engine.bonuses import STANDARD_SKILL_BONUSES

    prov = ORMDataProvider()
    ids = {sid for sid, _ in prov.required_skills(int(ship_type_id))}
    for it in items or []:
        ids |= {sid for sid, _ in prov.required_skills(int(it["type_id"]))}
    ids |= {b.skill_id for b in STANDARD_SKILL_BONUSES if b.skill_id}
    if not ids:
        return []
    names = _type_names(ids)
    out = [{"skill_type_id": sid, "name": names.get(sid, f"Skill {sid}"),
            "level": skills.level(sid)} for sid in ids]
    # Trained-highest first, then by name — the pilot sees their strongest relevant skills up top.
    out.sort(key=lambda s: (-s["level"], s["name"]))
    return out[:limit]


# --------------------------------------------------------------------------- #
# Fit input construction
# --------------------------------------------------------------------------- #
_SLOT_BY_VALUE = {s.value: s for s in SlotKind}
_STATE_BY_VALUE = {s.value: s for s in ModuleState}

# Doctrine fits persist their slot as a human display label ("High 0", "Mid 1", "Drone
# Bay", "Cargo"; see apps.doctrines.xml_import._slot_display), not the engine's rack token.
# Map any label, raw slot category, or EFT-style "hi slot 0" onto a canonical SlotKind
# value. Longest/most-specific prefixes first so "subsystem"/"service" never collide.
_SLOT_ALIAS_PREFIXES = (
    ("subsystem", "subsystem"),
    ("service", "service"),
    ("fighter", "fighter"),
    ("drone", "drone"),
    ("cargo", "cargo"),
    ("high", "high"), ("hi", "high"),
    ("med", "med"), ("mid", "med"),
    ("low", "low"),
    ("rig", "rig"),
)
_RACKED_SLOTS = ("high", "med", "low", "rig")
# A tactical destroyer's mode rides in the items blob as a single slot="mode" entry (not a
# rack module). This keeps persistence symmetric — the same list the client posts and the
# revision stores — while the engine reads it as FitInput.mode_type_id.
_MODE_SLOT = "mode"
# A projected hostile module (WS-6) rides the items blob as a slot="projected" entry — like
# the mode marker, it is not a fitted rack module. It is excluded from EFT export, pricing,
# stock coverage and doctrine promotion (it is not a purchasable/stockable part of the fit).
_PROJECTED_SLOT = "projected"
# A friendly fleet command burst (WS-7) rides the items blob as a slot="boost" entry (the
# burst CHARGE type id). Like the mode / projected markers it is never a fitted rack module,
# never priced/stocked/exported. An optional top-level "strength_pct" overrides the buff's
# unbonused-default strength.
_BOOST_SLOT = "boost"


def is_mode_item(it: dict) -> bool:
    """Whether an items entry is the tactical-mode marker (not a fitted rack module)."""
    return str((it or {}).get("slot", "")).strip().lower() == _MODE_SLOT


def is_projected_item(it: dict) -> bool:
    """Whether an items entry is a projected hostile module (not fitted to our ship)."""
    return str((it or {}).get("slot", "")).strip().lower() == _PROJECTED_SLOT


def is_boost_item(it: dict) -> bool:
    """Whether an items entry is a friendly fleet command burst (not fitted to our ship)."""
    return str((it or {}).get("slot", "")).strip().lower() == _BOOST_SLOT


def is_extra_item(it: dict) -> bool:
    """Whether an items entry is a non-fitted marker (tactical mode, projected module, or
    fleet boost) — excluded from EFT export, pricing, stock and doctrine promotion."""
    return is_mode_item(it) or is_projected_item(it) or is_boost_item(it)


def canonical_slot(value) -> str | None:
    """Normalise a slot label/category to a canonical engine token, or ``None`` if unknown.

    Doctrine fits speak labels ("High 0", "Drone Bay"); the editor racks and engine speak
    tokens ("high", "drone"). An unrecognised value returns ``None`` so callers can fall
    back to SDE inference rather than silently mis-slotting a module. Deliberately rack/
    hold-oriented: implant/booster never arrive as labels here, so they map to ``None``."""
    s = str(value or "").strip().lower()
    if not s:
        return None
    for prefix, token in _SLOT_ALIAS_PREFIXES:
        if s.startswith(prefix):
            return token
    return None


def fit_input_from_items(ship_type_id: int, items: list[dict],
                         mode_type_id: int | None = None) -> FitInput:
    """Build the engine's :class:`FitInput` from a revision's canonical ``items`` list.

    A slot="mode" entry is lifted out as the fit's tactical mode rather than fitted as a
    rack module (so the engine sees it as :attr:`FitInput.mode_type_id`). An explicit
    ``mode_type_id`` argument (the live-editor API key) takes precedence over the blob."""
    modules = []
    projected = []
    boosts = []
    fighters = []
    for it in items or []:
        if is_mode_item(it):
            if mode_type_id is None:
                mode_type_id = int(it["type_id"])
            continue
        _raw = it.get("slot")
        _slot = _SLOT_BY_VALUE[_raw] if _raw in _SLOT_BY_VALUE \
            else _SLOT_BY_VALUE.get(canonical_slot(_raw))
        if _slot == SlotKind.FIGHTER:
            # WS-12: a fighter squadron rides the blob as a slot="fighter" entry with
            # quantity = squadron count. Lifted into FitInput.fighters (not a rack module).
            fighters.append(FighterInput(type_id=int(it["type_id"]),
                                         count=max(1, int(it.get("quantity", 1)))))
            continue
        if is_projected_item(it):
            projected.append(ProjectedInput(
                type_id=int(it["type_id"]),
                state=_STATE_BY_VALUE.get(it.get("state"), ModuleState.ACTIVE),
                quantity=int(it.get("quantity", 1)),
            ))
            continue
        if is_boost_item(it):
            sp = it.get("strength_pct")
            boosts.append(BoostInput(
                charge_type_id=int(it["type_id"]),
                strength_pct=float(sp) if sp is not None and sp != "" else None,
            ))
            continue
        raw = it.get("slot")
        # Trust an already-canonical token; otherwise canonicalise a display label, falling
        # back to LOW. (Explicit membership test, so no reliance on enum truthiness.)
        slot = _SLOT_BY_VALUE[raw] if raw in _SLOT_BY_VALUE \
            else _SLOT_BY_VALUE.get(canonical_slot(raw), SlotKind.LOW)
        state = _STATE_BY_VALUE.get(it.get("state"), ModuleState.ACTIVE)
        modules.append(ModuleInput(
            type_id=int(it["type_id"]), slot=slot, state=state,
            charge_type_id=(int(it["charge_type_id"]) if it.get("charge_type_id") else None),
            quantity=int(it.get("quantity", 1)),
            # WS-11: mutated (abyssal) attribute overrides ride the items entry; ModuleInput
            # freezes the dict (str/int keys tolerated) into a canonical hashable tuple.
            attr_overrides=it.get("attr_overrides") or (),
        ))
    return FitInput(ship_type_id=int(ship_type_id), modules=tuple(modules),
                    mode_type_id=(int(mode_type_id) if mode_type_id else None),
                    projected=tuple(projected), boosts=tuple(boosts),
                    fighters=tuple(fighters))


def operating_profile(propulsion: bool = True,
                      damage: dict | None = None,
                      target: dict | None = None,
                      warp_distance_au: float | None = None) -> OperatingProfile:
    dp = DamageProfileInput(**damage) if damage else DamageProfileInput()
    tgt = None
    if target and (target.get("signature_radius") or target.get("velocity")
                   or target.get("distance_m") or target.get("hp")
                   or target.get("sensor_strength")):
        dist = target.get("distance_m")
        ang = target.get("angular")
        hp = target.get("hp")
        ss = target.get("sensor_strength")
        st = target.get("sensor_type")
        st = st if st in ("radar", "ladar", "magnetometric", "gravimetric") else None
        tgt = TargetProfile(
            signature_radius=max(0.0, float(target.get("signature_radius") or 0)),
            velocity=max(0.0, float(target.get("velocity") or 0)),
            label=str(target.get("label", ""))[:60],
            target_distance_m=max(0.0, float(dist)) if dist is not None else None,
            target_angular=max(0.0, float(ang)) if ang is not None else None,
            target_hp=max(0.0, float(hp)) if hp is not None else None,
            target_sensor_strength=max(0.0, float(ss)) if ss is not None else None,
            target_sensor_type=st,
        )
    warp = 10.0 if warp_distance_au is None else max(0.0, float(warp_distance_au))
    return OperatingProfile(propulsion_active=propulsion, damage_profile=dp,
                            target=tgt, warp_distance_au=warp)


def evaluate(ship_type_id: int, items: list[dict], skills: SkillProfile,
             op: OperatingProfile | None = None, *, cached: bool = True,
             mode_type_id: int | None = None) -> dict:
    """A flat telemetry dict for a loadout (via the adapter), ready for the template.

    The engine's ``to_dict`` nests the telemetry groups (resources/offence/…) under a
    ``telemetry`` key alongside diagnostics/versions; the workspace template and the
    comparison want them flattened into one namespace, so lift the groups up a level and
    merge the metadata (diagnostics, missing_skills, engine/data version, status).

    ``mode_type_id`` is the optional live-editor override for the tactical mode; when
    omitted the mode is read from a slot="mode" entry in ``items`` (persistence path)."""
    from .diagnostics import localise_diagnostics, localise_unsupported

    engine = FittingEngine()
    fit = fit_input_from_items(ship_type_id, items, mode_type_id=mode_type_id)
    result = engine.evaluate_cached(fit, skills, op) if cached else engine.evaluate(fit, skills, op).to_dict()
    flat = dict(result.get("telemetry", {}))
    flat.update({k: v for k, v in result.items() if k != "telemetry"})
    # The engine is Django-free and emits English + codes; render them in the reader's
    # language here (never mutates the cached dict — localise_* returns fresh lists).
    flat["diagnostics"] = localise_diagnostics(flat.get("diagnostics"))
    flat["unsupported"] = localise_unsupported(flat.get("unsupported"))
    return flat


# --------------------------------------------------------------------------- #
# Persistence: save / fork / share
# --------------------------------------------------------------------------- #
def _next_revision_number(fit: Fit) -> int:
    last = fit.revisions.order_by("-revision_number").values_list("revision_number", flat=True).first()
    return (last or 0) + 1


def _revision_matches(rev: FitRevision | None, ship_type_id: int, items: list[dict]) -> bool:
    """Whether a stored revision is the SAME loadout as ``(ship_type_id, items)`` — a canonical,
    order-independent comparison via :meth:`FitInput.hash` (the same sha256 the engine cache
    keys on). Used for save-dedup (API-10): the hash folds every engine-relevant field (slot,
    state, charge, quantity, mutated overrides, mode/projected/boost/fighter markers), so a
    re-save with no real change short-circuits instead of appending an identical revision."""
    if rev is None:
        return False
    if int(rev.ship_type_id) != int(ship_type_id):
        return False
    try:
        return (fit_input_from_items(rev.ship_type_id, rev.items or []).hash()
                == fit_input_from_items(ship_type_id, items or []).hash())
    except (KeyError, TypeError, ValueError):
        return False


@transaction.atomic
def save_revision(fit: Fit, *, ship_type_id: int, items: list[dict], user,
                  change_summary: str = "", notes: str = "",
                  dedup: bool = False) -> FitRevision:
    """Append a new immutable revision and point the fit at it. Never mutates history.

    With ``dedup=True`` (the interactive Save button), a payload identical to the fit's current
    revision returns that revision unchanged instead of appending a duplicate — so repeatedly
    clicking Save, or a no-op edit, never inflates the revision history. Import/fork/restore
    callers keep ``dedup=False`` (they always intend a new revision)."""
    if dedup and _revision_matches(fit.current_revision, ship_type_id, items):
        return fit.current_revision
    engine = FittingEngine()
    rev = FitRevision.objects.create(
        fit=fit, revision_number=_next_revision_number(fit), ship_type_id=int(ship_type_id),
        items=items, notes=notes, change_summary=change_summary, created_by=user,
        engine_version=engine.engine_version, data_version=engine.data_version,
    )
    fit.ship_type_id = int(ship_type_id)
    fit.current_revision = rev
    fit.save(update_fields=["ship_type_id", "current_revision", "updated_at"])
    return rev


@transaction.atomic
def create_fit(user, *, name: str, ship_type_id: int, items: list[dict],
               visibility: str = Visibility.PRIVATE, origin: str = "scratch",
               description: str = "", forked_from: Fit | None = None,
               forked_from_revision: int | None = None) -> Fit:
    fit = Fit.objects.create(
        owner=user, name=name[:200] or "Untitled fit", ship_type_id=int(ship_type_id),
        visibility=visibility, origin=origin, description=description,
        forked_from=forked_from, forked_from_revision=forked_from_revision,
    )
    save_revision(fit, ship_type_id=ship_type_id, items=items, user=user,
                  change_summary="Created")
    audit_log(user, "tochaslab.fit.created", target_type="fitting.Fit", target_id=fit.pk,
              metadata={"ship_type_id": int(ship_type_id), "origin": origin})
    return fit


@transaction.atomic
def fork_fit(source: Fit, revision: FitRevision, user, name: str | None = None) -> Fit:
    """Fork any fit the user can view into a new private fit they own (lineage recorded)."""
    fork = create_fit(
        user, name=name or f"{source.name} (fork)", ship_type_id=revision.ship_type_id,
        items=revision.items, visibility=Visibility.PRIVATE, origin="fork",
        forked_from=source, forked_from_revision=revision.revision_number,
    )
    audit_log(user, "tochaslab.fit.forked", target_type="fitting.Fit", target_id=fork.pk,
              metadata={"from_fit": source.pk, "from_revision": revision.revision_number})
    return fork


def rename_fit(fit: Fit, name: str, actor=None) -> Fit:
    fit.name = (name or fit.name).strip()[:200] or fit.name
    fit.save(update_fields=["name", "updated_at"])
    audit_log(actor, "tochaslab.fit.renamed", target_type="fitting.Fit", target_id=fit.pk)
    return fit


@transaction.atomic
def duplicate_fit(source: Fit, revision: FitRevision, user, name: str | None = None) -> Fit:
    """An independent copy the user owns (no fork lineage — unlike :func:`fork_fit`)."""
    dup = create_fit(
        user, name=name or f"{source.name} (copy)", ship_type_id=revision.ship_type_id,
        items=revision.items, visibility=Visibility.PRIVATE, origin="duplicate",
        description=source.description,
    )
    audit_log(user, "tochaslab.fit.duplicated", target_type="fitting.Fit", target_id=dup.pk,
              metadata={"from_fit": source.pk})
    return dup


def set_archived(fit: Fit, archived: bool, actor=None) -> Fit:
    fit.is_archived = archived
    fit.save(update_fields=["is_archived", "updated_at"])
    audit_log(actor, "tochaslab.fit.archived" if archived else "tochaslab.fit.restored",
              target_type="fitting.Fit", target_id=fit.pk)
    return fit


@transaction.atomic
def restore_revision(fit: Fit, revision: FitRevision, user) -> FitRevision:
    """Bring back an earlier revision by appending its content as a NEW revision (history
    stays append-only — nothing is rewritten)."""
    rev = save_revision(
        fit, ship_type_id=revision.ship_type_id, items=revision.items, user=user,
        change_summary=f"Restored from revision {revision.revision_number}",
    )
    audit_log(user, "tochaslab.fit.revision_restored", target_type="fitting.Fit",
              target_id=fit.pk, metadata={"from_revision": revision.revision_number})
    return rev


def create_share_link(fit: Fit, actor=None) -> str:
    from .models import new_share_token
    if not fit.share_token:
        fit.share_token = new_share_token()
    fit.share_revoked = False
    if fit.visibility == Visibility.PRIVATE:
        fit.visibility = Visibility.PUBLIC
    fit.save(update_fields=["share_token", "share_revoked", "visibility", "updated_at"])
    audit_log(actor, "tochaslab.fit.shared", target_type="fitting.Fit", target_id=fit.pk)
    return fit.share_token


def revoke_share_link(fit: Fit, actor=None) -> None:
    fit.share_revoked = True
    fit.save(update_fields=["share_revoked", "updated_at"])
    audit_log(actor, "tochaslab.fit.share_revoked", target_type="fitting.Fit", target_id=fit.pk)


# --------------------------------------------------------------------------- #
# Import / export
# --------------------------------------------------------------------------- #
def _resolve_names(names: set[str]) -> dict[str, int]:
    """Case-insensitive name → type_id for a batch of names (one query)."""
    out: dict[str, int] = {}
    lowered = {n.lower(): n for n in names if n}
    if not lowered:
        return out
    for tid, name in SdeType.objects.filter(name__in=list(names)).values_list("type_id", "name"):
        out[name.lower()] = tid
    # Fall back to a case-insensitive pass for names whose casing differs.
    missing = [orig for low, orig in lowered.items() if low not in out]
    for orig in missing:
        row = SdeType.objects.filter(name__iexact=orig).values_list("type_id", "name").first()
        if row:
            out[orig.lower()] = row[0]
    return out


def infer_slots(type_ids) -> dict[int, str]:
    """Best-effort fitting rack per module, from its slot-defining dogma effect, with a
    charge/drone fallback by category. Unknown => 'low' (the editor can reassign)."""
    type_ids = {int(t) for t in type_ids}
    if not type_ids:
        return {}
    by_effect: dict[int, str] = {}
    for tid, eff in SdeTypeEffect.objects.filter(
        type_id__in=type_ids, effect_id__in=list(A.SLOT_EFFECTS)
    ).values_list("type_id", "effect_id"):
        by_effect[tid] = A.SLOT_EFFECTS[eff]
    cats = dict(SdeType.objects.filter(type_id__in=type_ids)
                .values_list("type_id", "group__category_id"))
    out = {}
    for tid in type_ids:
        if tid in by_effect:
            out[tid] = by_effect[tid]
        elif cats.get(tid) == _CATEGORY_DRONE:
            out[tid] = "drone"
        elif cats.get(tid) == _CATEGORY_FIGHTER:
            out[tid] = "fighter"
        elif cats.get(tid) == _CATEGORY_CHARGE:
            out[tid] = "charge"
        else:
            out[tid] = "low"
    return out


def charge_takers(type_ids) -> set[int]:
    """Of ``type_ids``, those that accept a charge (they declare at least one accepted
    charge group) — i.e. the modules the editor should offer an ammo loader for."""
    type_ids = {int(t) for t in type_ids}
    if not type_ids:
        return set()
    return set(
        SdeTypeAttribute.objects.filter(
            type_id__in=type_ids, attribute_id__in=A.CHARGE_GROUP_ATTRS,
        ).values_list("type_id", flat=True)
    )


def compatible_charges(weapon_type_id: int, query: str, limit: int = 20) -> list[dict]:
    """Charges that fit ``weapon_type_id`` and whose name matches ``query`` — filtered to
    the weapon's accepted charge groups and matching charge size (the same rules EVE uses),
    so a pilot only loads ammo the weapon can actually fire. If the weapon declares no
    accepted groups (unexpected for a real weapon), fall back to any charge by name."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    attrs = dict(
        SdeTypeAttribute.objects.filter(
            type_id=int(weapon_type_id),
            attribute_id__in=(*A.CHARGE_GROUP_ATTRS, A.CHARGE_SIZE),
        ).values_list("attribute_id", "value")
    )
    group_ids = {int(attrs[a]) for a in A.CHARGE_GROUP_ATTRS if a in attrs}
    weapon_size = attrs.get(A.CHARGE_SIZE)

    qs = SdeType.objects.filter(name__icontains=query, published=True)
    qs = qs.filter(group_id__in=group_ids) if group_ids else \
        qs.filter(group__category_id=_CATEGORY_CHARGE)
    rows = list(qs.values("type_id", "name")[: limit * 4])

    if weapon_size is not None and rows:
        # Keep charges whose size matches the weapon (or that declare no size).
        sizes = dict(
            SdeTypeAttribute.objects.filter(
                type_id__in=[r["type_id"] for r in rows], attribute_id=A.CHARGE_SIZE,
            ).values_list("type_id", "value")
        )
        rows = [r for r in rows if sizes.get(r["type_id"], weapon_size) == weapon_size]

    low = query.lower()
    rows.sort(key=lambda r: (not r["name"].lower().startswith(low), r["name"]))
    return rows[:limit]


# --------------------------------------------------------------------------- #
# Editor catalogues — modes, fleet-boost charges, mutated-attr defs, projected search
# --------------------------------------------------------------------------- #
# A tactical destroyer's three modes are SdeTypes in the "Ship Modifiers" group, named for
# the hull ("Svipul Defense Mode" …). Data-driven — no per-hull table.
_SHIP_MODIFIERS_GROUP = "Ship Modifiers"


def ship_tactical_modes(ship_type_id: int) -> list[dict]:
    """The tactical-destroyer modes selectable for a hull, or ``[]`` for any other ship.

    Modes live in the "Ship Modifiers" group and are named after the hull, so match by the
    hull's own name — nothing hard-coded per hull. Each entry is ``{type_id, name}``; the UI
    renders them as a dropdown and writes the choice as a ``slot="mode"`` items entry."""
    hull = SdeType.objects.filter(type_id=int(ship_type_id)).values_list("name", flat=True).first()
    if not hull:
        return []
    rows = (SdeType.objects
            .filter(group__name=_SHIP_MODIFIERS_GROUP, name__istartswith=hull)
            .values("type_id", "name").order_by("name"))
    return [{"type_id": r["type_id"], "name": r["name"]} for r in rows]


def command_burst_charges() -> list[dict]:
    """The fleet command-burst charges a pilot can simulate being boosted by, grouped by burst
    family (Shield/Armor/Skirmish/Information/Mining). Each charge names the warfare buff(s) it
    grants; the UI toggles one per family and writes a ``slot="boost"`` items entry. Data-driven
    — a burst charge is any published type that carries warfareBuff1ID (2468); the group-name
    filter alone also matches the charges' blueprint groups (seen live on the test server)."""
    buff_carriers = SdeTypeAttribute.objects.filter(attribute_id=2468).values("type_id")
    rows = (SdeType.objects
            .filter(published=True, group__name__icontains="Burst Charge",
                    type_id__in=buff_carriers)
            .values("type_id", "name", "group__name").order_by("group__name", "name"))
    return [{"type_id": r["type_id"], "name": r["name"], "group": r["group__name"]} for r in rows]


def module_attribute_defs(type_id: int) -> list[dict]:
    """A module's own dogma attributes (id, name, base value) for the mutated-attribute editor.

    A mutation only ever re-rolls an attribute the base type already carries, so the editor
    offers exactly those. Returns ``[]`` for an unknown type."""
    attrs = dict(SdeTypeAttribute.objects.filter(type_id=int(type_id))
                 .values_list("attribute_id", "value"))
    if not attrs:
        return []
    names = dict(SdeDogmaAttribute.objects.filter(attribute_id__in=list(attrs))
                 .values_list("attribute_id", "name"))
    out = [{"attribute_id": aid, "name": names.get(aid, str(aid)), "value": val}
           for aid, val in attrs.items() if aid in names]
    out.sort(key=lambda a: a["name"])
    return out


# Modules that can be meaningfully PROJECTED onto a fit carry an offensive or assistance
# effect (ewar, energy warfare, remote reps) — the same category-2 "target" effects the
# engine's projected evaluator honours. Search is scoped to those so the projected picker
# never offers a Gyrostabilizer.
def search_projected_modules(query: str, limit: int = 20) -> list[dict]:
    """Modules valid to project onto a fit — those carrying an offensive/assistance (target)
    effect: ewar, neut/nos, remote reps. Name-matched, category-7 modules only."""
    from apps.sde.models import SdeDogmaEffect, SdeTypeEffect

    query = (query or "").strip()
    if len(query) < 2:
        return []
    cand = list(SdeType.objects.filter(
        name__icontains=query, published=True, group__category_id=7,
    ).values("type_id", "name")[: limit * 4])
    if not cand:
        return []
    ids = [c["type_id"] for c in cand]
    target_effects = set(SdeDogmaEffect.objects.filter(
        is_offensive=True).values_list("effect_id", flat=True)) | set(
        SdeDogmaEffect.objects.filter(is_assistance=True).values_list("effect_id", flat=True))
    has_target = set(SdeTypeEffect.objects.filter(
        type_id__in=ids, effect_id__in=list(target_effects),
    ).values_list("type_id", flat=True))
    low = query.lower()
    out = [c for c in cand if c["type_id"] in has_target]
    out.sort(key=lambda r: (not r["name"].lower().startswith(low), r["name"]))
    return out[:limit]


def _extract_mutations(lines: list[str]) -> tuple[list[str], dict[int, dict[int, float]], list[str]]:
    """WS-11: pull pyfa-style mutation blocks out of an EFT body, returning the remaining
    (rack) lines, a ``{ref: {attr_id: value}}`` map, and any unresolved attribute names.

    A block starts at a ``[N]`` header (digits only — the ship header and ``[Empty ...]`` slot
    stubs never match) and runs to the next header or blank line; its LAST line is the
    attribute-override line (pyfa's third line), the intervening line the mutaplasmid name we
    do not model. Consumed lines are removed so the caller's rack loop never sees them."""
    blocks: dict[int, list[str]] = {}
    consumed: set[int] = set()
    current_ref: int | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        if current_ref is not None and current_lines:
            blocks[current_ref] = list(current_lines)

    for i, raw in enumerate(lines):
        line = raw.strip()
        m = _MUT_HEADER_RE.match(line)
        if m:
            _flush()
            current_ref = int(m.group(1))
            current_lines = [m.group("tail").strip()]
            consumed.add(i)
        elif not line:
            _flush()
            current_ref, current_lines = None, []
        elif current_ref is not None:
            current_lines.append(line)
            consumed.add(i)
    _flush()

    remaining = [ln for i, ln in enumerate(lines) if i not in consumed]
    # The attribute line is the block's last line (index 2 in a full pyfa block); a lone
    # header line carries no overrides.
    attr_line_by_ref = {ref: (bl[-1] if len(bl) >= 2 else "") for ref, bl in blocks.items()}
    names: set[str] = set()
    for line in attr_line_by_ref.values():
        for pair in line.split(","):
            parts = pair.split()
            if len(parts) == 2:
                names.add(parts[0].strip())
    resolver = _resolve_attr_names(names)
    unresolved: list[str] = []
    mutations: dict[int, dict[int, float]] = {}
    for ref, line in attr_line_by_ref.items():
        if not line:
            continue
        parsed = _parse_mutation_attrs(line, resolver, unresolved)
        if parsed:
            mutations[ref] = parsed
    return remaining, mutations, unresolved


def import_eft(text: str) -> dict:
    """Parse EFT text into a Tocha's Lab loadout, preserving module+charge pairs and
    inferring each module's slot. Defensive: line-bounded, name-normalised through local
    SDE data only, never evaluated. Returns items + a list of unresolved names.

    Mutated (abyssal) modules: a ``[N]`` mutation block (pyfa syntax) is lifted out first and
    its attribute overrides attached to the module carrying the matching ``[N]`` reference;
    unresolvable attribute names join the ``unresolved`` list, exactly like unresolvable module
    names. The mutaplasmid identity in the block is parsed but not stored (FORCA models a
    mutation as attribute overrides only — see the mutation constants above)."""
    lines = [ln.rstrip() for ln in (text or "").strip().splitlines()][:_MAX_LINES]
    if not lines or not lines[0].startswith("["):
        raise ValueError("EFT must start with '[ShipName, Fit name]'")
    header = lines[0].strip().lstrip("[").rstrip("]")
    ship_name, _sep, fit_name = header.partition(",")
    ship_name, fit_name = ship_name.strip(), fit_name.strip()

    body, mutations, unresolved_attrs = _extract_mutations(lines[1:])

    entries: list[tuple[str, str | None, int, int | None]] = []
    names: set[str] = {ship_name}
    for raw in body:
        line = raw.strip()
        if not line or line.startswith("["):
            continue
        mut_ref = None
        mm = _MUT_REF_RE.search(line)
        if mm:
            mut_ref = int(mm.group(1))
            line = _MUT_REF_RE.sub("", line).strip()
        qty = 1
        m = _QTY_RE.search(line)
        if m:
            qty = int(m.group(1))
            line = _QTY_RE.sub("", line).strip()
        module_name, _, charge_name = line.partition(",")
        module_name = module_name.strip()
        charge_name = charge_name.strip() or None
        if not module_name or module_name.lower() in {"empty"}:
            continue
        entries.append((module_name, charge_name, qty, mut_ref))
        names.add(module_name)
        if charge_name:
            names.add(charge_name)

    resolved = _resolve_names(names)
    ship_type_id = resolved.get(ship_name.lower())
    module_ids = {resolved[e[0].lower()] for e in entries if e[0].lower() in resolved}
    slots = infer_slots(module_ids)

    items: list[dict] = []
    unresolved: list[str] = list(unresolved_attrs)
    for module_name, charge_name, qty, mut_ref in entries:
        tid = resolved.get(module_name.lower())
        if tid is None:
            unresolved.append(module_name)
            continue
        slot = slots.get(tid, "low")
        charge_id = resolved.get(charge_name.lower()) if charge_name else None
        if charge_name and charge_id is None:
            unresolved.append(charge_name)
        overrides = mutations.get(mut_ref) if mut_ref is not None else None
        ov_blob = {str(a): v for a, v in overrides.items()} if overrides else None
        if slot == "drone":
            item = {"type_id": tid, "slot": "drone", "state": "active",
                    "charge_type_id": None, "quantity": qty}
            if ov_blob:
                item["attr_overrides"] = ov_blob
            items.append(item)
        elif slot == "fighter":
            # WS-12: EFT renders a squadron as "Templar II x6" (same "Name xN" form as a
            # drone); the quantity is the squadron count. pyfa distinguishes the two by the
            # item's category on import — here infer_slots already mapped category 87 → fighter.
            items.append({"type_id": tid, "slot": "fighter", "state": "active",
                          "charge_type_id": None, "quantity": qty})
        elif slot == "charge":
            # A bare charge line = cargo; not loaded into a module.
            items.append({"type_id": tid, "slot": "cargo", "state": "offline",
                          "charge_type_id": None, "quantity": qty})
        else:
            for _ in range(max(1, qty) if slot in ("high", "med", "low", "rig") else 1):
                item = {"type_id": tid, "slot": slot, "state": "active",
                        "charge_type_id": charge_id, "quantity": 1}
                if ov_blob:
                    item["attr_overrides"] = ov_blob
                items.append(item)
    return {"ship_name": ship_name, "ship_type_id": ship_type_id, "fit_name": fit_name or ship_name,
            "items": items, "unresolved": unresolved}


def export_eft(ship_type_id: int, items: list[dict], fit_name: str = "Fit") -> str:
    """Deterministic EFT export for a revision. Groups by slot in EVE-client order and
    renders ``Module, Charge`` / ``Drone xN``.

    A mutated (abyssal) module gets a trailing ``[N]`` reference on its rack line and a
    matching mutation block appended after all racks, in pyfa's syntax (see the mutation
    constants above). FORCA→FORCA round-trips every override exactly; the emitted mutaplasmid
    line is a placeholder because FORCA does not track mutaplasmid identity."""
    names = _type_names({ship_type_id} | {int(i["type_id"]) for i in items}
                        | {int(i["charge_type_id"]) for i in items if i.get("charge_type_id")})
    ship = names.get(int(ship_type_id), f"TypeID:{ship_type_id}")
    order = {"low": 0, "med": 1, "high": 2, "rig": 3, "subsystem": 4, "drone": 5,
             "fighter": 6, "cargo": 7}
    # The tactical-mode marker and projected hostile modules are not EFT lines (EFT carries
    # neither) — skip them so the export round-trips cleanly.
    items = [it for it in items if not is_extra_item(it)]

    # Resolve every mutated attribute id to its dogma name once (for the block attr lines).
    mut_attr_ids: set[int] = set()
    for it in items:
        mut_attr_ids |= set(_overrides_of(it)) if it.get("attr_overrides") else set()
    attr_names = dict(SdeDogmaAttribute.objects.filter(
        attribute_id__in=list(mut_attr_ids)).values_list("attribute_id", "name")) \
        if mut_attr_ids else {}

    lines = [f"[{ship}, {fit_name}]"]
    mutation_blocks: list[str] = []
    ref = 1
    last_slot = None
    for it in sorted(items, key=lambda i: (order.get(i.get("slot"), 9), int(i["type_id"]))):
        slot = it.get("slot")
        if last_slot is not None and slot != last_slot:
            lines.append("")
        last_slot = slot
        name = names.get(int(it["type_id"]), f"TypeID:{it['type_id']}")
        overrides = _overrides_of(it) if it.get("attr_overrides") else {}
        suffix = ""
        if overrides:
            # Attribute line: "name value" pairs sorted by attribute name (pyfa's ordering).
            rendered = ", ".join(
                f"{attr_names.get(aid, str(aid))} {_fmt_mut_value(val)}"
                for aid, val in sorted(overrides.items(),
                                       key=lambda kv: attr_names.get(kv[0], str(kv[0]))))
            mutation_blocks.append(
                f"[{ref}] {name}\n  {_MUTAPLASMID_PLACEHOLDER}\n  {rendered}")
            suffix = f" [{ref}]"
            ref += 1
        if slot in ("drone", "fighter"):
            lines.append(f"{name} x{it.get('quantity', 1)}{suffix}")
        elif slot == "cargo":
            # Cargo items carry a real stack quantity (spare ammo/modules/scripts). pyfa renders
            # each cargo line as "Name xN"; mirror it so the count round-trips through import_eft
            # (which reads the trailing xN via _QTY_RE). A single item stays a bare name.
            qty = int(it.get("quantity", 1) or 1)
            lines.append(f"{name} x{qty}{suffix}" if qty > 1 else f"{name}{suffix}")
        elif it.get("charge_type_id"):
            lines.append(
                f"{name}, {names.get(int(it['charge_type_id']), '')}".rstrip(", ") + suffix)
        else:
            lines.append(f"{name}{suffix}")

    if mutation_blocks:
        lines.append("")
        lines.extend(mutation_blocks)
    return "\n".join(lines)


def _type_names(type_ids) -> dict[int, str]:
    return dict(SdeType.objects.filter(type_id__in=list(type_ids)).values_list("type_id", "name"))


def training_plan_text(missing_skills: list[dict]) -> str:
    """The fit's missing skills, expanded to their full prerequisite closure and ordered,
    as an EVE skill-planner clipboard paste (``Skill Name <level>`` per line).

    Reuses the skills app's prerequisite expansion + topological ordering (the same code
    the doctrine skill plans use); Tocha's Lab does not re-walk the skill graph."""
    from apps.skills.prereqs import expand_prerequisites, order_by_prereqs

    targets = {int(m["skill_type_id"]): int(m["required_level"]) for m in (missing_skills or [])}
    if not targets:
        return ""
    ordered = order_by_prereqs(expand_prerequisites(targets))
    names = _type_names([sid for sid, _ in ordered])
    return "\n".join(f"{names.get(sid, f'Skill {sid}')} {lvl}" for sid, lvl in ordered)


def items_from_esi_fitting(esi: dict) -> tuple[int, list[dict]]:
    """Convert an ESI-shaped fitting ({ship_type_id, items:[{flag,quantity,type_id}]}) —
    e.g. from apps.killboard.fitrender.esi_fitting — into a Tocha's Lab loadout."""
    from apps.killboard.fitrender import slot_bucket

    ship_type_id = int(esi["ship_type_id"])
    raw = esi.get("items", [])
    module_ids = {int(i["type_id"]) for i in raw}
    slots = infer_slots(module_ids)
    items = []
    for i in raw:
        tid = int(i["type_id"])
        bucket = slot_bucket(int(i["flag"]))
        # "implant" (ESI flag 89) must survive as an implant, not fall through to SDE slot
        # inference — implants carry no slot-defining effect, so infer_slots would mis-file
        # one as a low-slot module (a killmail-imported implant then silently buffs the ship
        # as if fitted low). The engine evaluates implants as their own kind, so keep the bucket.
        slot = bucket if bucket in ("high", "med", "low", "rig", "subsystem", "drone",
                                    "cargo", "implant") \
            else slots.get(tid, "cargo")
        # Cargo (killmail loot) must never influence telemetry — import it inert, the
        # same way EFT import does. Racked modules, drones and implants start active.
        state = "offline" if slot == "cargo" else "active"
        items.append({"type_id": tid, "slot": slot, "state": state,
                      "charge_type_id": None, "quantity": int(i.get("quantity", 1))})
    return ship_type_id, items


def items_from_doctrine_fit(doctrine_fit) -> tuple[int, list[dict]]:
    """Convert a DoctrineFit (.modules [{type_id,quantity,slot}]) into a loadout.

    A doctrine persists its slots as display labels ("High 0"); canonicalise each to an
    engine rack token so the loaded fit drops every module into the correct slot, with
    SDE-inferred slots as the fallback when a label isn't recognisable. Racked modules are
    expanded one-per-slot (five turrets → five high-slot entries) so the editor's slot racks
    fill as a pilot expects; drones/cargo stay a single stacked entry."""
    module_ids = {int(m["type_id"]) for m in (doctrine_fit.modules or [])}
    slots = infer_slots(module_ids)
    items = []
    for m in doctrine_fit.modules or []:
        tid = int(m["type_id"])
        slot = canonical_slot(m.get("slot")) or slots.get(tid, "low")
        qty = max(1, int(m.get("quantity", 1) or 1))
        racked = slot in _RACKED_SLOTS
        for _ in range(qty if racked else 1):
            items.append({"type_id": tid, "slot": slot, "state": "active",
                          "charge_type_id": None, "quantity": 1 if racked else qty})
    return int(doctrine_fit.ship_type_id), items


# --------------------------------------------------------------------------- #
# Market / stock / supply overlays (read-only; reuse the FORCA authorities)
# --------------------------------------------------------------------------- #
def price_fit(ship_type_id: int, items: list[dict]) -> dict:
    """Estimated fit cost via the market pricing authority (Jita sell → adjusted → 0).

    Uses ``build_price_index`` once (N+1-safe). A 0 price means *unpriced* — surfaced,
    never summed as a real zero. Includes the price 'as of' stamp for honesty."""
    from apps.market.pricing import build_price_index, price_maps

    lookup = build_price_index()
    counts: dict[int, int] = {int(ship_type_id): 1}
    for it in items:
        if is_extra_item(it):
            continue                            # a mode / projected module is not purchasable
        counts[int(it["type_id"])] = counts.get(int(it["type_id"]), 0) + int(it.get("quantity", 1))
        if it.get("charge_type_id"):
            cid = int(it["charge_type_id"])
            counts[cid] = counts.get(cid, 0) + 1
    names = _type_names(counts)
    meta = price_maps().get("meta", {})
    lines, total, unpriced = [], Decimal("0"), []
    as_of = None
    for tid, qty in counts.items():
        unit = lookup(tid)
        line_total = unit * qty
        total += line_total
        if unit <= 0:
            unpriced.append(names.get(tid, str(tid)))
        m = meta.get(tid)
        if m and m[1] and (as_of is None or m[1] > as_of):
            as_of = m[1]
        lines.append({"type_id": tid, "name": names.get(tid, str(tid)), "qty": qty,
                      "unit": unit, "total": line_total})
    return {"total": total, "lines": lines, "unpriced": unpriced, "as_of": as_of}


def stock_coverage(ship_type_id: int, items: list[dict]) -> dict:
    """Corp-stock coverage of the fit's components (reuses stockpile.availability.available)."""
    from apps.stockpile.availability import available

    counts: dict[int, int] = {int(ship_type_id): 1}
    for it in items:
        if is_extra_item(it):
            continue                            # a mode / projected module is not stockable
        counts[int(it["type_id"])] = counts.get(int(it["type_id"]), 0) + int(it.get("quantity", 1))
        # Charges/ammo count toward stock too (mirrors price_fit): a hull+modules fit with no
        # corp ammo must NOT read "all components available". A charge is counted once per
        # loaded module (the launcher/turret count), matching how pricing counts it. Fighter
        # squadrons are ordinary items above (quantity = squadron size) so they are covered.
        if it.get("charge_type_id"):
            cid = int(it["charge_type_id"])
            counts[cid] = counts.get(cid, 0) + 1
    have = available(list(counts))
    names = _type_names(counts)
    rows, missing = [], []
    for tid, need in counts.items():
        on_hand = int(have.get(tid, 0))
        short = max(0, need - on_hand)
        if short:
            missing.append({"type_id": tid, "name": names.get(tid, str(tid)), "short": short})
        rows.append({"type_id": tid, "name": names.get(tid, str(tid)), "need": need,
                     "on_hand": on_hand, "short": short})
    return {"rows": rows, "missing": missing, "covered": not missing}


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def compare(rev_a: FitRevision, rev_b: FitRevision, skills: SkillProfile,
            op: OperatingProfile | None = None) -> dict:
    """Module and telemetry differences between two revisions under one skill profile."""
    ta = evaluate(rev_a.ship_type_id, rev_a.items, skills, op)
    tb = evaluate(rev_b.ship_type_id, rev_b.items, skills, op)

    def _mods(items):
        c: dict[int, int] = {}
        for it in items:
            c[int(it["type_id"])] = c.get(int(it["type_id"]), 0) + int(it.get("quantity", 1))
        return c

    ma, mb = _mods(rev_a.items), _mods(rev_b.items)
    names = _type_names(set(ma) | set(mb))
    added = [{"type_id": t, "name": names.get(t, str(t)), "qty": mb[t] - ma.get(t, 0)}
             for t in mb if mb[t] > ma.get(t, 0)]
    removed = [{"type_id": t, "name": names.get(t, str(t)), "qty": ma[t] - mb.get(t, 0)}
               for t in ma if ma[t] > mb.get(t, 0)]

    # Metric deltas with a "higher is better?" flag so the UI colours changes contextually.
    def g(t, *path):
        cur = t
        for p in path:
            cur = (cur or {}).get(p, {})
        return cur if isinstance(cur, int | float) else 0

    metrics = [
        ("total_dps", g(tb, "offence", "total_dps") - g(ta, "offence", "total_dps"), True),
        ("ehp_total", g(tb, "defence", "ehp_total") - g(ta, "defence", "ehp_total"), True),
        ("max_velocity", g(tb, "mobility", "max_velocity") - g(ta, "mobility", "max_velocity"), True),
        ("align_time_s", g(tb, "mobility", "align_time_s") - g(ta, "mobility", "align_time_s"), False),
        ("signature_radius", g(tb, "mobility", "signature_radius") - g(ta, "mobility", "signature_radius"), False),
    ]
    return {
        "added": added, "removed": removed,
        "metrics": [{"key": k, "delta": round(d, 2), "higher_better": hb} for k, d, hb in metrics],
        "a": ta, "b": tb,
    }


# --------------------------------------------------------------------------- #
# Doctrine promotion (officer-gated, audited)
# --------------------------------------------------------------------------- #
@transaction.atomic
def promote_to_doctrine(fit: Fit, revision: FitRevision, doctrine, actor, *,
                        name: str | None = None, is_cheap_alt: bool = False):
    """Promote a fit revision into a doctrine fit (reuses doctrines.services.create_fit,
    which derives skill requirements). A deliberate, audited officer action — saving a fit
    never publishes a doctrine."""
    from apps.doctrines.services import create_fit as create_doctrine_fit

    modules = []
    for it in revision.items:
        if it.get("slot") == "cargo" or is_extra_item(it):
            continue                            # cargo loot / mode / projected are not doctrine modules
        modules.append({"type_id": int(it["type_id"]), "quantity": int(it.get("quantity", 1)),
                        "slot": it.get("slot", "")})
    doctrine_fit = create_doctrine_fit(
        doctrine, name=name or fit.name, ship_type_id=revision.ship_type_id,
        modules=modules, is_cheap_alt=is_cheap_alt,
    )
    fit.promoted_doctrine_fit_id = doctrine_fit.pk
    fit.visibility = Visibility.DOCTRINE
    fit.save(update_fields=["promoted_doctrine_fit_id", "visibility", "updated_at"])
    audit_log(actor, "tochaslab.doctrine_candidate.published", target_type="doctrines.DoctrineFit",
              target_id=doctrine_fit.pk, metadata={"fit": fit.pk, "doctrine": doctrine.pk})
    return doctrine_fit
