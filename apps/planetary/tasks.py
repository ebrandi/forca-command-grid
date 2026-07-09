"""Celery tasks for Planetary Industry: colony sync + plan re-costing.

All ESI work happens here (never in a web request). Both tasks degrade gracefully:
colony sync skips pilots without the opt-in scope; re-costing just reads prices.
"""
from __future__ import annotations

import logging

from celery import shared_task

log = logging.getLogger("forca.planetary")


@shared_task(name="planetary.sync_colonies")
def sync_colonies() -> int:
    """Refresh PI colonies for every pilot who granted the planets scope.

    ESI caches the planets list ~10 min server-side and only refreshes layout when
    the pilot opens the colony in the client, so a few times a day is plenty. Pilots
    without the scope are skipped with no ESI call, so it's safe for the whole roster.
    """
    from apps.sso.models import EveCharacter

    from .esi import import_colonies

    synced = 0
    candidates = EveCharacter.objects.filter(tokens__revoked_at__isnull=True).distinct()
    for character in candidates:
        if import_colonies(character)["status"] == "ok":
            synced += 1
    log.info("PI colony sync: %s pilot(s) refreshed", synced)
    return synced


@shared_task(name="planetary.sync_character_colonies")
def sync_character_colonies(character_id: int) -> str:
    """Import one character's colonies on demand (enqueued from the colonies page)."""
    from apps.sso.models import EveCharacter

    from .esi import import_colonies

    character = EveCharacter.objects.filter(character_id=character_id).first()
    if character is None:
        return "no_character"
    result = import_colonies(character)
    log.info("PI colony sync for %s: %s", character_id, result["status"])
    return result["status"]


@shared_task(name="planetary.recost_active_plans")
def recost_active_plans() -> int:
    """Re-cost active plans from current prices so cards show fresh numbers."""
    from .models import PiPlan, PiStatus
    from .services import recompute

    n = 0
    for plan in PiPlan.objects.filter(status=PiStatus.ACTIVE).prefetch_related("planets"):
        try:
            recompute(plan)
            n += 1
        except Exception:  # noqa: BLE001 - one bad plan must not stop the batch
            log.exception("failed to recost PI plan %s", plan.pk)
    log.info("PI recost: %s active plan(s)", n)
    return n
