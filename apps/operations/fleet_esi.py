"""Live fleet tracking via the ESI Fleet API → automatic attendance (PAP).

An FC who boss-fleets and granted the fleet scope can pull the live fleet roster
in one click; every fleet member who has a linked account is recorded as
attending the operation (confirmed, since the FC vouched for it by pulling), and
credited a fleet contribution — replacing manual "I was there" sign-in.
"""
from __future__ import annotations

FLEET_SCOPE = "esi-fleets.read_fleet.v1"


def _credit_fleet(user, operation) -> None:
    from django.utils import timezone

    from apps.pilots.models import ContributionEvent
    from apps.pilots.services import record_contribution

    record_contribution(
        user, ContributionEvent.Kind.FLEET, 1, "fleets",
        description=operation.name, ref_type="operation",
        ref_id=f"{operation.pk}:{user.pk}",
        occurred_at=operation.target_at or timezone.now(),
    )


def pull_fleet_attendance(operation, fc_character, client=None) -> dict:
    """Read the FC's current fleet and record attendance for linked members."""
    from apps.sso.models import EveCharacter
    from apps.sso.token_service import NoValidToken, get_valid_access_token
    from core.esi.client import ESIClient, ESIError

    from .models import OperationAttendance

    try:
        token = get_valid_access_token(fc_character, [FLEET_SCOPE])
    except NoValidToken:
        return {"status": "no_token", "recorded": 0}

    client = client or ESIClient()
    try:
        fleet = client.get(f"/characters/{fc_character.character_id}/fleet/", token=token).data or {}
    except ESIError:
        return {"status": "not_in_fleet", "recorded": 0}
    fleet_id = fleet.get("fleet_id")
    if not fleet_id:
        return {"status": "not_in_fleet", "recorded": 0}

    try:
        members = client.get(f"/fleets/{fleet_id}/members/", token=token).data or []
    except ESIError:
        # 403 here means the FC isn't fleet boss (only the boss can read members).
        return {"status": "not_boss", "recorded": 0}

    member_ids = [m.get("character_id") for m in members if m.get("character_id")]
    linked = EveCharacter.objects.filter(
        character_id__in=member_ids, user__isnull=False
    ).select_related("user")

    recorded = 0
    seen_users = set()
    for ch in linked:
        if ch.user_id in seen_users:  # an FC may have multiple alts in fleet
            continue
        seen_users.add(ch.user_id)
        OperationAttendance.objects.update_or_create(
            operation=operation, user=ch.user,
            defaults={
                "character_id": ch.character_id, "character_name": ch.name,
                "confirmed": True, "added_by_officer": True,
            },
        )
        _credit_fleet(ch.user, operation)
        recorded += 1

    return {"status": "ok", "recorded": recorded, "fleet_size": len(member_ids)}
