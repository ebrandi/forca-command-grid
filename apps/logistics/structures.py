"""Per-pilot ESI structure search for the freight location picker.

Player-owned (Upwell) structures aren't public SDE data — a character can only
discover the ones they personally have docking access to. We search via the
pilot's own ESI token (``esi-search.search_structures`` + ``esi-universe.read_structures``)
and resolve each id to a name + system. Best-effort throughout: a missing scope,
no token, or any ESI error yields an empty list, so the picker quietly falls back
to stations and systems.
"""
from __future__ import annotations

import logging

from django.core.cache import cache

from apps.sde.models import SdeSolarSystem
from apps.sso.models import EveCharacter
from apps.sso.token_service import NoValidToken, get_valid_access_token
from core.esi.client import ESIClient, ESIError

log = logging.getLogger("forca.logistics")

SEARCH_SCOPE = "esi-search.search_structures.v1"
READ_SCOPE = "esi-universe.read_structures.v1"
_REQUIRED = [SEARCH_SCOPE, READ_SCOPE]
_MAX = 8
_STRUCT_TTL = 24 * 3600


def has_structure_search(user) -> bool:
    """Whether the user has a character whose token can search structures."""
    return _token_for(user) is not None


def _token_for(user) -> tuple[int, str] | None:
    if not getattr(user, "is_authenticated", False):
        return None
    for char in EveCharacter.objects.filter(user=user):
        try:
            return char.character_id, get_valid_access_token(char, _REQUIRED)
        except NoValidToken:
            continue
    return None


def search_structures(user, query: str, limit: int = _MAX) -> list[dict]:
    """Structures the pilot can dock at, matching ``query`` (≥3 chars)."""
    query = (query or "").strip()
    if len(query) < 3:
        return []
    tok = _token_for(user)
    if not tok:
        return []
    char_id, access = tok
    client = ESIClient()
    try:
        resp = client.get(
            f"/characters/{char_id}/search/",
            token=access,
            params={"categories": "structure", "search": query, "strict": "false"},
            use_etag=False,
        )
    except ESIError as exc:
        log.info("structure search failed for %s: %s", char_id, exc)
        return []
    ids = (resp.data or {}).get("structure", []) or []
    out: list[dict] = []
    for structure_id in ids[:limit]:
        info = _resolve_structure(client, structure_id, access)
        if info:
            out.append(info)
    return out


def _resolve_structure(client: ESIClient, structure_id: int, access: str) -> dict | None:
    key = f"logi:struct:{structure_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    try:
        resp = client.get(f"/universe/structures/{structure_id}/", token=access, use_etag=False)
    except ESIError:
        cache.set(key, {}, 600)  # brief negative cache so one bad id isn't retried in a loop
        return None
    data = resp.data or {}
    name = data.get("name", "")
    system_id = data.get("solar_system_id")
    if not name or not system_id:
        cache.set(key, {}, 600)
        return None
    system_name = (
        SdeSolarSystem.objects.filter(system_id=system_id).values_list("name", flat=True).first()
        or ""
    )
    info = {
        "kind": "structure",
        "id": int(structure_id),
        "name": name,
        "system_id": int(system_id),
        "system_name": system_name,
    }
    cache.set(key, info, _STRUCT_TTL)
    return info
