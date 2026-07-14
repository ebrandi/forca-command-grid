"""Capsuleer Path plan generation and template instantiation (brief §6, doc 05 §2, doc 11 §3).

Two related concerns that both turn a goal's *targets* into concrete, trainable rows:

* :func:`build_plan` resolves a goal's skill targets — a template's named skill tiers, a doctrine's
  derived requirements, or a ship hull's SDE prerequisites — into a personalised, prerequisite-
  expanded ``skills.SkillPlan`` (written as the reserved ``Goal.CUSTOM`` slot so the existing skills
  UI, clipboard export and self-reconcile all work unchanged). Steps are split minimum / recommended
  / mastery, ordered so prerequisites train first (the ``apps.skills.prereqs`` shared expander fixes
  the documented prerequisite undercount), and estimated attribute-aware. Hull cost is priced via
  ``market.price_for`` with ``Decimal(0)`` rendered as *unknown*, never *free*.

* :func:`instantiate_template` turns a :class:`CareerTemplate.structure` blueprint into a goal with
  concrete milestones: skill/ship names resolve to SDE type ids, the ``$doctrine`` placeholder and
  ``doctrine_hull`` marker resolve through the template's ``doctrine_resolver`` against live active
  doctrines (degrading honestly to ``unresolved`` when nothing matches, doc 05 §13.3), and every
  materialised milestone passes the ``params.py`` validators.

Both are read-mostly over other apps (the one write into another app is the sanctioned
``derive_skill_requirements`` when a doctrine fit lacks derived requirements) and are called from the
service layer, never a view directly.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.db import transaction

from apps.skills.prereqs import expand_prerequisites, order_by_prereqs

from . import templates_i18n as t_i18n
from .models import CareerMilestone, GoalStatus, GoalType, MilestoneKind, Verification
from .params import validate_milestone_params

logger = logging.getLogger("forca.capsuleer")

# Skill-tier bands and the SkillPlanStep.reason they stamp.
BAND_MINIMUM, BAND_RECOMMENDED, BAND_MASTERY = "minimum", "recommended", "mastery"
_BAND_REASON = {
    BAND_MINIMUM: "minimum qualification",
    BAND_RECOMMENDED: "recommended support",
    BAND_MASTERY: "mastery",
}
_BAND_ORDER = (BAND_MINIMUM, BAND_RECOMMENDED, BAND_MASTERY)


# --------------------------------------------------------------------------- #
#  Skill-target resolution per goal type
# --------------------------------------------------------------------------- #
def _resolve_skill_names(entries) -> dict[int, int]:
    """``[{name, level}]`` → ``{type_id: level}`` via SDE name resolution; unresolved names dropped
    (retained-by-name/excluded-from-plan-math, doc 13.1)."""
    from apps.sde.search import resolve_type

    targets: dict[int, int] = {}
    for entry in entries or []:
        sde_type = resolve_type(entry.get("name", ""))
        if sde_type is None:
            continue
        level = max(1, min(int(entry.get("level", 1)), 5))
        targets[sde_type.type_id] = max(targets.get(sde_type.type_id, 0), level)
    return targets


def _doctrine_best_fit(doctrine):
    """The fit a doctrine plan targets: prefer a non-cheap-alt fit with derived requirements, then
    the first fit (deriving requirements if a fit has none yet — the sanctioned doctrines path)."""
    from apps.doctrines.services import derive_skill_requirements

    fits = sorted(doctrine.fits.all(), key=lambda f: (f.is_cheap_alt, f.id))
    if not fits:
        return None
    for fit in fits:
        if fit.skill_requirements.exists():
            return fit
    chosen = fits[0]
    derive_skill_requirements(chosen)
    return chosen


def _tier_targets(goal) -> dict[str, dict[int, int]]:
    """``{band: {skill_type_id: level}}`` for the goal, before prerequisite expansion."""
    bands = {BAND_MINIMUM: {}, BAND_RECOMMENDED: {}, BAND_MASTERY: {}}

    if goal.goal_type == GoalType.TEMPLATE and goal.template_id:
        tiers = (goal.template.structure or {}).get("skill_tiers", {})
        for band in _BAND_ORDER:
            bands[band] = _resolve_skill_names(tiers.get(band, []))
        return bands

    if goal.goal_type == GoalType.DOCTRINE and goal.doctrine_id:
        from apps.doctrines.models import Doctrine

        doctrine = Doctrine.objects.filter(id=goal.doctrine_id).first()
        fit = _doctrine_best_fit(doctrine) if doctrine else None
        if fit is not None:
            for req in fit.skill_requirements.all():
                bands[BAND_MINIMUM][req.skill_type_id] = req.min_level
                if req.optimal_level > req.min_level:
                    bands[BAND_RECOMMENDED][req.skill_type_id] = req.optimal_level
        return bands

    if goal.goal_type == GoalType.SHIP and goal.ship_type_id:
        from apps.sde.models import SdeTypeSkill

        for row in SdeTypeSkill.objects.filter(type_id=goal.ship_type_id):
            bands[BAND_MINIMUM][row.skill_type_id] = row.level
        return bands

    return bands


def _skill_seconds_map(character, skill_ids) -> dict[int, tuple]:
    """``{type_id: (rank, primary_attr_id, secondary_attr_id)}`` for the given skills."""
    from apps.sde.models import SdeType

    return {
        row["type_id"]: (row["rank"] or 1, row["primary_attribute_id"], row["secondary_attribute_id"])
        for row in SdeType.objects.filter(type_id__in=list(skill_ids)).values(
            "type_id", "rank", "primary_attribute_id", "secondary_attribute_id"
        )
    }


# --------------------------------------------------------------------------- #
#  build_plan (brief §6)
# --------------------------------------------------------------------------- #
@transaction.atomic
def build_plan(goal):
    """Create or refresh the goal's ``skills.SkillPlan`` (``Goal.CUSTOM``); return it, or ``None``.

    Resolves the goal's skill targets by type, expands prerequisites, prunes already-trained skills
    against the evidence character's latest snapshot, splits minimum/recommended/mastery, orders the
    outstanding steps prerequisites-first (quick wins tie-break), and estimates each attribute-aware.
    Replaces any prior plan for the goal (idempotent refresh). Goals with no skill targets (mentor /
    activity / custom) get no plan and return ``None``.
    """
    from apps.characters.models import CharacterAttributes
    from apps.skills.models import SkillPlan, SkillPlanStep
    from apps.skills.services import SP_PER_HOUR, sp_between_levels
    from apps.skills.training import sp_per_hour

    if goal.character_id is None:
        return None
    tier_targets = _tier_targets(goal)

    # Merge to a single {sid: max_level}; a skill's band is the tier that set its final (highest)
    # target — a skill trained to its optimal level reads "recommended", one held at minimum reads
    # "minimum". Expansion pulls in untrained prerequisites (the undercount fix), which inherit the
    # band of whatever tier demanded them at that level.
    merged: dict[int, int] = {}
    band_of: dict[int, str] = {}
    for band in _BAND_ORDER:
        expanded = expand_prerequisites(tier_targets[band])
        for sid, level in expanded.items():
            if level > merged.get(sid, 0):
                merged[sid] = level
                band_of[sid] = band
            elif sid not in band_of:
                band_of[sid] = band
    if not merged:
        _clear_existing_plan(goal)
        return None

    snapshot = goal.character.skill_snapshots.filter(is_latest=True).first()
    meta = _skill_seconds_map(goal.character, merged)
    missing = {}
    for sid, level in merged.items():
        have = snapshot.trained_level(sid) if snapshot else 0
        if have < level:
            missing[sid] = level

    _clear_existing_plan(goal)
    if not missing:
        # Every target already trained — a real, empty plan is still meaningful (ETA = trained).
        plan = SkillPlan.objects.create(
            character=goal.character, name=f"Capsuleer Path — {goal.title}"[:200],
            goal=SkillPlan.Goal.CUSTOM, estimated_total_seconds=0,
        )
        _link_plan(goal, plan, {"minimum": 0, "recommended": 0, "mastery": 0})
        return plan

    attrs = CharacterAttributes.objects.filter(character=goal.character).first()

    def _sp_gap(sid):
        rank = meta.get(sid, (1, None, None))[0]
        have = snapshot.trained_level(sid) if snapshot else 0
        return sp_between_levels(rank, have, missing[sid])

    ordered = order_by_prereqs(missing, sp_of=_sp_gap)

    plan = SkillPlan.objects.create(
        character=goal.character, name=f"Capsuleer Path — {goal.title}"[:200],
        goal=SkillPlan.Goal.CUSTOM,
    )
    total_seconds = 0
    band_counts = {BAND_MINIMUM: 0, BAND_RECOMMENDED: 0, BAND_MASTERY: 0}
    for order, (sid, level) in enumerate(ordered):
        rank, primary, secondary = meta.get(sid, (1, None, None))
        have = snapshot.trained_level(sid) if snapshot else 0
        sp = sp_between_levels(rank, have, level)
        rate = sp_per_hour(attrs, primary, secondary, SP_PER_HOUR)
        seconds = int(sp / rate * 3600)
        total_seconds += seconds
        band = band_of.get(sid, BAND_MINIMUM)
        band_counts[band] += 1
        SkillPlanStep.objects.create(
            plan=plan, order=order, skill_type_id=sid, target_level=level,
            estimated_seconds=seconds, reason=_BAND_REASON[band],
        )
    plan.estimated_total_seconds = total_seconds
    plan.save(update_fields=["estimated_total_seconds"])
    _link_plan(goal, plan, band_counts)
    return plan


def _clear_existing_plan(goal) -> None:
    """Drop the goal's prior generated plan so a rebuild stays clean (one plan per goal)."""
    from apps.skills.models import SkillPlan

    if goal.skill_plan_id:
        SkillPlan.objects.filter(pk=goal.skill_plan_id).delete()
        goal.skill_plan_id = None
        goal.save(update_fields=["skill_plan_id", "updated_at"])


def _link_plan(goal, plan, band_counts) -> None:
    """Point the goal at its plan and record a tier-safe ``plan.generated`` activity row."""
    from . import services

    goal.skill_plan_id = plan.pk
    goal.save(update_fields=["skill_plan_id", "updated_at"])
    snapshot = goal.character.skill_snapshots.filter(is_latest=True).first()
    cost = estimate_initial_cost(goal)
    services.record_activity(goal, None, "plan.generated", {
        "plan_id": plan.pk,
        "minimum": band_counts.get(BAND_MINIMUM, 0),
        "recommended": band_counts.get(BAND_RECOMMENDED, 0),
        "mastery": band_counts.get(BAND_MASTERY, 0),
        "est_cost_isk": str(cost["isk"]),
        "cost_unknown": cost["unknown"],
        "as_of": snapshot.as_of.isoformat() if snapshot else None,
    })


def estimate_initial_cost(goal) -> dict:
    """Estimated hull acquisition cost for the goal (market-derived, S-class — not budget).

    Prices the cheapest resolvable hull among the goal's ship targets via ``market.price_for``;
    ``Decimal(0)`` from the pricer means *unknown* and is reported as such, never as free.
    """
    from django.utils import timezone

    from apps.market.pricing import price_for

    source = "corp market pricing"
    hull_ids = _hull_type_ids(goal)
    if not hull_ids:
        return {"isk": Decimal("0"), "unknown": True, "source": source, "as_of": None}
    prices = [price_for(tid) for tid in hull_ids]
    known = [p for p in prices if p and p > 0]
    if not known:
        return {"isk": Decimal("0"), "unknown": True, "source": source, "as_of": None}
    return {"isk": min(known), "unknown": False, "source": source, "as_of": timezone.now().date()}


def _hull_type_ids(goal) -> list[int]:
    from apps.sde.search import resolve_type

    if goal.goal_type == GoalType.SHIP and goal.ship_type_id:
        return [goal.ship_type_id]
    if goal.goal_type == GoalType.DOCTRINE and goal.doctrine_id:
        from apps.doctrines.models import Doctrine

        doctrine = Doctrine.objects.filter(id=goal.doctrine_id).first()
        return [f.ship_type_id for f in doctrine.fits.all()] if doctrine else []
    if goal.goal_type == GoalType.TEMPLATE and goal.template_id:
        ids = []
        for target in (goal.template.structure or {}).get("ship_targets", []):
            name = target.get("name")
            if name:
                sde_type = resolve_type(name)
                if sde_type is not None:
                    ids.append(sde_type.type_id)
        return ids
    return []


# --------------------------------------------------------------------------- #
#  Template instantiation (doc 05 §2.2, §13.3)
# --------------------------------------------------------------------------- #
def resolve_doctrine(resolver: dict):
    """The highest-priority active doctrine matching a ``doctrine_resolver`` (doc 05 §13.3), or None.

    Matches when the doctrine or a fit's free-form ``role`` (or the doctrine name) case-insensitively
    contains one of the resolver's tokens. Ordered by ``-priority`` then id, so the first match is the
    highest-priority doctrine.
    """
    from apps.doctrines.models import Doctrine

    role_tokens = [t.lower() for t in resolver.get("role_tokens", []) if t]
    name_tokens = [t.lower() for t in resolver.get("name_tokens", []) if t]
    all_tokens = role_tokens + name_tokens
    if not all_tokens:
        return None
    doctrines = (
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .prefetch_related("fits").order_by("-priority", "id")
    )
    for doctrine in doctrines:
        name = (doctrine.name or "").lower()
        if any(tok in name for tok in name_tokens):
            return doctrine
        for fit in doctrine.fits.all():
            role = (fit.role or "").lower()
            if any(tok in role for tok in all_tokens):
                return doctrine
    return None


def _concrete_milestone_params(ms: dict, doctrine, goal):
    """Resolve a template milestone's authored params to concrete, validator-passing params.

    Returns ``(params, verification, note)`` where ``note`` is a system GoalActivity marker to record
    (or ``None``), or ``(None, None, note)`` to signal the milestone should be skipped (its references
    could not be resolved). Doctrine placeholder / hull markers resolve through ``doctrine``.
    """
    from apps.sde.search import resolve_type

    kind = ms["kind"]
    params = dict(ms.get("params") or {})
    verification = ms.get("verification", Verification.AUTO)

    if kind == MilestoneKind.SKILL_TARGET:
        resolved = _resolve_skill_names(params.get("skills", []))
        if not resolved:
            return None, None, "skill milestone unresolved against current SDE"
        return {"skills": [{"type_id": sid, "level": lvl} for sid, lvl in resolved.items()]}, \
            verification, None

    if kind == MilestoneKind.SHIP_OWNED:
        if params.get("resolve") == "doctrine_hull":
            if doctrine is None:
                return None, None, "no matching doctrine available"
            type_ids = [f.ship_type_id for f in doctrine.fits.all() if f.ship_type_id]
        else:
            type_ids = []
            for name in params.get("type_names", []):
                sde_type = resolve_type(name)
                if sde_type is not None:
                    type_ids.append(sde_type.type_id)
        type_ids = list(dict.fromkeys(type_ids))  # de-dupe, preserve order
        if not type_ids:
            return None, None, "ship milestone unresolved against current SDE"
        out = {"type_ids": type_ids}
        if params.get("require_fitted"):
            out["require_fitted"] = True
        return out, verification, None

    if kind == MilestoneKind.DOCTRINE_READY:
        tier = params.get("tier", "viable")
        if doctrine is None:
            return {"unresolved": True, "tier": tier}, verification, "no matching doctrine available"
        return {"doctrine_id": doctrine.id, "tier": tier}, verification, None

    # contribution / combat_first / practical / manual — already concrete and validator-valid.
    return params, verification, None


def instantiate_template(template, user, *, character=None, visibility=None, priority=None,
                         title=None, motivation="", target_date=None, activate=False):
    """Create a goal from a template's ``structure``, materialising concrete milestones (doc 05 §2.2).

    Resolves the ``doctrine_resolver`` once (degrading honestly when nothing matches), builds each
    milestone with validator-passing concrete params, and — when ``activate`` — moves the goal to
    ``active`` (which runs :func:`build_plan` and stamps contribution baselines through the service
    activation path). Returns the created (or pre-existing live) goal.

    Each copied milestone title is stamped with the built-in's stable ``source_key``
    (``templates_i18n``), so the render seam can show the *translated* built-in title while the row
    still holds the shipped English, and the pilot's own words verbatim once they edit it. The goal
    itself needs no new column: it already records ``template_key``, and its title is the path's
    name. English is what gets stored (never a lazy proxy) — the row stays the audit record.
    """
    from . import services

    structure = template.structure or {}
    resolver = structure.get("doctrine_resolver")
    doctrine = resolve_doctrine(resolver) if resolver else None

    kwargs = {}
    if visibility is not None:
        kwargs["visibility"] = visibility
    if priority is not None:
        kwargs["priority"] = priority

    goal = services.create_goal(
        user, title=title or template.name, goal_type=GoalType.TEMPLATE, template=template,
        character=character, motivation=motivation, target_date=target_date,
        doctrine_id=doctrine.id if doctrine else None, **kwargs,
    )
    # create_goal returns an existing live goal for this template unchanged — don't duplicate rows.
    if goal.milestones.exists():
        return goal

    with transaction.atomic():
        for ms in structure.get("milestones", []):
            params, verification, note = _concrete_milestone_params(ms, doctrine, goal)
            if params is None:
                services.record_activity(goal, None, "milestone.skipped_unresolved",
                                         {"title": ms.get("title", "")[:140], "reason": note})
                continue
            kind = ms["kind"]
            validate_milestone_params(kind, params, verification)
            milestone = CareerMilestone.objects.create(
                goal=goal, order=ms["order"], title=ms["title"][:140], kind=kind,
                required=ms.get("required", True), params=params, verification=verification,
                source_key=t_i18n.milestone_key(template.key, ms["order"]),
            )
            if note:
                # An unresolved reference is a permanent structural blocker until the template is
                # re-authored — stamp it so the goal page shows "blocked" on the request path without
                # waiting for the first sweep (finding 21).
                milestone.check_state = "unknown"
                milestone.data_source = note
                milestone.structural_block = True
                milestone.save(update_fields=["check_state", "data_source", "structural_block",
                                              "updated_at"])
                services.record_activity(goal, None, "milestone.degraded",
                                         {"milestone_id": milestone.pk, "reason": note})

    if activate and goal.status != GoalStatus.ACTIVE:
        goal = services.set_goal_status(goal, GoalStatus.ACTIVE, user)
    return goal
