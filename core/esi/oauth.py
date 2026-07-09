"""EVE SSO OAuth2 (authorization-code + PKCE) and JWT validation.

We implement the flow directly (rather than depending on django-esi) so we
control JWT validation and token encryption precisely. See handbooks/contributor-handbook/esi-integration.md §2.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import jwt
import requests
from django.conf import settings
from jwt import PyJWKClient

_TIMEOUT = 20


@dataclass(frozen=True)
class SSOClient:
    """A registered EVE application's credentials + callback.

    The app runs two: the member-login client (default) and a separate,
    read-only recruitment client. CCP's authorize/token/JWKS endpoints are
    shared; only the id/secret/callback (and therefore the JWT aud/azp the token
    is minted for) differ, so every function that touches a client-bound value
    takes an optional ``client`` and falls back to the login app.
    """

    client_id: str
    client_secret: str
    callback_url: str


def _resolve_client(client: SSOClient | None) -> SSOClient:
    return client or SSOClient(
        client_id=settings.EVE_SSO_CLIENT_ID,
        client_secret=settings.EVE_SSO_CLIENT_SECRET,
        callback_url=settings.EVE_SSO_CALLBACK_URL,
    )


def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def generate_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(
    state: str,
    code_challenge: str,
    scopes: list[str] | None = None,
    client: SSOClient | None = None,
) -> str:
    client = _resolve_client(client)
    params = {
        "response_type": "code",
        "redirect_uri": client.callback_url,
        "client_id": client.client_id,
        "scope": " ".join(scopes or settings.EVE_SSO_DEFAULT_SCOPES),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{settings.EVE_SSO_AUTHORIZE_URL}?{urlencode(params)}"


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str


def exchange_code(
    code: str, code_verifier: str, client: SSOClient | None = None
) -> TokenResponse:
    """Exchange an authorization code for tokens (PKCE; confidential client)."""
    client = _resolve_client(client)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "client_id": client.client_id,
    }
    resp = _token_request(data, client)
    return TokenResponse(
        access_token=resp["access_token"],
        refresh_token=resp.get("refresh_token", ""),
        expires_in=int(resp.get("expires_in", 1200)),
        token_type=resp.get("token_type", "Bearer"),
    )


def refresh_access_token(refresh_token: str) -> TokenResponse:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.EVE_SSO_CLIENT_ID,
    }
    resp = _token_request(data)
    return TokenResponse(
        access_token=resp["access_token"],
        # CCP rotates refresh tokens; fall back to the old one if absent.
        refresh_token=resp.get("refresh_token", refresh_token),
        expires_in=int(resp.get("expires_in", 1200)),
        token_type=resp.get("token_type", "Bearer"),
    )


def revoke_token(refresh_token: str) -> None:
    """Best-effort server-side revocation of a refresh token at CCP.

    Local erasure stays the hard guarantee; this additionally drops the grant at
    CCP so the refresh token can't be used until natural expiry. Never raises.
    """
    if not refresh_token:
        return
    data = {"token_type_hint": "refresh_token", "token": refresh_token}
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": settings.ESI_USER_AGENT,
    }
    auth = None
    if settings.EVE_SSO_CLIENT_SECRET:
        auth = (settings.EVE_SSO_CLIENT_ID, settings.EVE_SSO_CLIENT_SECRET)
    else:
        data["client_id"] = settings.EVE_SSO_CLIENT_ID
    url = settings.EVE_SSO_BASE.rstrip("/") + "/v2/oauth/revoke"
    try:
        requests.post(url, data=data, headers=headers, auth=auth, timeout=_TIMEOUT)
    except Exception:  # noqa: BLE001 - revocation is best-effort; local erase is the guarantee
        import logging

        logging.getLogger("forca.sso").warning("CCP refresh-token revoke failed (continuing)")


def _token_request(data: dict, client: SSOClient | None = None) -> dict:
    client = _resolve_client(client)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": settings.ESI_USER_AGENT,
    }
    auth = None
    # Confidential client: HTTP Basic with client_id:client_secret when a
    # secret is configured; otherwise public PKCE client.
    if client.client_secret:
        auth = (client.client_id, client.client_secret)
        data = {k: v for k, v in data.items() if k != "client_id"}
    resp = requests.post(
        settings.EVE_SSO_TOKEN_URL, data=data, headers=headers, auth=auth, timeout=_TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


class JWTValidationError(Exception):
    pass


def validate_access_token(
    access_token: str,
    jwks_client: PyJWKClient | None = None,
    client: SSOClient | None = None,
) -> dict:
    """Validate an EVE SSO JWT and return its claims.

    Verifies signature via JWKS (by ``kid``), ``exp``, the issuer, and that the
    audience contains both our client id and the literal "EVE Online". ``client``
    selects which registered app's id the audience/azp must bind to (defaults to
    the login app); a recruitment-minted token must validate against the
    recruitment client id, never the login one.
    """
    client = _resolve_client(client)
    try:
        signer = jwks_client or _get_jwks_client()
        signing_key = signer.get_signing_key_from_jwt(access_token)
        claims = jwt.decode(
            access_token,
            signing_key.key,
            # EVE SSO signs with RS256; pinning the single expected algorithm
            # removes any algorithm-confusion/downgrade surface.
            algorithms=["RS256"],
            # Reject a token that omits any security-relevant claim outright,
            # rather than letting a missing `exp` silently mean "never expires".
            options={
                "verify_aud": False,  # audience checked manually below
                "require": ["exp", "iss", "sub", "aud"],
            },
        )
    except jwt.PyJWTError as exc:
        raise JWTValidationError(f"JWT decode/verify failed: {exc}") from exc

    iss = claims.get("iss", "")
    if iss not in settings.EVE_SSO_ISSUERS:
        raise JWTValidationError(f"Unexpected issuer: {iss!r}")

    aud = claims.get("aud", [])
    aud = [aud] if isinstance(aud, str) else list(aud)
    if "EVE Online" not in aud or client.client_id not in aud:
        raise JWTValidationError("Audience does not contain client id and 'EVE Online'")

    # `azp` (authorized party) names the client the token was actually minted
    # for. EVE tokens carry it; binding to our client id here closes the residual
    # ambiguity of the membership-only audience check above.
    azp = claims.get("azp")
    if azp and azp != client.client_id:
        raise JWTValidationError("Token azp is not our client")

    return claims


def character_id_from_claims(claims: dict) -> int:
    """sub looks like 'CHARACTER:EVE:<character_id>'."""
    sub = claims.get("sub", "")
    parts = sub.split(":")
    if len(parts) != 3 or parts[0] != "CHARACTER":
        raise JWTValidationError(f"Unexpected sub claim: {sub!r}")
    try:
        return int(parts[2])
    except ValueError as exc:
        # Normalise a non-numeric id to the same typed failure every caller handles,
        # rather than letting a bare ValueError escape to a 500.
        raise JWTValidationError(f"Non-numeric character id in sub: {sub!r}") from exc


def owner_hash_from_claims(claims: dict) -> str:
    """The EVE SSO ``owner`` hash — changes when a character is transferred to a
    different EVE account. Persisted per character so a later login with a
    changed owner can be detected (and refused) rather than silently resolving
    into the previous owner's platform account."""
    return claims.get("owner", "") or ""


def scopes_from_claims(claims: dict) -> list[str]:
    scp = claims.get("scp", [])
    if isinstance(scp, str):
        return [scp]
    return list(scp)


_jwks_client_cache: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    """Lazily build the JWKS client from SSO metadata.

    On a transient metadata failure we raise (and leave the cache empty) so the
    next login retries, rather than wedging a bad/half-built client into the
    process global.
    """
    global _jwks_client_cache
    if _jwks_client_cache is None:
        try:
            resp = requests.get(settings.EVE_SSO_METADATA_URL, timeout=_TIMEOUT)
            resp.raise_for_status()
            jwks_uri = resp.json()["jwks_uri"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            raise JWTValidationError(f"Could not load SSO signing metadata: {exc}") from exc
        _jwks_client_cache = PyJWKClient(jwks_uri)
    return _jwks_client_cache
