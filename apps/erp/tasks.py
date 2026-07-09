"""Background sync of the corp's owned blueprints + running industry jobs (ESI)."""
from __future__ import annotations

import logging

from celery import shared_task

log = logging.getLogger("forca.erp")


@shared_task(name="erp.sync_blueprints")
def sync_blueprints() -> str:
    """Refresh the corp's owned blueprints. No-op until a Director grants the scope."""
    from .esi_import import sync_corp_blueprints

    result = sync_corp_blueprints()
    log.info("corp blueprint sync: %s — %s blueprints", result["status"], result.get("blueprints", 0))
    return result["status"]


@shared_task(name="erp.sync_industry_jobs")
def sync_industry_jobs() -> str:
    """Refresh the corp's industry jobs. No-op until a Director grants the scope."""
    from .esi_import import sync_corp_industry_jobs

    result = sync_corp_industry_jobs()
    log.info("corp industry-job sync: %s — %s jobs", result["status"], result.get("jobs", 0))
    return result["status"]


@shared_task(name="erp.sync_character_industry")
def sync_character_industry() -> str:
    """Import personal jobs + blueprints for every pilot who granted `my_industry`."""
    from .esi_import import sync_all_character_industry

    result = sync_all_character_industry()
    log.info(
        "character industry sync: %s chars, %s jobs, %s blueprints",
        result.get("characters", 0), result.get("jobs", 0), result.get("blueprints", 0),
    )
    return result["status"]
