"""Regression tests for issues found during adversarial QA."""
from __future__ import annotations

import pytest
import responses
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.killboard.ingest import classify_sec_band, ingest_killmail
from apps.killboard.models import Killmail, SecBand
from apps.sso.models import AuthToken, EveCharacter
from apps.sso.services import CharacterAlreadyLinked, upsert_character
from apps.sso.token_service import get_valid_access_token


# --- account-takeover guard -------------------------------------------------
@pytest.mark.django_db
def test_cannot_hijack_character_linked_to_another_account():
    User = get_user_model()
    owner = User.objects.create(username="owner")
    attacker = User.objects.create(username="attacker")
    EveCharacter.objects.create(character_id=500, user=owner, name="Victim")

    with pytest.raises(CharacterAlreadyLinked):
        upsert_character(attacker, 500, "Victim")

    # Ownership unchanged.
    assert EveCharacter.objects.get(character_id=500).user_id == owner.id


@pytest.mark.django_db
def test_upsert_links_unowned_character():
    User = get_user_model()
    user = User.objects.create(username="u")
    # A character referenced by a killmail but not yet linked to anyone.
    EveCharacter.objects.create(character_id=501, name="Free")
    ch = upsert_character(user, 501, "Free")
    assert ch.user_id == user.id


# --- sec band boundaries ----------------------------------------------------
def test_sec_band_boundaries():
    assert classify_sec_band(30000142, 0.9) == SecBand.HIGHSEC
    assert classify_sec_band(30000142, 0.5) == SecBand.HIGHSEC
    assert classify_sec_band(30000142, 0.3) == SecBand.LOWSEC
    # ~0.04 rounds to 0.0 -> nullsec (was wrongly lowsec before the fix)
    assert classify_sec_band(30000142, 0.04) == SecBand.NULLSEC
    assert classify_sec_band(30000142, -0.2) == SecBand.NULLSEC
    assert classify_sec_band(31000005, 0.0) == SecBand.WORMHOLE
    assert classify_sec_band(30000142, 0.5, region_id=10000070) == SecBand.POCHVEN


# --- NPC / solo classification ----------------------------------------------
def _body(km_id, attackers, time="2026-06-20T10:00:00Z"):
    return {
        "killmail_id": km_id,
        "killmail_time": time,
        "solar_system_id": 30002053,
        "victim": {"character_id": 9, "corporation_id": 98000001, "ship_type_id": 587, "items": []},
        "attackers": attackers,
    }


@pytest.mark.django_db
def test_solo_with_npc_coattacker_stays_solo(sde):
    body = _body(
        200,
        [
            {"character_id": 1, "corporation_id": 99, "ship_type_id": 587, "final_blow": True},
            {"faction_id": 500001, "ship_type_id": 1},  # NPC, no character_id
        ],
    )
    km = ingest_killmail(200, "h", body=body)
    assert km.is_solo is True
    assert km.is_npc is False


@pytest.mark.django_db
def test_pure_npc_kill_flagged(sde):
    body = _body(201, [{"faction_id": 500001, "corporation_id": 1000125, "ship_type_id": 1}])
    km = ingest_killmail(201, "h", body=body)
    assert km.is_npc is True
    assert km.is_solo is False


@pytest.mark.django_db
def test_missing_killmail_time_raises(sde):
    body = _body(202, [{"character_id": 1, "corporation_id": 99}], time=None)
    with pytest.raises(ValueError):
        ingest_killmail(202, "h", body=body)
    assert not Killmail.objects.filter(killmail_id=202).exists()


# --- token refresh ----------------------------------------------------------
@responses.activate
@pytest.mark.django_db
def test_token_refresh_rotates_and_returns_new_access(settings):
    character = EveCharacter.objects.create(character_id=700, name="T")
    token = AuthToken(
        character=character,
        scopes=["esi-skills.read_skills.v1"],
        access_expires_at=timezone.now() - timezone.timedelta(minutes=5),  # expired
    )
    token.refresh_token = "old-refresh"
    token.access_token = "old-access"
    token.save()

    responses.add(
        responses.POST,
        settings.EVE_SSO_TOKEN_URL,
        json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 1200},
        status=200,
    )
    access = get_valid_access_token(character, ["esi-skills.read_skills.v1"])
    assert access == "new-access"
    token.refresh_from_db()
    assert token.refresh_token == "new-refresh"  # rotated + re-encrypted
    assert token.refresh_fail_count == 0
