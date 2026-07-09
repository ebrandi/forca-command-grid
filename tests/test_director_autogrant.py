"""Auto-grant the app Director role to in-game corporation Directors at login."""
from __future__ import annotations

import pytest
import responses
from django.conf import settings as dj_settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.sso.models import AuthToken, EveCharacter
from apps.sso.services import ROLE_SCOPE, ensure_role, sync_roles_for_user
from core import rbac


def test_default_login_scopes_include_corporation_roles():
    # Needed so a Director's roles can be read at login without manual .env work.
    assert ROLE_SCOPE in dj_settings.EVE_SSO_DEFAULT_SCOPES


def _member(username: str) -> tuple:
    user = get_user_model().objects.create(username=username)
    char = EveCharacter.objects.create(
        character_id=int(username.split(":")[1]),
        user=user,
        name="Pilot",
        is_main=True,
        is_corp_member=True,
    )
    return user, char


_HOME_CORP = 98000001


def _grant_roles_token(char: EveCharacter) -> None:
    token = AuthToken(
        character=char,
        scopes=[ROLE_SCOPE],
        access_expires_at=timezone.now() + timezone.timedelta(hours=1),
    )
    token.refresh_token = "refresh"
    token.access_token = "access"
    token.save()


def _affiliation(char_id: int, corporation_id: int) -> None:
    """Register the public affiliation read the Director co-check performs before roles."""
    responses.add(
        responses.GET,
        f"https://esi.evetech.net/characters/{char_id}/",
        json={"corporation_id": corporation_id, "name": "Pilot"},
        status=200,
    )


@responses.activate
@pytest.mark.django_db
def test_in_game_director_is_auto_granted_director_role(settings):
    settings.FORCA_HOME_CORP_ID = _HOME_CORP
    user, char = _member("eve:3001")
    _grant_roles_token(char)
    _affiliation(3001, _HOME_CORP)  # currently in the home corp
    responses.add(
        responses.GET,
        "https://esi.evetech.net/characters/3001/roles/",
        json={"roles": ["Director", "Personnel_Manager"]},
        status=200,
    )
    sync_roles_for_user(user)
    assert rbac.has_role(user, rbac.ROLE_DIRECTOR) is True


@responses.activate
@pytest.mark.django_db
def test_non_director_member_does_not_get_director_role(settings):
    settings.FORCA_HOME_CORP_ID = _HOME_CORP
    user, char = _member("eve:3002")
    _grant_roles_token(char)
    _affiliation(3002, _HOME_CORP)
    responses.add(
        responses.GET,
        "https://esi.evetech.net/characters/3002/roles/",
        json={"roles": ["Accountant"]},
        status=200,
    )
    sync_roles_for_user(user)
    assert rbac.has_role(user, rbac.ROLE_MEMBER) is True
    assert rbac.has_role(user, rbac.ROLE_DIRECTOR) is False


@responses.activate
@pytest.mark.django_db
def test_director_role_is_withdrawn_once_not_a_director(settings):
    settings.FORCA_HOME_CORP_ID = _HOME_CORP
    user, char = _member("eve:3003")
    _grant_roles_token(char)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    _affiliation(3003, _HOME_CORP)
    responses.add(
        responses.GET,
        "https://esi.evetech.net/characters/3003/roles/",
        json={"roles": []},
        status=200,
    )
    sync_roles_for_user(user)
    assert rbac.has_role(user, rbac.ROLE_DIRECTOR) is False


@responses.activate
@pytest.mark.django_db
def test_director_role_stripped_when_no_longer_in_home_corp(settings):
    # L1 regression: the pilot's cached is_corp_member flag is stale (they left to
    # another corp where they ARE a Director), but the live affiliation co-check sees a
    # non-home corp, so the roles read is never trusted → app Director is NOT granted.
    settings.FORCA_HOME_CORP_ID = _HOME_CORP
    user, char = _member("eve:3007")  # is_corp_member=True (stale)
    _grant_roles_token(char)
    _affiliation(3007, 98009999)  # actually in a DIFFERENT corp now
    # A roles read would say Director — but it must never be reached / trusted.
    responses.add(
        responses.GET,
        "https://esi.evetech.net/characters/3007/roles/",
        json={"roles": ["Director"]},
        status=200,
    )
    sync_roles_for_user(user)
    assert rbac.has_role(user, rbac.ROLE_DIRECTOR) is False


@responses.activate
@pytest.mark.django_db
def test_director_role_kept_when_roles_unreadable_but_token_present(settings):
    # A proving token still exists, but ESI is momentarily unreadable (the roles lookup
    # errors → status "unknown"). The role must be kept rather than flapped off on a
    # transient failure. (Affiliation resolves to the home corp so the co-check passes.)
    settings.FORCA_HOME_CORP_ID = _HOME_CORP
    user, char = _member("eve:3004")
    _grant_roles_token(char)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    _affiliation(3004, _HOME_CORP)
    # ESI returns an error for the roles lookup → status is "unknown" (None).
    responses.add(
        responses.GET,
        "https://esi.evetech.net/characters/3004/roles/",
        json={"error": "unavailable"},
        status=404,
    )
    sync_roles_for_user(user)
    assert rbac.has_role(user, rbac.ROLE_DIRECTOR) is True


@responses.activate
@pytest.mark.django_db
def test_director_role_withdrawn_when_no_proving_token():
    # No non-revoked token carrying the roles scope can ever substantiate the
    # grant (e.g. the proving character was disconnected), so the stale Director
    # role is withdrawn rather than left sticky.
    user, _ = _member("eve:3006")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    sync_roles_for_user(user)
    assert rbac.has_role(user, rbac.ROLE_DIRECTOR) is False


@responses.activate
@pytest.mark.django_db
def test_leaving_corp_strips_member_and_director():
    user, char = _member("eve:3005")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_DIRECTOR))
    char.is_corp_member = False
    char.save(update_fields=["is_corp_member"])
    sync_roles_for_user(user)
    assert rbac.has_role(user, rbac.ROLE_MEMBER) is False
    assert rbac.has_role(user, rbac.ROLE_DIRECTOR) is False
