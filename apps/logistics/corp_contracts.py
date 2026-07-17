"""Snapshot all corp contracts for the oversight board (ESI corp contracts).

Distinct from the courier-verification path (which only cares about courier
contracts): this stores the full picture — item exchanges, auctions, loans and
couriers — so officers can see what's outstanding/in-progress/finished. Reuses
the corp-contracts director token; snapshot-replaced each sync.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.utils.dateparse import parse_datetime

from .contracts_esi import _director_contract_token


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - tolerate odd ESI values
        return Decimal("0")


def sync_corp_contracts(corp_id: int | None = None, client=None) -> dict:
    """Snapshot every corp contract into ``CorpContract`` (names resolved best-effort)."""
    from core.esi.client import ESIClient, ESIError

    from .models import CorpContract

    corp_id = corp_id or getattr(settings, "FORCA_HOME_CORP_ID", 0)
    token = _director_contract_token(corp_id)
    if token is None:
        return {"status": "no_token", "count": 0}

    client = client or ESIClient()
    try:
        rows = client.get_paged(f"/corporations/{corp_id}/contracts/", token=token)
    except ESIError:
        return {"status": "error", "count": 0}

    from apps.corporation.models import EveName

    party_ids = {r.get("issuer_id") for r in rows} | {r.get("assignee_id") for r in rows}
    party_ids.discard(None)
    names = dict(EveName.objects.filter(entity_id__in=party_ids).values_list("entity_id", "name"))

    objs = []
    for r in rows:
        cid = r.get("contract_id")
        if not cid:
            continue
        objs.append(CorpContract(
            contract_id=cid, type=(r.get("type") or "")[:20], status=(r.get("status") or "")[:24],
            issuer_id=r.get("issuer_id"), issuer_corporation_id=r.get("issuer_corporation_id"),
            issuer_name=names.get(r.get("issuer_id"), ""),
            assignee_id=r.get("assignee_id"), assignee_name=names.get(r.get("assignee_id"), ""),
            title=(r.get("title") or "")[:255],
            price=_dec(r.get("price", 0)), reward=_dec(r.get("reward", 0)),
            volume=float(r.get("volume") or 0),
            date_issued=parse_datetime(r.get("date_issued") or "") or None,
            date_expired=parse_datetime(r.get("date_expired") or "") or None,
            date_completed=parse_datetime(r.get("date_completed") or "") or None,
        ))

    # Snapshot replace: the endpoint returns the corp's recent + outstanding set.
    CorpContract.objects.all().delete()
    CorpContract.objects.bulk_create(objs)

    # Register freshness so the procurement board (and the health page) can show a
    # dead director token as a stale chip instead of silently green. Recorded only
    # on success: a no_token/error path leaves the last-good stamp to age out.
    from apps.admin_audit.health import record_sync

    record_sync("corp_contracts", count=len(objs))
    return {"status": "ok", "count": len(objs)}
