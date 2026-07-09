"""Import the corp's owned blueprints + running industry jobs from ESI.

Mirrors the corp-finance/roster sync pattern: find a home-corp character whose
token carries the right Director scope, pull the (paginated) corp endpoint, and
snapshot-replace our rows. Reads ESI, writes our tables — only ever called from a
Celery task or an explicit officer "sync now", never from a web request.

Both syncs are no-ops (``status="no_token"``) until a Director grants the
``corp_industry`` feature, exactly like the other corp syncs.
"""
from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils.dateparse import parse_datetime

BLUEPRINTS_SCOPE = "esi-corporations.read_blueprints.v1"
JOBS_SCOPE = "esi-industry.read_corporation_jobs.v1"
CHAR_JOBS_SCOPE = "esi-industry.read_character_jobs.v1"
CHAR_BLUEPRINTS_SCOPE = "esi-characters.read_blueprints.v1"


def _token_character(scope: str):
    from apps.sso.models import EveCharacter
    from apps.sso.token_service import NoValidToken, get_valid_access_token

    for character in EveCharacter.objects.filter(is_corp_member=True):
        try:
            if get_valid_access_token(character, [scope]):
                return character
        except NoValidToken:
            continue
    return None


def sync_corp_blueprints(corp_id: int | None = None, client=None) -> dict:
    """Snapshot the corp's owned blueprints into ``erp.Blueprint`` (source='esi')."""
    from apps.industry.bom import product_for
    from core.esi.client import ESIClient, ESIError

    from .models import Blueprint

    corp_id = corp_id or getattr(settings, "FORCA_HOME_CORP_ID", 0)
    character = _token_character(BLUEPRINTS_SCOPE)
    if character is None:
        return {"status": "no_token", "blueprints": 0}

    from apps.sso.token_service import get_valid_access_token

    token = get_valid_access_token(character, [BLUEPRINTS_SCOPE])
    client = client or ESIClient()
    try:
        rows = client.get_paged(f"/corporations/{corp_id}/blueprints/", token=token)
    except ESIError:
        return {"status": "error", "blueprints": 0}

    objs = []
    for r in rows:
        type_id = r.get("type_id")
        if not type_id:
            continue
        objs.append(Blueprint(
            owner_type=Blueprint.Owner.CORPORATION, owner_id=corp_id, type_id=type_id,
            product_type_id=product_for(type_id),
            me=max(0, int(r.get("material_efficiency") or 0)),
            te=max(0, int(r.get("time_efficiency") or 0)),
            quantity=int(r.get("quantity", -1)), runs=int(r.get("runs", -1)),
            item_id=r.get("item_id"), location_id=r.get("location_id"), source="esi",
        ))

    # Snapshot replace: the ESI endpoint returns the full owned set, so the corp's
    # esi-sourced rows are the truth. Manual rows (source!='esi') are left intact.
    with transaction.atomic():
        Blueprint.objects.filter(
            owner_type=Blueprint.Owner.CORPORATION, owner_id=corp_id, source="esi"
        ).delete()
        Blueprint.objects.bulk_create(objs)
    return {"status": "ok", "blueprints": len(objs)}


def sync_corp_industry_jobs(corp_id: int | None = None, client=None) -> dict:
    """Snapshot the corp's industry jobs (incl. recently completed) into ``CorpIndustryJob``."""
    from core.esi.client import ESIClient, ESIError

    from .models import CorpIndustryJob

    corp_id = corp_id or getattr(settings, "FORCA_HOME_CORP_ID", 0)
    character = _token_character(JOBS_SCOPE)
    if character is None:
        return {"status": "no_token", "jobs": 0}

    from apps.sso.token_service import get_valid_access_token

    token = get_valid_access_token(character, [JOBS_SCOPE])
    client = client or ESIClient()
    try:
        rows = client.get_paged(
            f"/corporations/{corp_id}/industry/jobs/", token=token,
            params={"include_completed": "true"},
        )
    except ESIError:
        return {"status": "error", "jobs": 0}

    objs = []
    for r in rows:
        jid = r.get("job_id")
        if not jid:
            continue
        objs.append(CorpIndustryJob(
            job_id=jid, installer_id=r.get("installer_id") or 0,
            activity_id=int(r.get("activity_id") or 1),
            blueprint_type_id=r.get("blueprint_type_id") or 0,
            product_type_id=r.get("product_type_id"),
            runs=int(r.get("runs") or 1), status=(r.get("status") or "active")[:12],
            facility_id=r.get("facility_id"), location_id=r.get("location_id"),
            start_date=parse_datetime(r.get("start_date") or "") or None,
            end_date=parse_datetime(r.get("end_date") or "") or None,
        ))

    with transaction.atomic():
        CorpIndustryJob.objects.all().delete()
        CorpIndustryJob.objects.bulk_create(objs)
    return {"status": "ok", "jobs": len(objs)}


# --------------------------------------------------------------------------- #
# Per-pilot (character) imports — opt-in ``my_industry`` scope, pilot's own token.
# --------------------------------------------------------------------------- #
def _char_token(character, scope: str) -> str | None:
    from apps.sso.token_service import NoValidToken, get_valid_access_token

    try:
        return get_valid_access_token(character, [scope])
    except NoValidToken:
        return None


def sync_character_industry_jobs(character, client=None) -> dict:
    """Snapshot one pilot's own industry jobs into ``CharacterIndustryJob``.

    Uses the pilot's own token (``my_industry`` scope). No-op (``no_token``) if the
    pilot hasn't granted it. Snapshot-replaces only this character's rows.
    """
    from core.esi.client import ESIClient, ESIError

    from .models import CharacterIndustryJob

    token = _char_token(character, CHAR_JOBS_SCOPE)
    if token is None:
        return {"status": "no_token", "jobs": 0}
    client = client or ESIClient()
    try:
        rows = client.get_paged(
            f"/characters/{character.character_id}/industry/jobs/", token=token,
            params={"include_completed": "true"},
        )
    except ESIError:
        return {"status": "error", "jobs": 0}

    objs = []
    for r in rows:
        jid = r.get("job_id")
        if not jid:
            continue
        objs.append(CharacterIndustryJob(
            character_id=character.character_id, job_id=jid,
            activity_id=int(r.get("activity_id") or 1),
            blueprint_type_id=r.get("blueprint_type_id") or 0,
            product_type_id=r.get("product_type_id"),
            runs=int(r.get("runs") or 1), status=(r.get("status") or "active")[:12],
            cost=r.get("cost"),
            facility_id=r.get("facility_id"),
            location_id=r.get("station_id") or r.get("location_id"),
            start_date=parse_datetime(r.get("start_date") or "") or None,
            end_date=parse_datetime(r.get("end_date") or "") or None,
        ))
    with transaction.atomic():
        CharacterIndustryJob.objects.filter(character_id=character.character_id).delete()
        CharacterIndustryJob.objects.bulk_create(objs)
    return {"status": "ok", "jobs": len(objs)}


def sync_character_blueprints(character, client=None) -> dict:
    """Snapshot one pilot's owned blueprints into ``erp.Blueprint`` (owner=character)."""
    from apps.industry.bom import product_for
    from core.esi.client import ESIClient, ESIError

    from .models import Blueprint

    token = _char_token(character, CHAR_BLUEPRINTS_SCOPE)
    if token is None:
        return {"status": "no_token", "blueprints": 0}
    client = client or ESIClient()
    try:
        rows = client.get_paged(f"/characters/{character.character_id}/blueprints/", token=token)
    except ESIError:
        return {"status": "error", "blueprints": 0}

    objs = []
    for r in rows:
        type_id = r.get("type_id")
        if not type_id:
            continue
        objs.append(Blueprint(
            owner_type=Blueprint.Owner.CHARACTER, owner_id=character.character_id,
            type_id=type_id, product_type_id=product_for(type_id),
            me=max(0, int(r.get("material_efficiency") or 0)),
            te=max(0, int(r.get("time_efficiency") or 0)),
            quantity=int(r.get("quantity", -1)), runs=int(r.get("runs", -1)),
            item_id=r.get("item_id"), location_id=r.get("location_id"), source="esi",
        ))
    with transaction.atomic():
        Blueprint.objects.filter(
            owner_type=Blueprint.Owner.CHARACTER, owner_id=character.character_id, source="esi"
        ).delete()
        Blueprint.objects.bulk_create(objs)
    return {"status": "ok", "blueprints": len(objs)}


def sync_all_character_industry(client=None) -> dict:
    """Import personal jobs + blueprints for every pilot who granted ``my_industry``.

    Iterates linked characters and imports for those whose token carries the scope;
    others are skipped silently. Returns per-character counts.
    """
    from apps.sso.models import EveCharacter

    client = client or None
    synced, jobs, blueprints = 0, 0, 0
    for character in EveCharacter.objects.all():
        jr = sync_character_industry_jobs(character, client=client)
        if jr["status"] == "no_token":
            continue
        synced += 1
        jobs += jr.get("jobs", 0)
        br = sync_character_blueprints(character, client=client)
        blueprints += br.get("blueprints", 0)
    return {"status": "ok", "characters": synced, "jobs": jobs, "blueprints": blueprints}
