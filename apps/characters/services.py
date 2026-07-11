"""Character data services: import skills/skillqueue/attributes via ESI."""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.sso.models import EveCharacter
from apps.sso.token_service import get_valid_access_token
from core.esi.client import ESIClient
from core.mixins import Source

from .models import CharacterAttributes, CharacterSkillSnapshot, SkillQueueSnapshot

logger = logging.getLogger(__name__)


def import_character_skills(
    character: EveCharacter, client: ESIClient | None = None
) -> CharacterSkillSnapshot:
    """Fetch and store a fresh skills snapshot for a character."""
    client = client or ESIClient()
    access = get_valid_access_token(character, ["esi-skills.read_skills.v1"])
    resp = client.get(f"/characters/{character.character_id}/skills/", token=access)
    data = resp.data or {}

    skills: dict[str, dict] = {}
    for entry in data.get("skills", []):
        sid = str(entry.get("skill_id"))
        skills[sid] = {
            "trained_level": entry.get("trained_skill_level", 0),
            "active_level": entry.get("active_skill_level", 0),
            "sp": entry.get("skillpoints_in_skill", 0),
        }

    now = timezone.now()
    # Keep the previous snapshot's skills to detect what was newly trained.
    prev = character.skill_snapshots.filter(is_latest=True).first()
    prev_skills = prev.skills if prev else None
    character.skill_snapshots.filter(is_latest=True).update(is_latest=False)
    snapshot = CharacterSkillSnapshot.objects.create(
        character=character,
        skills=skills,
        total_sp=data.get("total_sp", 0),
        is_latest=True,
        source=Source.ESI_CHAR,
        as_of=now,
        fetched_at=now,
    )
    # The closest-doctrines card is derived from this snapshot and cached; a fresh
    # import (e.g. a pilot's first) must show through immediately, not after the TTL.
    from apps.skills.services import invalidate_closest_doctrines

    invalidate_closest_doctrines(character.character_id)
    # Auto-reconcile the pilot's skill plans against the fresh snapshot: a step whose
    # target level is now trained ticks to done and the plan's remaining time refreshes,
    # with no manual bookkeeping. A secondary side-effect — isolated so a reconcile
    # problem can never fail the import (the snapshot above is the source of truth).
    try:
        from apps.skills.services import reconcile_plans_from_snapshot

        reconcile_plans_from_snapshot(character, snapshot)
    except Exception:
        logger.exception("skill-plan reconcile failed for character %s", character.character_id)
    # Award training / doctrine-unlock contributions for the delta. This is a
    # secondary side-effect: a failure here must never fail the skill import (the
    # snapshot above is the source of truth), so it's isolated.
    try:
        from apps.pilots.progression import award_progression

        award_progression(character, prev_skills, snapshot)
    except Exception:
        logger.exception("award_progression failed for character %s", character.character_id)
    # Credit any Capsuleer Path skill/doctrine milestones the fresh snapshot now satisfies. A
    # secondary side-effect isolated exactly like the two above — a reconcile problem (or a disabled
    # feature) must never fail the skill import (the snapshot above is the source of truth).
    try:
        from apps.capsuleer.services import reconcile_from_snapshot

        reconcile_from_snapshot(character, snapshot)
    except Exception:
        logger.exception("capsuleer reconcile failed for character %s", character.character_id)
    return snapshot


def import_character_skillqueue(
    character: EveCharacter, client: ESIClient | None = None
) -> SkillQueueSnapshot:
    client = client or ESIClient()
    access = get_valid_access_token(character, ["esi-skills.read_skillqueue.v1"])
    resp = client.get(f"/characters/{character.character_id}/skillqueue/", token=access)
    entries = resp.data or []
    now = timezone.now()
    character.skillqueue_snapshots.filter(is_latest=True).update(is_latest=False)
    return SkillQueueSnapshot.objects.create(
        character=character,
        entries=entries,
        is_latest=True,
        source=Source.ESI_CHAR,
        as_of=now,
        fetched_at=now,
    )


def import_character_attributes(
    character: EveCharacter, client: ESIClient | None = None
) -> CharacterAttributes:
    client = client or ESIClient()
    access = get_valid_access_token(character, ["esi-skills.read_skills.v1"])
    resp = client.get(f"/characters/{character.character_id}/attributes/", token=access)
    data = resp.data or {}
    now = timezone.now()
    attrs, _ = CharacterAttributes.objects.update_or_create(
        character=character,
        defaults={
            "intelligence": data.get("intelligence", 20),
            "memory": data.get("memory", 20),
            "perception": data.get("perception", 20),
            "willpower": data.get("willpower", 20),
            "charisma": data.get("charisma", 19),
            "source": Source.ESI_CHAR,
            "as_of": now,
            "fetched_at": now,
        },
    )
    return attrs
