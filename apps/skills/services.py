"""Skill-plan generation, time estimation, progress, and EVE export.

A plan turns "you can't fly this doctrine yet" into an ordered, trackable list
of skills to train — the missing skills for a doctrine, quick wins first, with
honest training-time estimates and a clipboard export the member can paste
straight into the in-game skill planner.
"""
from __future__ import annotations

import math

from django.db import transaction

from apps.doctrines.models import Doctrine
from apps.doctrines.services import character_readiness
from apps.sde.models import SdeType

from .models import SkillPlan, SkillPlanStep

# Attribute/implant-independent estimate. Real rates vary with attributes and
# implants; we label this an estimate in the UI rather than implying precision.
SP_PER_HOUR = 2000

# Sentinel: lets a caller scoring many doctrines/fits load the character's latest
# skill snapshot ONCE and thread it through, instead of re-querying it per fit (the
# N+1 that made closest_doctrines / the briefing fire 100+ identical snapshot reads).
# ``snapshot=None`` means "no snapshot" (treat every level as 0), not "go fetch it".
_SNAP_UNSET = object()


def _latest_snapshot(character, snapshot):
    return (character.skill_snapshots.filter(is_latest=True).first()
            if snapshot is _SNAP_UNSET else snapshot)


def sp_between_levels(rank: int, from_level: int, to_level: int) -> int:
    """Skillpoints to train a skill of ``rank`` from one level to another."""
    rank = rank or 1
    total = 0
    for level in range(from_level + 1, to_level + 1):
        total += math.ceil(250 * rank * (2 ** (2.5 * (level - 1))))
    return total


_ATTR_UNSET = object()


def _ranks(skill_ids: set[int]) -> dict[int, int]:
    """``skill_type_id -> training rank`` (used where attributes aren't needed)."""
    return {
        t.type_id: (t.rank or 1)
        for t in SdeType.objects.filter(type_id__in=skill_ids)
    }


def _skill_meta(skill_ids: set[int]) -> dict[int, tuple]:
    """``skill_type_id -> (rank, primary_attr_id, secondary_attr_id)`` in one query.

    Merges the rank and training-attribute lookups so the estimate paths hit SdeType
    once, not twice (they run inside the per-doctrine scan).
    """
    return {
        row["type_id"]: (row["rank"] or 1, row["primary_attribute_id"], row["secondary_attribute_id"])
        for row in SdeType.objects.filter(type_id__in=skill_ids).values(
            "type_id", "rank", "primary_attribute_id", "secondary_attribute_id"
        )
    }


def _character_attributes(character, attrs=_ATTR_UNSET):
    """The character's imported training attributes (or ``None``).

    Pass a pre-loaded ``attrs`` to reuse it across a doctrine scan rather than
    re-querying per doctrine (mirrors the ``snapshot`` threading).
    """
    if attrs is not _ATTR_UNSET:
        return attrs
    from apps.characters.models import CharacterAttributes

    return CharacterAttributes.objects.filter(character=character).first()


def _skill_seconds(sp: int, attrs, meta, skill_id: int) -> int:
    """Training seconds for ``sp`` skillpoints of one skill, attribute-aware when we can
    be (the pilot's attributes + the skill's training attributes), else the flat rate."""
    from .training import sp_per_hour

    _rank, primary, secondary = meta.get(skill_id, (1, None, None))
    rate = sp_per_hour(attrs, primary, secondary, SP_PER_HOUR)
    return int(sp / rate * 3600)


def collect_missing_for_doctrine(character, doctrine: Doctrine, snapshot=_SNAP_UNSET) -> dict[int, int]:
    """Highest required level per skill the character still lacks for a doctrine.

    A skill counts as missing if no fit's requirement is met; we take the
    maximum level demanded across the doctrine's fits. Pass ``snapshot`` to reuse a
    once-loaded snapshot across many doctrines (avoids the per-fit re-fetch).
    """
    snapshot = _latest_snapshot(character, snapshot)
    needed: dict[int, int] = {}
    for fit in doctrine.fits.all():
        readiness = character_readiness(character, fit, snapshot=snapshot)
        for miss in readiness.missing_viable:
            sid = miss["skill_type_id"]
            needed[sid] = max(needed.get(sid, 0), miss["need"])
    return needed


@transaction.atomic
def generate_plan_for_doctrine(character, doctrine: Doctrine) -> SkillPlan:
    """Create (or replace) a skill plan covering everything a doctrine needs."""
    snapshot = character.skill_snapshots.filter(is_latest=True).first()
    needed = collect_missing_for_doctrine(character, doctrine, snapshot=snapshot)
    meta = _skill_meta(set(needed))
    attrs = _character_attributes(character)

    rows = []
    for skill_id, target in needed.items():
        have = snapshot.trained_level(skill_id) if snapshot else 0
        sp = sp_between_levels(meta.get(skill_id, (1, None, None))[0], have, target)
        rows.append((skill_id, target, have, sp))
    # Quick wins first: least training time leads.
    rows.sort(key=lambda r: (r[3], r[0]))

    # Replace any prior plan for this character + doctrine so re-runs stay clean.
    SkillPlan.objects.filter(
        character=character, target_doctrine=doctrine, goal=SkillPlan.Goal.DOCTRINE
    ).delete()
    plan = SkillPlan.objects.create(
        character=character,
        name=f"Fly {doctrine.name}",
        target_doctrine=doctrine,
        goal=SkillPlan.Goal.DOCTRINE,
    )
    total_seconds = 0
    for order, (skill_id, target, _have, sp) in enumerate(rows):
        seconds = _skill_seconds(sp, attrs, meta, skill_id)
        total_seconds += seconds
        SkillPlanStep.objects.create(
            plan=plan, order=order, skill_type_id=skill_id, target_level=target,
            estimated_seconds=seconds, reason=f"for {doctrine.name}",
        )
    plan.estimated_total_seconds = total_seconds
    plan.save(update_fields=["estimated_total_seconds"])
    return plan


def estimate_seconds_to_doctrine(character, doctrine: Doctrine, snapshot=_SNAP_UNSET,
                                 attrs=_ATTR_UNSET) -> int:
    """Estimated training time for a character to reach min-viable on a doctrine.

    Zero means already viable. Sums the SP gap across every still-missing skill (best
    fit), attribute-aware where the pilot's attributes + the skill's training attributes
    are known. Pass ``snapshot`` and ``attrs`` to reuse once-loaded values across a
    many-doctrine scan (both are invariant per character).
    """
    snapshot = _latest_snapshot(character, snapshot)
    needed = collect_missing_for_doctrine(character, doctrine, snapshot=snapshot)
    if not needed:
        return 0
    meta = _skill_meta(set(needed))
    attrs = _character_attributes(character, attrs)
    total_seconds = 0
    for skill_id, target in needed.items():
        have = snapshot.trained_level(skill_id) if snapshot else 0
        sp = sp_between_levels(meta.get(skill_id, (1, None, None))[0], have, target)
        total_seconds += _skill_seconds(sp, attrs, meta, skill_id)
    return total_seconds


_CLOSEST_TTL = 900  # 15 min; skills only change on the 12h sync, and this is warmed


def invalidate_closest_doctrines(character_id) -> None:
    """Drop the cached closest-doctrines list — call when a skill snapshot is written."""
    from django.core.cache import cache

    cache.delete(f"skills:closest:{character_id}")


def closest_doctrines(character, limit: int = 4) -> list[dict]:
    """Doctrines the character is closest to flying but can't yet (top ``limit``).

    Cached per character: the underlying scan iterates every active doctrine and
    resolves SDE skill data per doctrine (~55 queries), and it runs on every
    Command-Center load. The result changes when the pilot's skills change (a 12h
    sync — invalidated on each snapshot write) OR when leadership edits the active
    doctrines/fits (self-heals within the 900s TTL — not separately invalidated).
    Cached (and warmed) and re-sliced cheaply per ``limit``.
    """
    from django.core.cache import cache

    key = f"skills:closest:{character.character_id}"
    full = cache.get(key)
    if full is None:
        full = _closest_doctrines_compute(character)
        cache.set(key, full, _CLOSEST_TTL)
    return full[:limit]


def _closest_doctrines_compute(character) -> list[dict]:
    """The full ranked list of not-yet-flyable doctrines (uncached; see caller)."""
    out: list[dict] = []
    # One snapshot + one attributes read for the whole scan (both invariant per
    # character), threaded through so the per-doctrine estimate never re-queries them.
    snapshot = character.skill_snapshots.filter(is_latest=True).first()
    attrs = _character_attributes(character)
    doctrines = (
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .prefetch_related("fits__skill_requirements")
        .order_by("-priority", "name")
    )
    for doctrine in doctrines:
        readies = [character_readiness(character, fit, snapshot=snapshot) for fit in doctrine.fits.all()]
        if not readies:
            continue
        # Already flyable on some fit → not a "closest" goal.
        if any(r.status in ("viable", "optimal") for r in readies):
            continue
        # Only count doctrines with real, known requirements.
        if all(r.status == "unknown" for r in readies):
            continue
        seconds = estimate_seconds_to_doctrine(character, doctrine, snapshot=snapshot, attrs=attrs)
        if seconds <= 0:
            continue
        out.append(
            {"doctrine_id": doctrine.id, "doctrine": doctrine.name, "seconds": seconds}
        )
    out.sort(key=lambda d: d["seconds"])
    return out


def remaining_seconds(plan: SkillPlan) -> int:
    """Estimated training time left across not-yet-done steps."""
    # Iterate the prefetched ``steps`` and filter in Python so the dashboard's
    # ``prefetch_related('steps')`` cache is reused (a manager filter like
    # ``.exclude(...)`` bypasses it and issues one extra SELECT per plan).
    return sum(
        s.estimated_seconds or 0
        for s in plan.steps.all()
        if s.status != SkillPlanStep.Status.DONE
    )


@transaction.atomic
def reconcile_plans_from_snapshot(character, snapshot) -> int:
    """Auto-tick plan steps a pilot has finished training, on each skills import.

    Any not-yet-done ``SkillPlanStep`` whose target level the fresh snapshot now
    satisfies is marked DONE, and each affected plan's ``estimated_total_seconds`` is
    refreshed to the time still outstanding — so plans stay honest with zero manual
    bookkeeping and the remaining-time figure reflects reality. Returns the number of
    steps newly marked done. Idempotent: re-importing the same snapshot is a no-op.
    """
    if snapshot is None:
        return 0
    plans = list(SkillPlan.objects.filter(character=character).prefetch_related("steps"))
    newly_done = 0
    for plan in plans:
        changed = False
        for step in plan.steps.all():
            if (
                step.status != SkillPlanStep.Status.DONE
                and snapshot.trained_level(step.skill_type_id) >= step.target_level
            ):
                step.status = SkillPlanStep.Status.DONE
                step.save(update_fields=["status"])
                newly_done += 1
                changed = True
        # ``remaining_seconds`` iterates the same prefetched (and now-mutated) step
        # objects, so it sees the fresh DONE statuses without another query.
        remaining = remaining_seconds(plan)
        if changed or plan.estimated_total_seconds != remaining:
            plan.estimated_total_seconds = remaining
            plan.save(update_fields=["estimated_total_seconds"])
    return newly_done


def plan_remap_advice(plan: SkillPlan) -> dict | None:
    """A neural-remap suggestion for a plan's outstanding steps, or ``None``.

    Recomputes the SP still to train per not-done step and asks
    :func:`apps.skills.training.remap_advice` whether a remap would meaningfully help.
    Returns ``None`` when the pilot has no imported attributes, the plan is short, or the
    saving isn't worth a remap — so the UI only ever shows honest, actionable advice.
    """
    from .training import remap_advice

    character = plan.character
    attrs = _character_attributes(character)
    if attrs is None:
        return None
    steps = [s for s in plan.steps.all() if s.status != SkillPlanStep.Status.DONE]
    if not steps:
        return None
    snapshot = character.skill_snapshots.filter(is_latest=True).first()
    meta = _skill_meta({s.skill_type_id for s in steps})
    specs = []
    for s in steps:
        have = snapshot.trained_level(s.skill_type_id) if snapshot else 0
        rank, primary, secondary = meta.get(s.skill_type_id, (1, None, None))
        sp = sp_between_levels(rank, have, s.target_level)
        specs.append((sp, primary, secondary))
    return remap_advice(attrs, specs)


def export_plan_text(plan: SkillPlan) -> str:
    """EVE skill-planner clipboard format: one ``Skill Name <level>`` per line."""
    names = dict(
        SdeType.objects.filter(
            type_id__in=plan.steps.values_list("skill_type_id", flat=True)
        ).values_list("type_id", "name")
    )
    lines = []
    for step in plan.steps.all():
        name = names.get(step.skill_type_id, f"Skill {step.skill_type_id}")
        lines.append(f"{name} {step.target_level}")
    return "\n".join(lines)
