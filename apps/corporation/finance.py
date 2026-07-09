"""Corp wallet sync + finance summaries (ESI corporation wallets).

Pulls division balances and the recent wallet journal from an Accountant/Director
token, de-duplicated by ESI entry id. The journal also yields a member ISK ledger
(who has donated / paid tax). Reads ESI, writes our tables — never called from a web
request except via the explicit officer "sync now".
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.utils.dateparse import parse_datetime

WALLET_SCOPE = "esi-wallet.read_corporation_wallets.v1"


def _token_character(corp_id: int):
    from apps.sso.models import EveCharacter
    from apps.sso.token_service import NoValidToken, get_valid_access_token

    for character in EveCharacter.objects.filter(is_corp_member=True):
        try:
            if get_valid_access_token(character, [WALLET_SCOPE]):
                return character
        except NoValidToken:
            continue
    return None


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - tolerate odd ESI values
        return Decimal("0")


def sync_corp_wallets(corp_id: int | None = None, client=None) -> dict:
    """Sync division balances + recent journal for the corporation."""
    from core.esi.client import ESIClient, ESIError

    from .models import CorpWalletDivision, CorpWalletJournalEntry

    corp_id = corp_id or getattr(settings, "FORCA_HOME_CORP_ID", 0)
    character = _token_character(corp_id)
    if character is None:
        return {"status": "no_token", "entries": 0}

    from apps.sso.token_service import get_valid_access_token

    token = get_valid_access_token(character, [WALLET_SCOPE])
    client = client or ESIClient()
    try:
        divisions = client.get(f"/corporations/{corp_id}/wallets/", token=token).data or []
    except ESIError:
        return {"status": "error", "entries": 0}

    for d in divisions:
        CorpWalletDivision.objects.update_or_create(
            division=d["division"], defaults={"balance": _dec(d.get("balance", 0))}
        )

    entries = 0
    for d in divisions:
        division = d["division"]
        try:
            rows = client.get(
                f"/corporations/{corp_id}/wallets/{division}/journal/", token=token,
            ).data or []
        except ESIError:
            continue
        objs = []
        for r in rows:
            eid = r.get("id")
            ts = parse_datetime(r.get("date") or "")
            if not eid or ts is None:
                continue
            objs.append(CorpWalletJournalEntry(
                entry_id=eid, division=division, date=ts, ref_type=r.get("ref_type", ""),
                amount=_dec(r.get("amount", 0)),
                balance=_dec(r["balance"]) if r.get("balance") is not None else None,
                first_party_id=r.get("first_party_id"), second_party_id=r.get("second_party_id"),
                description=(r.get("description") or "")[:255], reason=(r.get("reason") or "")[:255],
                tax=_dec(r["tax"]) if r.get("tax") is not None else None,
            ))
        CorpWalletJournalEntry.objects.bulk_create(objs, ignore_conflicts=True)
        entries += len(objs)
    return {"status": "ok", "divisions": len(divisions), "entries": entries}


def member_isk_ledger(days: int = 30, limit: int = 15) -> list[dict]:
    """Top ISK-earning members over the window.

    Each positive journal line is credited to whichever party is an actual corp
    member — so ratting/ESS/mission income (where the *first* party is the NPC
    payer, CONCORD/ESS/agent corp, and the member is the *second* party) is
    attributed to the member, not the NPC. Entries that involve no corp member
    (pure NPC noise) are ignored, which is why the NPC payers no longer appear.
    """
    import datetime as dt
    from decimal import Decimal

    from django.db.models import Q
    from django.utils import timezone

    from apps.sso.models import EveCharacter

    from .models import CorpMember, CorpWalletJournalEntry, EveName

    since = timezone.now() - dt.timedelta(days=days)
    # Who counts as "us": registered corp characters + the synced roster (covers
    # members who haven't linked an account yet).
    member_ids = set(
        EveCharacter.objects.filter(is_corp_member=True).values_list("character_id", flat=True)
    )
    member_ids |= set(CorpMember.objects.values_list("character_id", flat=True))
    if not member_ids:
        return []

    totals: dict[int, list] = {}
    for fp, sp, amount in (
        CorpWalletJournalEntry.objects.filter(date__gte=since, amount__gt=0)
        .filter(Q(first_party_id__in=member_ids) | Q(second_party_id__in=member_ids))
        .values_list("first_party_id", "second_party_id", "amount")
    ):
        member = fp if fp in member_ids else sp
        agg = totals.setdefault(member, [Decimal("0"), 0])
        agg[0] += amount
        agg[1] += 1

    ranked = sorted(totals.items(), key=lambda kv: -kv[1][0])[:limit]
    ids = [m for m, _ in ranked]
    char_names = dict(
        EveCharacter.objects.filter(character_id__in=ids).values_list("character_id", "name")
    )
    roster_names = dict(
        CorpMember.objects.filter(character_id__in=ids).exclude(name="")
        .values_list("character_id", "name")
    )
    eve_names = dict(EveName.objects.filter(entity_id__in=ids).values_list("entity_id", "name"))
    return [
        {"party_id": m,
         "name": char_names.get(m) or roster_names.get(m) or eve_names.get(m) or f"#{m}",
         "total": agg[0], "count": agg[1]}
        for m, agg in ranked
    ]
