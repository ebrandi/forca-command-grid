"""Discord OAuth2 — account linking (5.1).

Standard OAuth2 authorization-code + PKCE. Reuses the provider-neutral primitives from
``core.esi.oauth`` (``generate_pkce`` / ``generate_state``); only the endpoints and the
identity call are Discord-specific (EVE's JWT/``aud`` validation is replaced by a simple
``GET /users/@me``). The client stays inert unless ``DISCORD_OAUTH_ENABLED`` (both id +
secret set), mirroring ``RECRUITMENT_SSO_ENABLED``.

All calls are worker/request-safe and SSRF-guarded: HTTPS + ``discord.com`` host only, no
redirects; errors are redacted (never echo a token).
"""
from __future__ import annotations

from urllib.parse import urlencode

import requests
from django.conf import settings

from core.esi.oauth import generate_pkce, generate_state  # noqa: F401 (re-exported)

from . import credentials

AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
TOKEN_URL = "https://discord.com/api/oauth2/token"  # noqa: S105 - endpoint URL, not a secret
USERINFO_URL = "https://discord.com/api/users/@me"
_ALLOWED_HOST = "discord.com"
# Minimal scope: read the linked user's id + username. (guilds.join — auto-add the pilot to
# the guild — is a deliberate future opt-in, not requested here.)
DEFAULT_SCOPES = ("identify",)


class OAuthError(Exception):
    """Redacted OAuth failure (safe to surface to the user)."""


def enabled() -> bool:
    """Linking is available when a client id + secret are configured (console or env)."""
    return credentials.discord_oauth_enabled() or bool(
        getattr(settings, "DISCORD_OAUTH_ENABLED", False)
    )


def build_authorize_url(state: str, code_challenge: str, scopes=DEFAULT_SCOPES) -> str:
    creds = credentials.discord_oauth()
    params = {
        "response_type": "code",
        "client_id": creds["client_id"],
        "redirect_uri": creds["callback_url"],
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str, code_verifier: str) -> dict:
    """Exchange an auth code for tokens. Returns the token dict; raises OAuthError."""
    creds = credentials.discord_oauth()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": creds["callback_url"],
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "code_verifier": code_verifier,
    }
    try:
        resp = requests.post(
            TOKEN_URL, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10, allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise OAuthError(f"token request failed: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise OAuthError(f"token exchange returned http {resp.status_code}")
    try:
        body = resp.json() or {}
    except ValueError as exc:
        raise OAuthError("token response was not JSON") from exc
    if not body.get("access_token"):
        raise OAuthError("token response missing access_token")
    return body


def fetch_identity(access_token: str) -> dict:
    """``{id, username, ...}`` for the authenticated Discord user. Raises OAuthError."""
    try:
        resp = requests.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10, allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise OAuthError(f"identity request failed: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise OAuthError(f"identity lookup returned http {resp.status_code}")
    try:
        body = resp.json() or {}
    except ValueError as exc:
        raise OAuthError("identity response was not JSON") from exc
    if not body.get("id"):
        raise OAuthError("identity response missing user id")
    return body


def display_handle(identity: dict) -> str:
    """A human label for the linked account (``username`` or legacy ``name#discriminator``)."""
    username = identity.get("username", "") or ""
    discriminator = identity.get("discriminator", "") or ""
    if discriminator and discriminator != "0":
        return f"{username}#{discriminator}"
    return username or identity.get("global_name", "") or str(identity.get("id", ""))
