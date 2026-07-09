"""Tests for console-managed platform credentials (Phase 5.2 — no-.env configuration).

Covers the encrypted-at-rest ``PlatformCredential`` model, the console-first / env-fallback
resolver, the effective ``feature_active`` gate, and the Director-gated credentials form
(write-only secrets: blank keeps, clear wipes, values never echoed or logged).
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.admin_audit.models import AuditLog
from apps.comms_access import config, credentials, oauth
from apps.comms_access.models import Platform, PlatformCredential
from apps.comms_access.providers.discord import DiscordAccessProvider
from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


# --- model: encryption at rest -----------------------------------------------
@pytest.mark.django_db
def test_secret_is_encrypted_and_roundtrips():
    cred = PlatformCredential(platform=Platform.DISCORD)
    cred.bot_token = "super-secret-token"
    cred.oauth_client_secret = "oauth-secret"
    cred.save()

    cred.refresh_from_db()
    assert cred.bot_token == "super-secret-token"
    assert cred.oauth_client_secret == "oauth-secret"
    # Ciphertext at rest — the plaintext never appears in the stored column.
    assert "super-secret-token" not in cred._bot_token
    assert cred.has_bot_token and cred.has_oauth_client_secret


@pytest.mark.django_db
def test_clearing_a_secret_reports_unconfigured():
    cred = PlatformCredential(platform=Platform.DISCORD)
    cred.bot_token = "x"
    cred.save()
    assert cred.has_bot_token
    cred.bot_token = ""
    cred.save()
    assert not cred.has_bot_token
    assert cred._bot_token == ""  # stored literally empty, not empty-ciphertext


# --- resolver precedence -----------------------------------------------------
@pytest.mark.django_db
def test_console_token_wins_over_env(settings):
    settings.DISCORD_BOT_TOKEN = "env-token"
    cred = PlatformCredential(platform=Platform.DISCORD)
    cred.bot_token = "console-token"
    cred.save()
    assert credentials.discord_bot_token() == "console-token"


@pytest.mark.django_db
def test_env_token_is_fallback(settings):
    settings.DISCORD_BOT_TOKEN = "env-token"
    assert credentials.discord_bot_token() == "env-token"  # no console row


@pytest.mark.django_db
def test_oauth_enabled_from_console_only(settings):
    settings.DISCORD_OAUTH_CLIENT_ID = ""
    settings.DISCORD_OAUTH_CLIENT_SECRET = ""
    settings.DISCORD_OAUTH_ENABLED = False
    assert oauth.enabled() is False
    cred = PlatformCredential(platform=Platform.DISCORD, oauth_client_id="cid")
    cred.oauth_client_secret = "sec"
    cred.save()
    assert credentials.discord_oauth_enabled() is True
    assert oauth.enabled() is True


@pytest.mark.django_db
def test_provider_reads_console_token(settings, django_user_model):
    settings.DISCORD_BOT_TOKEN = "env-token"
    cred = PlatformCredential(platform=Platform.DISCORD)
    cred.bot_token = "console-token"
    cred.save()
    prov = DiscordAccessProvider({"guild_id": "42"})
    assert prov._token() == "console-token"
    assert prov.validate_configuration()[0] is True


# --- feature_active gate -----------------------------------------------------
@pytest.mark.django_db
def test_feature_active_requires_env_and_config(settings):
    settings.COMMS_ACCESS_ENABLED = True
    assert config.feature_active() is False  # config still disabled by default
    config.set("general", {"enabled": True, "global_dry_run": True, "revoke_grace_minutes": 0})
    assert config.feature_active() is True
    settings.COMMS_ACCESS_ENABLED = False  # ops hard kill switch wins
    assert config.feature_active() is False


# --- console credentials form (Director-gated) -------------------------------
def _director(django_user_model, uid=9600):
    u = django_user_model.objects.create(username=f"cred-{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_DIRECTOR))
    EveCharacter.objects.create(character_id=uid, user=u, name=f"Dir{uid}",
                                is_main=True, is_corp_member=True)
    return u


@pytest.mark.django_db
def test_credentials_denied_for_member(client, django_user_model):
    u = django_user_model.objects.create(username="cred-member")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(u)
    resp = client.post(reverse("admin_audit:comms_access_settings"), {
        "domain": "credentials", "discord_bot_token": "hacktoken",
    })
    assert resp.status_code == 403
    assert not PlatformCredential.objects.filter(platform="discord").exists()


@pytest.mark.django_db
def test_credentials_save_stores_encrypted(client, django_user_model):
    client.force_login(_director(django_user_model))
    resp = client.post(reverse("admin_audit:comms_access_settings"), {
        "domain": "credentials",
        "discord_bot_token": "the-bot-token",
        "discord_client_id": "cid-123",
        "discord_client_secret": "the-client-secret",
        "discord_callback_url": "https://grid.example.com/comms/discord/callback/",
    })
    assert resp.status_code == 302
    cred = PlatformCredential.objects.get(platform="discord")
    assert cred.oauth_client_id == "cid-123"
    assert cred.bot_token == "the-bot-token"
    assert cred.oauth_client_secret == "the-client-secret"
    # The secret is never written into the audit log.
    entry = AuditLog.objects.filter(action="comms_access.credentials.update").first()
    assert entry is not None
    assert "the-bot-token" not in str(entry.metadata)
    assert "the-client-secret" not in str(entry.metadata)


@pytest.mark.django_db
def test_blank_secret_keeps_existing(client, django_user_model):
    cred = PlatformCredential(platform=Platform.DISCORD, oauth_client_id="old")
    cred.bot_token = "keep-me"
    cred.save()
    client.force_login(_director(django_user_model))
    client.post(reverse("admin_audit:comms_access_settings"), {
        "domain": "credentials", "discord_client_id": "new", "discord_bot_token": "",
    })
    cred.refresh_from_db()
    assert cred.oauth_client_id == "new"
    assert cred.bot_token == "keep-me"  # blank submission preserved the stored token


@pytest.mark.django_db
def test_clear_checkbox_wipes_secret(client, django_user_model):
    cred = PlatformCredential(platform=Platform.DISCORD)
    cred.bot_token = "wipe-me"
    cred.save()
    client.force_login(_director(django_user_model))
    client.post(reverse("admin_audit:comms_access_settings"), {
        "domain": "credentials", "clear_bot_token": "on",
    })
    cred.refresh_from_db()
    assert not cred.has_bot_token


@pytest.mark.django_db
def test_settings_page_shows_credential_status(client, django_user_model):
    cred = PlatformCredential(platform=Platform.DISCORD)
    cred.bot_token = "SEKRET-BOT-VALUE-XYZ"
    cred.save()
    client.force_login(_director(django_user_model))
    resp = client.get(reverse("admin_audit:comms_access_settings"))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Discord credentials" in body
    assert "configured" in body  # status shown, secret value not echoed
    assert "SEKRET-BOT-VALUE-XYZ" not in body
