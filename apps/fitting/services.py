"""Tocha's Lab domain services.

The orchestration layer between the workspace views and (a) the calculation engine and
(b) the FORCA subsystems Tocha's Lab integrates with — skills/readiness, market pricing,
Shipyard/stock availability, doctrines and supply. Everything here reuses the existing
authorities (see handbooks/contributor-handbook/decision-log.md TL1/TL2); nothing is
re-implemented.
"""
from __future__ import annotations

import re
from decimal import Decimal

from django.db import transaction

from apps.sde.models import SdeType, SdeTypeAttribute, SdeTypeEffect
from core.audit import audit_log

from .engine import attributes as A
from .engine.adapter import FittingEngine
from .engine.types import (
    DamageProfileInput,
    FitInput,
    ModuleInput,
    ModuleState,
    OperatingMode,
    OperatingProfile,
    SkillProfile,
    SlotKind,
    TargetProfile,
)
from .models import Fit, FitRevision, Visibility

_MAX_LINES = 500  # mirrors apps/doctrines/fitparser — bound a pathological paste
_QTY_RE = re.compile(r"\sx(\d+)\s*$", re.IGNORECASE)
_CATEGORY_CHARGE = 8
_CATEGORY_DRONE = 18


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


def fit_input_from_items(ship_type_id: int, items: list[dict]) -> FitInput:
    """Build the engine's :class:`FitInput` from a revision's canonical ``items`` list."""
    modules = []
    for it in items or []:
        slot = _SLOT_BY_VALUE.get(it.get("slot"), SlotKind.LOW)
        state = _STATE_BY_VALUE.get(it.get("state"), ModuleState.ACTIVE)
        modules.append(ModuleInput(
            type_id=int(it["type_id"]), slot=slot, state=state,
            charge_type_id=(int(it["charge_type_id"]) if it.get("charge_type_id") else None),
            quantity=int(it.get("quantity", 1)),
        ))
    return FitInput(ship_type_id=int(ship_type_id), modules=tuple(modules))


def operating_profile(mode: str = "all_active", propulsion: bool = True,
                      damage: dict | None = None,
                      target: dict | None = None) -> OperatingProfile:
    dp = DamageProfileInput(**damage) if damage else DamageProfileInput()
    try:
        m = OperatingMode(mode)
    except ValueError:
        m = OperatingMode.ALL_ACTIVE
    tgt = None
    if target and (target.get("signature_radius") or target.get("velocity")):
        tgt = TargetProfile(
            signature_radius=max(0.0, float(target.get("signature_radius") or 0)),
            velocity=max(0.0, float(target.get("velocity") or 0)),
            label=str(target.get("label", ""))[:60],
        )
    return OperatingProfile(mode=m, propulsion_active=propulsion, damage_profile=dp, target=tgt)


def evaluate(ship_type_id: int, items: list[dict], skills: SkillProfile,
             op: OperatingProfile | None = None, *, cached: bool = True) -> dict:
    """A flat telemetry dict for a loadout (via the adapter), ready for the template.

    The engine's ``to_dict`` nests the telemetry groups (resources/offence/…) under a
    ``telemetry`` key alongside diagnostics/versions; the workspace template and the
    comparison want them flattened into one namespace, so lift the groups up a level and
    merge the metadata (diagnostics, missing_skills, engine/data version, status)."""
    from .diagnostics import localise_diagnostics, localise_unsupported

    engine = FittingEngine()
    fit = fit_input_from_items(ship_type_id, items)
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


@transaction.atomic
def save_revision(fit: Fit, *, ship_type_id: int, items: list[dict], user,
                  change_summary: str = "", notes: str = "") -> FitRevision:
    """Append a new immutable revision and point the fit at it. Never mutates history."""
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


def import_eft(text: str) -> dict:
    """Parse EFT text into a Tocha's Lab loadout, preserving module+charge pairs and
    inferring each module's slot. Defensive: line-bounded, name-normalised through local
    SDE data only, never evaluated. Returns items + a list of unresolved names."""
    lines = [ln.rstrip() for ln in (text or "").strip().splitlines()][:_MAX_LINES]
    if not lines or not lines[0].startswith("["):
        raise ValueError("EFT must start with '[ShipName, Fit name]'")
    header = lines[0].strip().lstrip("[").rstrip("]")
    ship_name, _sep, fit_name = header.partition(",")
    ship_name, fit_name = ship_name.strip(), fit_name.strip()

    entries: list[tuple[str, str | None, int]] = []
    names: set[str] = {ship_name}
    for raw in lines[1:]:
        line = raw.strip()
        if not line or line.startswith("["):
            continue
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
        entries.append((module_name, charge_name, qty))
        names.add(module_name)
        if charge_name:
            names.add(charge_name)

    resolved = _resolve_names(names)
    ship_type_id = resolved.get(ship_name.lower())
    module_ids = {resolved[e[0].lower()] for e in entries if e[0].lower() in resolved}
    slots = infer_slots(module_ids)

    items: list[dict] = []
    unresolved: list[str] = []
    for module_name, charge_name, qty in entries:
        tid = resolved.get(module_name.lower())
        if tid is None:
            unresolved.append(module_name)
            continue
        slot = slots.get(tid, "low")
        charge_id = resolved.get(charge_name.lower()) if charge_name else None
        if charge_name and charge_id is None:
            unresolved.append(charge_name)
        if slot == "drone":
            items.append({"type_id": tid, "slot": "drone", "state": "active",
                          "charge_type_id": None, "quantity": qty})
        elif slot == "charge":
            # A bare charge line = cargo; not loaded into a module.
            items.append({"type_id": tid, "slot": "cargo", "state": "offline",
                          "charge_type_id": None, "quantity": qty})
        else:
            for _ in range(max(1, qty) if slot in ("high", "med", "low", "rig") else 1):
                items.append({"type_id": tid, "slot": slot, "state": "active",
                              "charge_type_id": charge_id, "quantity": 1})
    return {"ship_name": ship_name, "ship_type_id": ship_type_id, "fit_name": fit_name or ship_name,
            "items": items, "unresolved": unresolved}


def export_eft(ship_type_id: int, items: list[dict], fit_name: str = "Fit") -> str:
    """Deterministic EFT export for a revision. Groups by slot in EVE-client order and
    renders ``Module, Charge`` / ``Drone xN``."""
    names = _type_names({ship_type_id} | {int(i["type_id"]) for i in items}
                        | {int(i["charge_type_id"]) for i in items if i.get("charge_type_id")})
    ship = names.get(int(ship_type_id), f"TypeID:{ship_type_id}")
    order = {"low": 0, "med": 1, "high": 2, "rig": 3, "subsystem": 4, "drone": 5, "cargo": 6}
    lines = [f"[{ship}, {fit_name}]"]
    last_slot = None
    for it in sorted(items, key=lambda i: (order.get(i.get("slot"), 9), int(i["type_id"]))):
        slot = it.get("slot")
        if last_slot is not None and slot != last_slot:
            lines.append("")
        last_slot = slot
        name = names.get(int(it["type_id"]), f"TypeID:{it['type_id']}")
        if slot == "drone":
            lines.append(f"{name} x{it.get('quantity', 1)}")
        elif it.get("charge_type_id"):
            lines.append(f"{name}, {names.get(int(it['charge_type_id']), '')}".rstrip(", "))
        else:
            lines.append(name)
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
        slot = bucket if bucket in ("high", "med", "low", "rig", "subsystem", "drone", "cargo") \
            else slots.get(tid, "cargo")
        items.append({"type_id": tid, "slot": slot, "state": "active",
                      "charge_type_id": None, "quantity": int(i.get("quantity", 1))})
    return ship_type_id, items


def items_from_doctrine_fit(doctrine_fit) -> tuple[int, list[dict]]:
    """Convert a DoctrineFit (.modules [{type_id,quantity,slot}]) into a loadout."""
    module_ids = {int(m["type_id"]) for m in (doctrine_fit.modules or [])}
    slots = infer_slots(module_ids)
    items = []
    for m in doctrine_fit.modules or []:
        tid = int(m["type_id"])
        slot = m.get("slot") or slots.get(tid, "low")
        for _ in range(int(m.get("quantity", 1)) if slot in ("high", "med", "low", "rig") else 1):
            items.append({"type_id": tid, "slot": slot, "state": "active",
                          "charge_type_id": None, "quantity": 1
                          if slot in ("high", "med", "low", "rig") else int(m.get("quantity", 1))})
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
        counts[int(it["type_id"])] = counts.get(int(it["type_id"]), 0) + int(it.get("quantity", 1))
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
        if it.get("slot") == "cargo":
            continue
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
