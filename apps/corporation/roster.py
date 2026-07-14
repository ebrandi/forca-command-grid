"""Corp roster via the Director member-tracking feed (ESI).

Mirrors ``/corporations/{id}/membertracking/`` into ``CorpMember`` so leadership
can see who is in the corp, where/when each pilot was last active, and whether
they have registered in Command Grid (linked a character + provided a token).
Needs a Director token with the member-tracking scope; degrades gracefully when
none is granted. ESI is only ever called from the background task / sync action.
"""
from __future__ import annotations

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _

from apps.sso.models import AuthToken, EveCharacter
from apps.sso.token_service import NoValidToken, get_valid_access_token
from core.esi.client import ESIClient, ESIError
from core.mixins import Source

from .models import CorpMember, EveName

MEMBERSHIP_SCOPE = "esi-corporations.read_corporation_membership.v1"
TRACK_MEMBERS_SCOPE = "esi-corporations.track_members.v1"
ROSTER_SCOPES = [MEMBERSHIP_SCOPE, TRACK_MEMBERS_SCOPE]


def _dt(value):
    if not value:
        return None
    if isinstance(value, str):
        return parse_datetime(value.replace("Z", "+00:00"))
    return value


def find_director_token_character(corp_id: int) -> EveCharacter | None:
    """A corp character whose stored token carries the member-tracking scope."""
    for character in EveCharacter.objects.filter(corporation_id=corp_id, is_corp_member=True):
        if character.tokens.filter(revoked_at__isnull=True).exists():
            try:
                get_valid_access_token(character, [TRACK_MEMBERS_SCOPE])
                return character
            except NoValidToken:
                continue
    return None


def import_corp_members() -> dict:
    """Refresh the corp roster from the member-tracking endpoint."""
    corp_id = settings.FORCA_HOME_CORP_ID
    if not corp_id:
        return {
            "status": "no_corp",
            "message": _("Home corporation id not configured."),
            "count": 0,
        }

    director = find_director_token_character(corp_id)
    if director is None:
        return {
            "status": "no_token",
            "message": _("No Director has granted member-tracking access."),
            "count": 0,
        }
    try:
        token = get_valid_access_token(director, [TRACK_MEMBERS_SCOPE])
    except NoValidToken:
        return {"status": "no_token", "message": _("Director token unavailable."), "count": 0}

    client = ESIClient()
    try:
        resp = client.get(f"/corporations/{corp_id}/membertracking/", token=token)
    except ESIError as exc:
        return {"status": "error", "message": str(exc), "count": 0}

    now = timezone.now()
    seen: set[int] = set()
    for row in resp.data or []:
        cid = row.get("character_id")
        if not cid:
            continue
        seen.add(cid)
        CorpMember.objects.update_or_create(
            character_id=cid,
            defaults={
                "corporation_id": corp_id,
                "location_id": row.get("location_id"),
                "ship_type_id": row.get("ship_type_id"),
                "base_id": row.get("base_id"),
                "start_date": _dt(row.get("start_date")),
                "logon_date": _dt(row.get("logon_date")),
                "logoff_date": _dt(row.get("logoff_date")),
                "source": Source.ESI_CORP,
                "as_of": now,
                "fetched_at": now,
            },
        )
    # Members no longer in the corp drop off the roster.
    CorpMember.objects.filter(corporation_id=corp_id).exclude(character_id__in=seen).delete()
    _resolve_names(seen)

    from apps.admin_audit.health import record_sync

    record_sync("corp_members", members=len(seen), by=director.name)
    return {
        "status": "ok",
        "message": _("%(count)s members synced.") % {"count": len(seen)},
        "count": len(seen),
    }


def _resolve_names(character_ids) -> None:
    """Best-effort character + location name resolution via /universe/names/.

    Character ids and location ids are resolved in separate passes: locations can
    be player structures that /universe/names/ refuses, and we must never let that
    block pilot-name resolution (the cause of members showing as raw ids).
    """
    from core.esi.names import resolve_ids

    members = list(CorpMember.objects.filter(character_id__in=character_ids))
    cids = [m.character_id for m in members]
    location_ids = {m.location_id for m in members if m.location_id}
    try:
        resolve_ids(cids)
        if location_ids:
            resolve_ids(location_ids)
    except (requests.RequestException, ESIError):
        pass  # names stay best-effort; the roster is still useful without them

    resolved = dict(EveName.objects.filter(entity_id__in=cids).values_list("entity_id", "name"))
    linked = dict(
        EveCharacter.objects.filter(character_id__in=cids).values_list("character_id", "name")
    )
    for m in members:
        name = linked.get(m.character_id) or resolved.get(m.character_id)
        if name and name != m.name:
            m.name = name
            m.save(update_fields=["name"])


def pending_registration_count() -> int:
    """Corp members who have not registered (linked + tokened) — cheap count."""
    cids = list(CorpMember.objects.values_list("character_id", flat=True))
    if not cids:
        return 0
    registered = set(
        AuthToken.objects.filter(
            character__character_id__in=cids,
            character__user_id__isnull=False,
            revoked_at__isnull=True,
        ).values_list("character__character_id", flat=True)
    )
    return len(cids) - len(registered)


def roster() -> dict:
    """Display rows for the officer roster page, plus registration counts.

    Registered = the pilot has linked this character to a Command Grid account
    *and* has a non-revoked ESI token. Pending pilots (who leadership needs to
    chase) are listed first.
    """
    from apps.sde.models import SdeType

    members = list(CorpMember.objects.all())
    cids = [m.character_id for m in members]

    linked = {c.character_id: c for c in EveCharacter.objects.filter(character_id__in=cids)}
    with_token = set(
        AuthToken.objects.filter(
            character__character_id__in=cids, revoked_at__isnull=True
        ).values_list("character__character_id", flat=True)
    )
    loc_names = dict(
        EveName.objects.filter(
            entity_id__in=[m.location_id for m in members if m.location_id]
        ).values_list("entity_id", "name")
    )
    ships = dict(
        SdeType.objects.filter(
            type_id__in=[m.ship_type_id for m in members if m.ship_type_id]
        ).values_list("type_id", "name")
    )

    rows = []
    registered = 0
    for m in members:
        char = linked.get(m.character_id)
        is_linked = bool(char and char.user_id)
        is_registered = is_linked and m.character_id in with_token
        if is_registered:
            registered += 1
        rows.append(
            {
                "character_id": m.character_id,
                "name": m.name or (char.name if char else str(m.character_id)),
                "ship_type_id": m.ship_type_id,
                "ship": ships.get(m.ship_type_id) if m.ship_type_id else None,
                "location_id": m.location_id,
                "location": loc_names.get(m.location_id) if m.location_id else None,
                "last_login": m.logon_date,
                "last_seen": m.logoff_date,
                "registered": is_registered,
                # Linked but token gone → needs to re-authorise (still "to chase").
                "linked_no_token": is_linked and not is_registered,
            }
        )
    # Pending pilots first (the ones to contact), then alphabetical.
    rows.sort(key=lambda r: (r["registered"], r["name"].lower()))
    return {
        "rows": rows,
        "total": len(rows),
        "registered": registered,
        "pending": len(rows) - registered,
    }
