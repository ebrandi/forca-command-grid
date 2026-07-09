"""Celery tasks for stockpile asset synchronisation (corp + personal)."""
from __future__ import annotations

import logging

from celery import shared_task

from .assets import import_character_assets, import_corporation_assets

log = logging.getLogger("forca.stockpile")


@shared_task(name="stockpile.sync_corp_assets")
def sync_corp_assets() -> str:
    """Refresh the ESI-sourced corp-asset stockpile via a Director token.

    The corp-assets endpoint is cached ~1h server-side, so hourly is the useful
    maximum; the beat schedule runs this every few hours. Degrades gracefully
    (logs and returns a status) when no Director has granted the scope.
    """
    result = import_corporation_assets()
    log.info("corp asset sync: %s — %s", result["status"], result.get("message", ""))
    return result["status"]


@shared_task(name="stockpile.sync_personal_assets")
def sync_personal_assets() -> int:
    """Refresh personal assets for every pilot who granted the asset scope.

    Personal assets are cached ~1h server-side like corp assets. Each pilot's
    own token is used; characters without the scope are skipped (no ESI call),
    so this is safe to run for the whole roster.
    """
    from apps.admin_audit.health import record_sync
    from apps.sso.models import EveCharacter

    synced = 0
    candidates = EveCharacter.objects.filter(tokens__revoked_at__isnull=True).distinct()
    for character in candidates:
        if import_character_assets(character)["status"] == "ok":
            synced += 1
    record_sync("personal_assets", pilots=synced)
    log.info("personal asset sync: %s pilot(s) refreshed", synced)
    return synced
