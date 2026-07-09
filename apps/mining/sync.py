"""Sync the corp mining ledger from ESI (corporation mining observers).

Each refinery (observer) records who mined what ore. We pull the observer list and each
observer's per-pilot ledger, resolving pilot names. Uses the same mining scope as moon
extractions. Reads ESI, writes our tables — not called from the request path except via
an explicit officer "sync now".
"""
from __future__ import annotations

from django.conf import settings
from django.utils.dateparse import parse_date

MINING_SCOPE = "esi-industry.read_corporation_mining.v1"


def _token_character(corp_id: int):
    from apps.sso.models import EveCharacter
    from apps.sso.token_service import NoValidToken, get_valid_access_token

    for character in EveCharacter.objects.filter(is_corp_member=True):
        try:
            if get_valid_access_token(character, [MINING_SCOPE]):
                return character
        except NoValidToken:
            continue
    return None


def _resolve_names(character_ids: set[int]) -> dict[int, str]:
    from apps.corporation.models import EveName
    from apps.sso.models import EveCharacter

    names = dict(
        EveCharacter.objects.filter(character_id__in=character_ids)
        .values_list("character_id", "name")
    )
    missing = [c for c in character_ids if c not in names or not names[c]]
    if missing:
        try:
            from core.esi.names import resolve_ids
            resolve_ids(missing)
        except Exception:  # noqa: BLE001,S110 - name resolution is best-effort
            pass
        for cid, name in EveName.objects.filter(entity_id__in=missing).values_list("entity_id", "name"):
            names.setdefault(cid, name)
    return names


def sync_mining_ledger(corp_id: int | None = None, client=None) -> dict:
    """Refresh observers and their per-pilot mining ledger."""
    from core.esi.client import ESIClient, ESIError

    from .models import MiningLedgerEntry, MiningObserver

    corp_id = corp_id or getattr(settings, "FORCA_HOME_CORP_ID", 0)
    character = _token_character(corp_id)
    if character is None:
        return {"status": "no_token", "entries": 0}

    from apps.sso.token_service import get_valid_access_token

    token = get_valid_access_token(character, [MINING_SCOPE])
    client = client or ESIClient()
    try:
        observers = client.get(f"/corporation/{corp_id}/mining/observers/", token=token).data or []
    except ESIError:
        return {"status": "error", "entries": 0}

    entries = 0
    all_rows: list[tuple[int, dict]] = []
    for obs in observers:
        oid = obs.get("observer_id")
        if not oid:
            continue
        MiningObserver.objects.update_or_create(
            observer_id=oid,
            defaults={
                "observer_type": obs.get("observer_type", ""),
                "last_updated": parse_date(obs.get("last_updated") or "") or None,
            },
        )
        try:
            rows = client.get(
                f"/corporation/{corp_id}/mining/observers/{oid}/", token=token,
            ).data or []
        except ESIError:
            continue
        for r in rows:
            all_rows.append((oid, r))

    names = _resolve_names({r.get("character_id") for _, r in all_rows if r.get("character_id")})
    for oid, r in all_rows:
        cid = r.get("character_id")
        day = parse_date(r.get("last_updated") or "")
        if not cid or day is None:
            continue
        MiningLedgerEntry.objects.update_or_create(
            observer_id=oid, character_id=cid, type_id=r.get("type_id", 0), day=day,
            defaults={"quantity": r.get("quantity", 0) or 0,
                      "character_name": names.get(cid, "")},
        )
        entries += 1
    return {"status": "ok", "observers": len(observers), "entries": entries}
