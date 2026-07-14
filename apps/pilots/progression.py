"""Award contribution credit for skill/doctrine *progression*.

Called from the skill-import path with the pilot's previous and new skill
snapshots. Two awards:

* **train** — when a pilot trains a skill the app recommends (a doctrine skill
  requirement) up to the recommended level.
* **doctrine** — when a pilot becomes able to fly a doctrine ship they couldn't
  before, with points scaled by the doctrine's corp priority and how much SP it
  takes (``ContributionWeights``).

Both are idempotent, so re-imports never double-credit. Awards are deltas only:
the first import for a character establishes the baseline (no retroactive credit
for everything they could already fly).
"""
from __future__ import annotations

from apps.doctrines.models import Doctrine, SkillRequirement
from apps.doctrines.services import (
    _level_in,
    doctrine_required_sp,
    flyable_doctrine_ids,
)
from apps.sde.models import SdeType

from .services import record_contribution
from .weights import points_for


def recommended_skill_levels() -> dict[int, int]:
    """skill_type_id → the highest level any active doctrine fit asks for.

    This is exactly what readiness nags pilots to train, so reaching one of these
    levels is "training a recommended skill".
    """
    out: dict[int, int] = {}
    rows = SkillRequirement.objects.filter(
        fit__doctrine__status=Doctrine.Status.ACTIVE
    ).values_list("skill_type_id", "min_level")
    for sid, level in rows:
        out[sid] = max(out.get(sid, 0), level)
    return out


def award_progression(character, prev_skills: dict | None, new_snapshot) -> None:
    """Credit training + doctrine unlocks between ``prev_skills`` and the new
    snapshot. No-op on the first import (``prev_skills`` is None) — that just sets
    the baseline."""
    user = getattr(character, "user", None)
    if user is None or prev_skills is None:
        return
    new_skills = new_snapshot.skills or {}

    # --- 1. Recommended skills newly trained to their target level ---
    fired: list[tuple[int, int]] = []
    for sid, target in recommended_skill_levels().items():
        if _level_in(prev_skills, sid) < target <= _level_in(new_skills, sid):
            fired.append((sid, target))
    if fired:
        names = dict(
            SdeType.objects.filter(type_id__in=[s for s, _ in fired])
            .values_list("type_id", "name")
        )
        train_points = points_for("train", magnitude=1)
        for sid, target in fired:
            record_contribution(
                user, "train", magnitude=1, unit="levels",
                # Skill + level only; "Trained" is the kind, rendered translated by the ledger.
                description=f"{names.get(sid, sid)} {target}",
                ref_type="skill", ref_id=f"{character.character_id}:{sid}:{target}",
                points=train_points,
            )

    # --- 2. Doctrine ships newly unlocked ---
    newly = flyable_doctrine_ids(new_skills) - flyable_doctrine_ids(prev_skills)
    if newly:
        for doctrine in Doctrine.objects.filter(id__in=newly):
            pts = points_for(
                "doctrine",
                doctrine_priority=doctrine.priority,
                required_sp=doctrine_required_sp(doctrine),
            )
            record_contribution(
                user, "doctrine", magnitude=1, unit="doctrines",
                # The doctrine's name; "Unlocked" is the kind, rendered translated by the ledger.
                description=doctrine.name,
                ref_type="doctrine", ref_id=f"{user.id}:{doctrine.id}",
                points=pts,
            )
