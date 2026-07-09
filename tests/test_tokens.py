"""Token encryption tests."""
from __future__ import annotations

import pytest

from apps.sso.models import AuthToken, EveCharacter
from core.esi import tokens


def test_encrypt_decrypt_roundtrip():
    secret = "refresh-abc-123"
    ct = tokens.encrypt(secret)
    assert ct != secret
    assert tokens.decrypt(ct) == secret


def test_empty_values():
    assert tokens.encrypt("") == ""
    assert tokens.decrypt("") == ""


@pytest.mark.django_db
def test_authtoken_stores_ciphertext_not_plaintext():
    character = EveCharacter.objects.create(character_id=42, name="Tok")
    t = AuthToken(character=character)
    t.refresh_token = "super-secret-refresh"
    t.save()
    # The DB column holds ciphertext; the property returns plaintext.
    assert t._refresh_token != "super-secret-refresh"
    assert t.refresh_token == "super-secret-refresh"
    reloaded = AuthToken.objects.get(pk=t.pk)
    assert reloaded.refresh_token == "super-secret-refresh"
    assert "super-secret-refresh" not in reloaded._refresh_token
