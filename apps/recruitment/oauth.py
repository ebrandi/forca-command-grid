"""Recruitment OAuth — the SECOND EVE application, bound to RECRUITMENT_SSO_*.

Read-only candidate vetting: a consenting candidate authorises once, we read
their skills + corp roles a single time to derive evidence, then discard the
token. Because nothing is stored, only the authorize + one-shot exchange/validate
are needed here — no refresh, no revoke. Everything client-agnostic (PKCE, state,
JWKS, claim parsing) is reused from core.esi.oauth so the JWT is validated by the
exact same hardened path as member login, just bound to the recruitment client.
"""
from __future__ import annotations

from django.conf import settings

from core.esi import oauth as _core

# Client-agnostic helpers reused verbatim by the views.
generate_pkce = _core.generate_pkce
generate_state = _core.generate_state
character_id_from_claims = _core.character_id_from_claims
scopes_from_claims = _core.scopes_from_claims
JWTValidationError = _core.JWTValidationError
TokenResponse = _core.TokenResponse


def _client() -> _core.SSOClient:
    return _core.SSOClient(
        client_id=settings.RECRUITMENT_SSO_CLIENT_ID,
        client_secret=settings.RECRUITMENT_SSO_CLIENT_SECRET,
        callback_url=settings.RECRUITMENT_SSO_CALLBACK_URL,
    )


def build_authorize_url(state: str, code_challenge: str, scopes: list[str]) -> str:
    return _core.build_authorize_url(state, code_challenge, scopes, client=_client())


def exchange_code(code: str, code_verifier: str) -> _core.TokenResponse:
    return _core.exchange_code(code, code_verifier, client=_client())


def validate_access_token(access_token: str) -> dict:
    return _core.validate_access_token(access_token, client=_client())
