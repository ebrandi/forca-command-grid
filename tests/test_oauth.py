"""EVE SSO OAuth/JWT tests."""
from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from core.esi import oauth


def test_pkce_and_state_are_random():
    v1, c1 = oauth.generate_pkce()
    v2, c2 = oauth.generate_pkce()
    assert v1 != v2 and c1 != c2
    assert oauth.generate_state() != oauth.generate_state()


def test_build_authorize_url_contains_params(settings):
    settings.EVE_SSO_CLIENT_ID = "cid"
    url = oauth.build_authorize_url("st4te", "ch4llenge", ["publicData"])
    assert "response_type=code" in url
    assert "code_challenge=ch4llenge" in url
    assert "code_challenge_method=S256" in url
    assert "state=st4te" in url
    assert "client_id=cid" in url


def test_character_id_and_scopes_from_claims():
    claims = {"sub": "CHARACTER:EVE:90000001", "scp": ["a", "b"]}
    assert oauth.character_id_from_claims(claims) == 90000001
    assert oauth.scopes_from_claims(claims) == ["a", "b"]
    assert oauth.scopes_from_claims({"scp": "solo"}) == ["solo"]


def _signed(claims, key):
    return jwt.encode(claims, key, algorithm="RS256")


def _fake_jwks(pub):
    return SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=pub))


def test_validate_access_token_ok(settings):
    settings.EVE_SSO_CLIENT_ID = "test-client-id"
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {
        "iss": "login.eveonline.com",
        "aud": ["test-client-id", "EVE Online"],
        "sub": "CHARACTER:EVE:1001",
        "name": "Test",
        "scp": ["publicData"],
        "exp": int(time.time()) + 1200,
    }
    token = _signed(claims, priv)
    out = oauth.validate_access_token(token, jwks_client=_fake_jwks(priv.public_key()))
    assert oauth.character_id_from_claims(out) == 1001


def test_validate_rejects_bad_audience(settings):
    settings.EVE_SSO_CLIENT_ID = "test-client-id"
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {
        "iss": "login.eveonline.com",
        "aud": ["someone-else"],  # missing client id + "EVE Online"
        "sub": "CHARACTER:EVE:1001",
        "exp": int(time.time()) + 1200,
    }
    token = _signed(claims, priv)
    with pytest.raises(oauth.JWTValidationError):
        oauth.validate_access_token(token, jwks_client=_fake_jwks(priv.public_key()))


def test_validate_rejects_bad_issuer(settings):
    settings.EVE_SSO_CLIENT_ID = "test-client-id"
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {
        "iss": "https://evil.example.com/",
        "aud": ["test-client-id", "EVE Online"],
        "sub": "CHARACTER:EVE:1001",
        "exp": int(time.time()) + 1200,
    }
    token = _signed(claims, priv)
    with pytest.raises(oauth.JWTValidationError):
        oauth.validate_access_token(token, jwks_client=_fake_jwks(priv.public_key()))


def test_validate_rejects_expired(settings):
    settings.EVE_SSO_CLIENT_ID = "test-client-id"
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = {
        "iss": "login.eveonline.com",
        "aud": ["test-client-id", "EVE Online"],
        "sub": "CHARACTER:EVE:1001",
        "exp": int(time.time()) - 10,
    }
    token = _signed(claims, priv)
    with pytest.raises(oauth.JWTValidationError):
        oauth.validate_access_token(token, jwks_client=_fake_jwks(priv.public_key()))
