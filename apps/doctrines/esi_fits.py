"""Import a director's saved ship fittings from ESI.

ESI exposes **character** fittings only (``GET /characters/{id}/fittings/``,
scope ``esi-fittings.read_fittings.v1``) — there is no corporation-fittings
endpoint — so corp doctrines are seeded from a director's own saved fits. Each
fitting carries its ship and item list; we resolve type names from the SDE and
hand back a normalised structure the doctrine importer can turn into fits.
"""
from __future__ import annotations

from apps.sde.models import SdeType
from apps.sso.models import EveCharacter
from apps.sso.token_service import NoValidToken, get_valid_access_token
from core.esi.client import ESIClient, ESIError

FITTINGS_SCOPE = "esi-fittings.read_fittings.v1"


def characters_with_fittings_scope(user) -> list[EveCharacter]:
    """The user's linked characters whose stored token can read saved fittings."""
    out = []
    for character in user.characters.all():
        if not character.tokens.filter(revoked_at__isnull=True).exists():
            continue
        try:
            get_valid_access_token(character, [FITTINGS_SCOPE])
        except NoValidToken:
            continue
        out.append(character)
    return out


def _resolve_names(type_ids: set[int]) -> dict[int, str]:
    return dict(
        SdeType.objects.filter(type_id__in=type_ids).values_list("type_id", "name")
    )


def fetch_character_fittings(
    character: EveCharacter, client: ESIClient | None = None
) -> list[dict]:
    """Saved fittings for one character, normalised with resolved names.

    Returns ``[{fitting_id, name, ship_type_id, ship_name, modules, item_count,
    character}]``. ``modules`` is the doctrine format ``[{type_id, quantity,
    name}]`` aggregated across slots/cargo (slot layout isn't needed for a
    doctrine — only the bill of materials). Returns ``[]`` on a token/ESI error so
    one bad character doesn't sink the whole import.
    """
    client = client or ESIClient()
    try:
        access = get_valid_access_token(character, [FITTINGS_SCOPE])
        resp = client.get(f"/characters/{character.character_id}/fittings/", token=access)
    except (NoValidToken, ESIError):
        return []
    raw = resp.data or []

    type_ids: set[int] = set()
    for fitting in raw:
        type_ids.add(fitting.get("ship_type_id"))
        for item in fitting.get("items", []) or []:
            type_ids.add(item.get("type_id"))
    type_ids.discard(None)
    names = _resolve_names(type_ids)

    out: list[dict] = []
    for fitting in raw:
        ship_type_id = fitting.get("ship_type_id")
        if not ship_type_id:
            continue
        agg: dict[int, int] = {}
        for item in fitting.get("items", []) or []:
            tid = item.get("type_id")
            if not tid:
                continue
            agg[tid] = agg.get(tid, 0) + int(item.get("quantity", 1) or 1)
        modules = [
            {"type_id": tid, "quantity": qty, "name": names.get(tid, f"TypeID:{tid}")}
            for tid, qty in agg.items()
        ]
        out.append({
            "fitting_id": fitting.get("fitting_id"),
            "name": (fitting.get("name") or "").strip() or names.get(ship_type_id, "Fit"),
            "ship_type_id": ship_type_id,
            "ship_name": names.get(ship_type_id, f"TypeID:{ship_type_id}"),
            "modules": modules,
            "item_count": sum(m["quantity"] for m in modules),
            "character_id": character.character_id,
            "character_name": character.name,
        })
    return out


def fetch_all_fittings(user, client: ESIClient | None = None) -> list[dict]:
    """Saved fittings across every one of the user's characters that granted the
    fittings scope, each tagged with its source character."""
    client = client or ESIClient()
    fits: list[dict] = []
    for character in characters_with_fittings_scope(user):
        fits.extend(fetch_character_fittings(character, client))
    return fits
