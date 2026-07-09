"""Corp structure monitoring sync (ESI corporation structures).

Pulls every Upwell structure the corp owns from a Station-Manager/Director token —
fuel timer, state and reinforcement window — for the director structures board.
Structure *names* aren't on the corp endpoint, so we resolve them best-effort via
``/universe/structures/{id}/`` (needs docking access); type/system names come from
the SDE we already load. Reads ESI, writes our tables — only from a Celery task or
an explicit officer "sync now".
"""
from __future__ import annotations

from django.conf import settings
from django.utils.dateparse import parse_datetime

STRUCTURES_SCOPE = "esi-corporations.read_structures.v1"
NAME_SCOPE = "esi-universe.read_structures.v1"


def _token_character(corp_id: int):
    from apps.sso.models import EveCharacter
    from apps.sso.token_service import NoValidToken, get_valid_access_token

    for character in EveCharacter.objects.filter(is_corp_member=True):
        try:
            if get_valid_access_token(character, [STRUCTURES_SCOPE]):
                return character
        except NoValidToken:
            continue
    return None


def _resolve_name(client, structure_id: int, token: str) -> str:
    """Best-effort structure name via the universe endpoint (needs docking access)."""
    from core.esi.client import ESIError

    try:
        data = client.get(f"/universe/structures/{structure_id}/", token=token).data or {}
    except ESIError:
        return ""
    return (data.get("name") or "")[:200]


def sync_corp_structures(corp_id: int | None = None, client=None) -> dict:
    """Snapshot the corp's structures (fuel/state/timers) into ``CorpStructure``."""
    from apps.sde.models import SdeSolarSystem, SdeType
    from core.esi.client import ESIClient, ESIError
    from core.mixins import Source

    from .models import CorpStructure

    corp_id = corp_id or getattr(settings, "FORCA_HOME_CORP_ID", 0)
    character = _token_character(corp_id)
    if character is None:
        return {"status": "no_token", "count": 0}

    from apps.sso.token_service import NoValidToken, get_valid_access_token

    token = get_valid_access_token(character, [STRUCTURES_SCOPE])
    client = client or ESIClient()
    try:
        rows = client.get_paged(f"/corporations/{corp_id}/structures/", token=token)
    except ESIError:
        return {"status": "error", "count": 0}

    # A name token (universe read + docking) is optional — resolve names if present.
    try:
        name_token = get_valid_access_token(character, [NAME_SCOPE])
    except NoValidToken:
        name_token = None

    type_ids = {r.get("type_id") for r in rows if r.get("type_id")}
    system_ids = {r.get("system_id") for r in rows if r.get("system_id")}
    type_names = dict(SdeType.objects.filter(type_id__in=type_ids).values_list("type_id", "name"))
    system_names = dict(
        SdeSolarSystem.objects.filter(system_id__in=system_ids).values_list("system_id", "name")
    )

    seen = set()
    for r in rows:
        sid = r.get("structure_id")
        if not sid:
            continue
        seen.add(sid)
        existing = CorpStructure.objects.filter(structure_id=sid).first()
        # Keep a previously-resolved name; only call ESI for names we don't have.
        name = existing.name if existing and existing.name else ""
        if not name and name_token:
            name = _resolve_name(client, sid, name_token)
        CorpStructure.objects.update_or_create(
            structure_id=sid,
            defaults={
                "name": name,
                "type_id": r.get("type_id") or 0,
                "type_name": type_names.get(r.get("type_id"), ""),
                "system_id": r.get("system_id"),
                "system_name": system_names.get(r.get("system_id"), ""),
                "state": (r.get("state") or "")[:32],
                "fuel_expires": parse_datetime(r.get("fuel_expires") or "") or None,
                "state_timer_start": parse_datetime(r.get("state_timer_start") or "") or None,
                "state_timer_end": parse_datetime(r.get("state_timer_end") or "") or None,
                "unanchors_at": parse_datetime(r.get("unanchors_at") or "") or None,
                "reinforce_hour": r.get("reinforce_hour"),
                "services": r.get("services") or [],
                "source": Source.ESI_CORP,
            },
        )

    # Prune structures the corp no longer owns (transferred / destroyed).
    CorpStructure.objects.exclude(structure_id__in=seen).delete()
    timers = import_reinforcement_timers()
    return {"status": "ok", "count": len(seen), "timers": timers}


# ESI reinforcement states → our timer board's timer type.
_REINFORCE_TIMER = {
    "armor_reinforce": "armor",
    "hull_reinforce": "hull",
}


def import_reinforcement_timers() -> int:
    """Mirror reinforced structures onto the manual timer board (idempotent).

    Bridges structure monitoring into the existing structure-timer board so a
    reinforcement the corp owns shows up with a countdown automatically, without
    an officer hand-entering it. Friendly side (it's our structure); keyed on the
    structure + exit time so re-syncs don't duplicate, and a moved timer updates.
    """
    from apps.operations.models import StructureTimer

    from .models import CorpStructure

    created = 0
    live_ids = set()
    for s in CorpStructure.objects.filter(state__in=_REINFORCE_TIMER):
        if not (s.is_reinforced and s.state_timer_end):
            continue
        label = s.name or f"Structure {s.structure_id}"
        timer, was_created = StructureTimer.objects.update_or_create(
            name=label, exits_at=s.state_timer_end,
            defaults={
                "system_name": s.system_name,
                "system_id": s.system_id,
                "structure_type": s.type_name[:80],
                "timer_type": _REINFORCE_TIMER[s.state],
                "side": StructureTimer.Side.FRIENDLY,
                "notes": StructureTimer.AUTO_IMPORT_NOTE,
            },
        )
        live_ids.add(timer.pk)
        created += int(was_created)

    # Drop stale auto-imported timers whose structure is no longer reinforced.
    StructureTimer.objects.filter(
        notes=StructureTimer.AUTO_IMPORT_NOTE,
    ).exclude(pk__in=live_ids).delete()
    return created
