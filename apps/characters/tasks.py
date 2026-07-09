"""Celery tasks for character data sync."""
from __future__ import annotations

import logging

from celery import shared_task

from apps.sso.models import EveCharacter

from . import services

log = logging.getLogger("forca.characters")


@shared_task(name="characters.sync_skills")
def sync_character_skills(character_id: int) -> None:
    character = EveCharacter.objects.filter(character_id=character_id).first()
    if character:
        services.import_character_skills(character)
        # Attributes share the skills scope and drive attribute-aware training ETAs;
        # a failure here must never fail the (primary) skills import.
        try:
            services.import_character_attributes(character)
        except Exception as exc:  # noqa: BLE001 - attributes are advisory, never fatal
            log.warning("attributes sync skipped for %s: %s", character.character_id, exc)


@shared_task(name="characters.sync_skillqueue")
def sync_character_skillqueue(character_id: int) -> None:
    character = EveCharacter.objects.filter(character_id=character_id).first()
    if character:
        services.import_character_skillqueue(character)


@shared_task(name="characters.sync_all_member_skills")
def sync_all_member_skills() -> int:
    """Refresh skills + skill-queue for every corp member with a token (best-effort).

    The skill-queue powers the "My Skills & Training" display; refreshing it on the
    same corp-wide beat keeps the queue from going stale until a pilot next logs in.
    """
    count = 0
    for character in EveCharacter.objects.filter(is_corp_member=True):
        try:
            services.import_character_skills(character)
            count += 1
        except Exception as exc:  # noqa: BLE001 - one bad token must not stop the batch
            log.warning("skill sync skipped for %s: %s", character.character_id, exc)
            continue
        try:
            services.import_character_skillqueue(character)
        except Exception as exc:  # noqa: BLE001 - queue scope is optional; never fatal
            log.warning("skillqueue sync skipped for %s: %s", character.character_id, exc)
        try:
            services.import_character_attributes(character)
        except Exception as exc:  # noqa: BLE001 - attributes are advisory, never fatal
            log.warning("attributes sync skipped for %s: %s", character.character_id, exc)
    return count
