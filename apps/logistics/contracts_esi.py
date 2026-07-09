"""Verify courier (freight) deliveries against the in-game contracts via ESI.

A hauler self-reports a delivery in the app; this cross-checks the real in-game
contract so a haul only earns full credit once CCP confirms it `finished`. Reads
corp contracts via a director token when one is granted, and falls back to each
hauler's own contracts token. Matching is by acceptor (the assigned hauler's
character) + volume, which is strong enough to avoid false positives. Celery-only.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from apps.sso.models import EveCharacter
from apps.sso.token_service import NoValidToken, get_valid_access_token
from core.esi.client import ESIClient, ESIError

from .models import CourierContract

logger = logging.getLogger("forca.logistics")

CORP_CONTRACTS_SCOPE = "esi-contracts.read_corporation_contracts.v1"
CHAR_CONTRACTS_SCOPE = "esi-contracts.read_character_contracts.v1"
_VOLUME_TOLERANCE = Decimal("0.02")  # ±2% to absorb rounding


def _director_contract_token(corp_id: int):
    """A corp character whose token carries the corp-contracts scope (or None)."""
    for character in EveCharacter.objects.filter(corporation_id=corp_id, is_corp_member=True):
        if not character.tokens.filter(revoked_at__isnull=True).exists():
            continue
        try:
            return get_valid_access_token(character, [CORP_CONTRACTS_SCOPE])
        except NoValidToken:
            continue
    return None


def _courier(rows: list) -> list:
    return [r for r in rows if r.get("type") == "courier"]


def _fetch_corp_contracts(client: ESIClient, corp_id: int) -> list:
    token = _director_contract_token(corp_id)
    if token is None:
        return []
    try:
        return _courier(client.get_paged(f"/corporations/{corp_id}/contracts/", token=token))
    except ESIError:
        logger.warning("corp contracts fetch failed for corp %s", corp_id)
        return []


def _fetch_char_contracts(client: ESIClient, character_id: int) -> list:
    character = EveCharacter.objects.filter(character_id=character_id).first()
    if character is None:
        return []
    try:
        token = get_valid_access_token(character, [CHAR_CONTRACTS_SCOPE])
    except NoValidToken:
        return []
    try:
        return _courier(client.get_paged(f"/characters/{character_id}/contracts/", token=token))
    except ESIError:
        return []


def _volume_matches(app_vol, esi_vol) -> bool:
    app = Decimal(str(app_vol or 0))
    esi = Decimal(str(esi_vol or 0))
    if app <= 0:
        return esi <= 0
    return abs(esi - app) <= app * _VOLUME_TOLERANCE


def reconcile_courier_contracts(client: ESIClient | None = None) -> dict:
    """Verify in-progress / self-delivered hauls against the in-game contracts."""
    client = client or ESIClient()
    corp_id = settings.FORCA_HOME_CORP_ID

    pending = list(
        CourierContract.objects.filter(
            status__in=[CourierContract.Status.IN_PROGRESS, CourierContract.Status.DELIVERED],
            verification_state=CourierContract.Verification.UNVERIFIED,
            assigned_hauler_character_id__isnull=False,
        )
    )
    if not pending:
        return {"checked": 0, "verified": 0, "failed": 0}

    rows = list(_fetch_corp_contracts(client, corp_id)) if corp_id else []
    covered = {r.get("acceptor_id") for r in rows}
    # Where the corp token doesn't see a hauler's contracts, try their own token.
    for hauler_id in {c.assigned_hauler_character_id for c in pending} - covered:
        rows.extend(_fetch_char_contracts(client, hauler_id))

    by_acceptor: dict[int, list] = {}
    for r in rows:
        by_acceptor.setdefault(r.get("acceptor_id"), []).append(r)

    verified = failed = 0
    for contract in pending:
        match = next(
            (
                r
                for r in by_acceptor.get(contract.assigned_hauler_character_id, [])
                if r.get("status") in ("finished", "failed")
                and _volume_matches(contract.volume_m3, r.get("volume"))
            ),
            None,
        )
        if match is None:
            continue
        if match.get("status") == "finished":
            _mark_verified(contract, match)
            verified += 1
        else:
            _mark_failed(contract)
            failed += 1
    return {"checked": len(pending), "verified": verified, "failed": failed}


def _mark_verified(contract: CourierContract, esi_row: dict) -> None:
    contract.verification_state = CourierContract.Verification.VERIFIED
    contract.status = CourierContract.Status.DELIVERED
    contract.esi_contract_id = esi_row.get("contract_id")
    contract.verified_at = timezone.now()
    contract.save(update_fields=["verification_state", "status", "esi_contract_id", "verified_at"])

    if contract.assigned_user_id is None:
        return
    from apps.pilots.models import ContributionEvent
    from apps.pilots.services import record_contribution
    from apps.pilots.weights import points_for

    # Idempotent per contract: upgrades a provisional (0-point) haul to full points.
    record_contribution(
        contract.assigned_user, ContributionEvent.Kind.HAUL,
        magnitude=contract.volume_m3, unit="m³",
        description=f"{contract.origin_name} → {contract.dest_name}",
        ref_type="courier_contract", ref_id=str(contract.pk),
        gap_ref="verified", points=points_for("haul"),
    )


def _mark_failed(contract: CourierContract) -> None:
    contract.verification_state = CourierContract.Verification.FAILED
    contract.status = CourierContract.Status.FAILED
    contract.save(update_fields=["verification_state", "status"])

    # A job that didn't actually complete earns nothing — drop any provisional credit.
    from apps.pilots.models import ContributionEvent

    ContributionEvent.objects.filter(
        kind=ContributionEvent.Kind.HAUL, ref_type="courier_contract", ref_id=str(contract.pk)
    ).delete()
