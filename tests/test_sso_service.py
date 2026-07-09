"""SSO login/service tests."""
from __future__ import annotations

import pytest
import responses
from django.contrib.auth import get_user_model

from apps.sso.models import AuthToken
from apps.sso.services import complete_login
from core import rbac
from core.esi import oauth


@responses.activate
@pytest.mark.django_db
def test_complete_login_links_character_and_assigns_member(settings):
    settings.FORCA_HOME_CORP_ID = 98000001
    responses.add(
        responses.GET,
        "https://esi.evetech.net/characters/1001/",
        json={"corporation_id": 98000001, "alliance_id": 99000001, "name": "Test Pilot"},
        status=200,
    )
    User = get_user_model()
    user = User.objects.create(username="eve:1001")

    claims = {"sub": "CHARACTER:EVE:1001", "name": "Test Pilot", "scp": ["publicData"]}
    token = oauth.TokenResponse("access", "refresh", 1200, "Bearer")

    character = complete_login(user, claims, token)

    assert character.character_id == 1001
    assert character.is_corp_member is True
    assert character.is_main is True
    # token stored + encrypted
    stored = AuthToken.objects.get(character=character)
    assert stored.refresh_token == "refresh"
    assert "refresh" not in stored._refresh_token
    # member role assigned
    assert rbac.has_role(user, rbac.ROLE_MEMBER) is True


@responses.activate
@pytest.mark.django_db
def test_complete_login_non_member_gets_no_member_role(settings):
    settings.FORCA_HOME_CORP_ID = 98000001
    responses.add(
        responses.GET,
        "https://esi.evetech.net/characters/2002/",
        json={"corporation_id": 55555, "name": "Outsider"},
        status=200,
    )
    User = get_user_model()
    user = User.objects.create(username="eve:2002")
    claims = {"sub": "CHARACTER:EVE:2002", "name": "Outsider", "scp": []}
    token = oauth.TokenResponse("a", "r", 1200, "Bearer")

    character = complete_login(user, claims, token)
    assert character.is_corp_member is False
    assert rbac.has_role(user, rbac.ROLE_MEMBER) is False


# --- token pruning (every login mints a token; redundant ones must die) -------
def _tok(character, scopes):
    from apps.sso.services import store_token

    return store_token(character, oauth.TokenResponse("a", "r", 1200, "Bearer"), scopes)


@pytest.mark.django_db
def test_store_token_revokes_older_subset_tokens(character):
    basic = _tok(character, ["publicData", "esi-skills.read_skills.v1"])
    director = _tok(character, ["publicData", "esi-assets.read_corporation_assets.v1"])
    # A fresh basic login: covers the first token (revoked) but NOT the
    # director grant (extra scope — must survive).
    newest = _tok(character, ["publicData", "esi-skills.read_skills.v1"])

    basic.refresh_from_db()
    director.refresh_from_db()
    newest.refresh_from_db()
    assert basic.revoked_at is not None
    assert director.revoked_at is None
    assert newest.revoked_at is None
    assert AuthToken.objects.filter(character=character, revoked_at__isnull=True).count() == 2


@pytest.mark.django_db
def test_prune_superseded_tokens_collapses_backlog(character):
    from apps.sso.tasks import prune_superseded_tokens

    # Simulate the pre-fix backlog: identical tokens accumulated by re-logins
    # (bypass store_token's own pruning by creating rows directly).
    for _ in range(4):
        AuthToken.objects.create(character=character, scopes=["publicData", "esi-skills.read_skills.v1"])
    AuthToken.objects.create(
        character=character,
        scopes=["publicData", "esi-skills.read_skills.v1", "esi-assets.read_corporation_assets.v1"],
    )

    # An OLDER-but-wider token must also retire newer narrower ones —
    # coverage wins regardless of age.
    AuthToken.objects.create(character=character, scopes=["publicData"])

    revoked = prune_superseded_tokens()

    # The wide token covers everything, so all six others are redundant.
    assert revoked == 5
    live = AuthToken.objects.filter(character=character, revoked_at__isnull=True)
    assert live.count() == 1
    assert "esi-assets.read_corporation_assets.v1" in live.first().scopes
