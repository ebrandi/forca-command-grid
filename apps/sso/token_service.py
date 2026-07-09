"""Provide valid ESI access tokens for a character, refreshing as needed."""
from __future__ import annotations

import logging

import requests
from django.db import transaction
from django.utils import timezone

from core.esi import oauth

from .models import AuthToken, EveCharacter

log = logging.getLogger("forca.sso")


class NoValidToken(Exception):
    pass


def _select_token(character: EveCharacter, required_scopes: list[str] | None) -> AuthToken | None:
    qs = character.tokens.filter(revoked_at__isnull=True).order_by("-updated_at")
    if required_scopes:
        for token in qs:
            if set(required_scopes).issubset(set(token.scopes or [])):
                return token
        return None
    return qs.first()


def get_valid_access_token(
    character: EveCharacter, required_scopes: list[str] | None = None
) -> str:
    """Return a usable access token for a character, refreshing if expired.

    Raises NoValidToken if no non-revoked token (with the required scopes)
    exists or refresh fails.
    """
    selected = _select_token(character, required_scopes)
    if selected is None or not selected.is_valid:
        raise NoValidToken(f"No valid token for character {character.character_id}")
    return access_token_for(selected)


def access_token_for(token: AuthToken) -> str:
    """Return a usable access token for a *specific* AuthToken row, refreshing if
    expired. Lets a caller (e.g. scope reconciliation) address one exact token
    rather than "the widest token that covers these scopes".

    Raises NoValidToken if the token is revoked/empty or a refresh fails.
    """
    if not token.is_valid:
        raise NoValidToken(f"No valid token for character {token.character_id}")

    if not token.access_expired and token.access_token:
        return token.access_token

    # Serialise refresh across workers: CCP invalidates the old refresh token on
    # first use, so a concurrent double-refresh would lose the rotated token.
    with transaction.atomic():
        locked = AuthToken.objects.select_for_update().get(pk=token.pk)
        if not locked.access_expired and locked.access_token:
            return locked.access_token  # another worker already refreshed it

        try:
            refreshed = oauth.refresh_access_token(locked.refresh_token)
        except requests.HTTPError as exc:
            # 400 invalid_grant => the refresh token is dead; count toward revocation.
            status = exc.response.status_code if exc.response is not None else None
            if status == 400:
                locked.refresh_fail_count += 1
                if locked.refresh_fail_count >= 3:
                    locked.revoked_at = timezone.now()
                locked.save(update_fields=["refresh_fail_count", "revoked_at"])
            log.warning("token refresh rejected for %s: %s", locked.character_id, exc)
            raise NoValidToken("Token refresh rejected") from exc
        except requests.RequestException as exc:
            # Transient (network/5xx): do NOT count toward revocation.
            log.warning("transient token refresh error for %s: %s", locked.character_id, exc)
            raise NoValidToken("Token refresh transiently failed") from exc

        locked.access_token = refreshed.access_token
        locked.refresh_token = refreshed.refresh_token  # CCP may rotate it
        locked.access_expires_at = timezone.now() + timezone.timedelta(seconds=refreshed.expires_in)
        locked.last_refresh_ok_at = timezone.now()
        locked.refresh_fail_count = 0
        locked.save(
            update_fields=[
                "_access_token",
                "_refresh_token",
                "access_expires_at",
                "last_refresh_ok_at",
                "refresh_fail_count",
            ]
        )
        return locked.access_token
