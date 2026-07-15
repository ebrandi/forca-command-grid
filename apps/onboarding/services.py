"""Onboarding services: milestone auto-detection and 'what to do today'."""
from __future__ import annotations

from django.utils import timezone

from .models import OnboardingMilestone, OnboardingProgress

# Criteria types the engine can verify on its own. Anything else (including an
# empty dict) is a MANUAL milestone: the pilot checks it off on the page.
AUTO_TYPES = {
    "linked", "corp_member", "skills_imported", "skill_min",
    "doctrine_ready", "doctrine_any", "scopes",
}


def is_manual(criteria: dict | None) -> bool:
    """True when the milestone can't auto-complete — the pilot marks it done."""
    return (criteria or {}).get("type") not in AUTO_TYPES


def _criterion_met(character, criteria: dict) -> bool:
    """Evaluate a milestone's criteria against a character's current state.

    Supported criteria types (criteria is a small JSON rule):
      {"type": "linked"}                          -> a character is linked
      {"type": "corp_member"}                     -> verified FORCA member
      {"type": "skills_imported"}                 -> has a skill snapshot
      {"type": "skill_min", "skill_type_id", "level"}
      {"type": "doctrine_ready", "doctrine_id"}   -> can fly a doctrine (viable+)
      {"type": "doctrine_any"}                    -> can fly ANY active doctrine
      {"type": "scopes", "scopes": [...]}         -> a token covers these ESI scopes
    Unknown/empty types are manual (never auto-completed).
    """
    ctype = (criteria or {}).get("type")
    if ctype == "linked":
        return True  # having a character record means it's linked
    if ctype == "corp_member":
        return bool(character.is_corp_member)
    if ctype == "skills_imported":
        return character.skill_snapshots.filter(is_latest=True).exists()
    if ctype == "skill_min":
        snap = character.skill_snapshots.filter(is_latest=True).first()
        if not snap:
            return False
        return snap.trained_level(criteria["skill_type_id"]) >= criteria.get("level", 1)
    if ctype == "doctrine_ready":
        from apps.doctrines.models import Doctrine
        from apps.doctrines.services import character_readiness

        doctrine = Doctrine.objects.filter(pk=criteria["doctrine_id"]).prefetch_related("fits").first()
        if not doctrine:
            return False
        return any(
            character_readiness(character, fit).status in ("viable", "optimal")
            for fit in doctrine.fits.all()
        )
    if ctype == "doctrine_any":
        from apps.doctrines.services import flyable_doctrine_ids

        snap = character.skill_snapshots.filter(is_latest=True).first()
        if not snap:
            return False
        return bool(flyable_doctrine_ids(snap.skills))
    if ctype == "scopes":
        wanted = set(criteria.get("scopes") or [])
        if not wanted:
            return False
        return any(
            wanted <= set(token.scopes or [])
            for token in character.tokens.filter(revoked_at__isnull=True)
        )
    return False


def evaluate_milestones(character) -> list[dict]:
    """Recompute auto-detectable milestone progress for a character."""
    results: list[dict] = []
    for milestone in OnboardingMilestone.objects.filter(active=True):
        progress, _ = OnboardingProgress.objects.get_or_create(
            character=character, milestone=milestone
        )
        if progress.status != OnboardingProgress.Status.DONE and _criterion_met(
            character, milestone.criteria
        ):
            progress.status = OnboardingProgress.Status.DONE
            progress.completed_at = timezone.now()
            progress.auto_detected = True
            progress.save(update_fields=["status", "completed_at", "auto_detected"])
        results.append(
            {
                "milestone": milestone.title,
                "key": milestone.key,
                "status": progress.status,
                "category": milestone.category,
            }
        )
    return results


def next_actions(character, limit: int = 3) -> list[dict]:
    """The next few incomplete milestones — 'what should I do today?'."""
    evaluate_milestones(character)
    todo = (
        OnboardingProgress.objects.filter(character=character)
        .exclude(status=OnboardingProgress.Status.DONE)
        .select_related("milestone")
        .order_by("milestone__sort_order")[:limit]
    )
    return [
        {
            "title": p.milestone.title_i18n,
            "description": p.milestone.description_i18n,
            "key": p.milestone.key,
            "id": p.milestone_id,
            "url": p.milestone.url,
            "manual": is_manual(p.milestone.criteria),
        }
        for p in todo
    ]
