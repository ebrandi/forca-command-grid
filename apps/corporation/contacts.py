"""Corp standings/contacts sync (ESI corporation contacts).

Pulls the corporation's contact list (blue/red standings) from a role-holder token,
resolves names via EveName, and stores them for a member-facing standings page — the
"who's friendly / who's a target" reference, kept in sync rather than maintained by hand.
"""
from __future__ import annotations

from django.conf import settings

CONTACTS_SCOPE = "esi-corporations.read_contacts.v1"


def _token_character(corp_id: int):
    from apps.sso.models import EveCharacter
    from apps.sso.token_service import NoValidToken, get_valid_access_token

    for character in EveCharacter.objects.filter(is_corp_member=True):
        try:
            if get_valid_access_token(character, [CONTACTS_SCOPE]):
                return character
        except NoValidToken:
            continue
    return None


def sync_corp_contacts(corp_id: int | None = None, client=None) -> dict:
    """Replace the stored contact list with the corp's current ESI contacts."""
    from core.esi.client import ESIClient, ESIError

    from .models import Contact, EveName

    corp_id = corp_id or getattr(settings, "FORCA_HOME_CORP_ID", 0)
    character = _token_character(corp_id)
    if character is None:
        return {"status": "no_token", "count": 0}

    from apps.sso.token_service import get_valid_access_token

    token = get_valid_access_token(character, [CONTACTS_SCOPE])
    client = client or ESIClient()
    try:
        rows = client.get(f"/corporations/{corp_id}/contacts/", token=token).data or []
    except ESIError:
        return {"status": "error", "count": 0}

    seen_ids = []
    for r in rows:
        cid = r.get("contact_id")
        if not cid:
            continue
        seen_ids.append(cid)
        Contact.objects.update_or_create(
            contact_id=cid,
            defaults={
                "contact_type": r.get("contact_type", "character"),
                "standing": r.get("standing", 0.0) or 0.0,
            },
        )
    # Drop contacts that are no longer on the corp list.
    Contact.objects.exclude(contact_id__in=seen_ids).delete()

    # Resolve any missing names (best-effort) and copy them onto the rows.
    try:
        from core.esi.names import resolve_ids
        resolve_ids(seen_ids)
    except Exception:  # noqa: BLE001,S110 - name resolution is best-effort
        pass
    names = dict(EveName.objects.filter(entity_id__in=seen_ids).values_list("entity_id", "name"))
    for cid, name in names.items():
        Contact.objects.filter(contact_id=cid).update(name=name)

    return {"status": "ok", "count": len(seen_ids)}
