"""Tests for the Discord provider + OAuth account-link flow (Phase 5.1)."""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.comms_access import oauth
from apps.comms_access.models import CommsAccount, Platform
from apps.comms_access.providers import provider_class
from apps.comms_access.providers.discord import DiscordAccessProvider
from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


# --- fakes -------------------------------------------------------------------
class FakeResp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


class Recorder:
    """A stand-in for ``requests.request`` that records calls and pops queued responses."""

    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url))
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]


def _patch_http(monkeypatch, recorder):
    monkeypatch.setattr("apps.comms_access.providers.discord.requests.request", recorder)
    monkeypatch.setattr("apps.comms_access.providers.discord.time.sleep", lambda *_: None)


def _account(django_user_model, uid=8000, external_id="555"):
    u = django_user_model.objects.create(username=f"dc-{uid}")
    return CommsAccount.objects.create(
        user=u, platform=Platform.DISCORD, external_id=external_id, verified=True,
    )


def _provider(guild="42"):
    return DiscordAccessProvider({"guild_id": guild})


# --- provider: registration + config -----------------------------------------
def test_discord_provider_is_registered():
    assert provider_class("discord") is DiscordAccessProvider


def test_validate_configuration(settings):
    settings.DISCORD_BOT_TOKEN = ""
    assert _provider().validate_configuration()[0] is False
    settings.DISCORD_BOT_TOKEN = "botsecret"
    assert _provider("42").validate_configuration()[0] is True
    assert _provider("").validate_configuration()[0] is False  # missing guild


# --- provider: read_current --------------------------------------------------
@pytest.mark.django_db
def test_read_current_returns_role_ids(django_user_model, settings, monkeypatch):
    settings.DISCORD_BOT_TOKEN = "botsecret"
    acct = _account(django_user_model)
    _patch_http(monkeypatch, Recorder([FakeResp(200, {"roles": [123, 456]})]))
    assert _provider().read_current(acct) == {"123", "456"}


@pytest.mark.django_db
def test_read_current_not_in_guild_is_empty(django_user_model, settings, monkeypatch):
    settings.DISCORD_BOT_TOKEN = "botsecret"
    acct = _account(django_user_model)
    _patch_http(monkeypatch, Recorder([FakeResp(404)]))
    assert _provider().read_current(acct) == set()


# --- provider: apply ---------------------------------------------------------
@pytest.mark.django_db
def test_apply_grants_role_with_put(django_user_model, settings, monkeypatch):
    settings.DISCORD_BOT_TOKEN = "botsecret"
    acct = _account(django_user_model)
    rec = Recorder([FakeResp(204)])
    _patch_http(monkeypatch, rec)

    res = _provider().apply(acct, add={"R1"}, remove=set())

    assert res.ok and res.applied_add == {"R1"}
    assert rec.calls[0][0] == "PUT" and rec.calls[0][1].endswith("/members/555/roles/R1")


@pytest.mark.django_db
def test_apply_revokes_role_with_delete(django_user_model, settings, monkeypatch):
    settings.DISCORD_BOT_TOKEN = "botsecret"
    acct = _account(django_user_model)
    rec = Recorder([FakeResp(204)])
    _patch_http(monkeypatch, rec)

    res = _provider().apply(acct, add=set(), remove={"R1"})

    assert res.ok and res.applied_remove == {"R1"}
    assert rec.calls[0][0] == "DELETE"


@pytest.mark.django_db
def test_apply_403_is_failure_not_applied(django_user_model, settings, monkeypatch):
    # Bot role below the target role (hierarchy) ⇒ 403; the ref is not applied.
    settings.DISCORD_BOT_TOKEN = "botsecret"
    acct = _account(django_user_model)
    _patch_http(monkeypatch, Recorder([FakeResp(403)]))

    res = _provider().apply(acct, add={"R1"}, remove=set())

    assert not res.ok and res.applied_add == set() and "403" in res.error


@pytest.mark.django_db
def test_apply_honours_429_retry(django_user_model, settings, monkeypatch):
    settings.DISCORD_BOT_TOKEN = "botsecret"
    acct = _account(django_user_model)
    rec = Recorder([FakeResp(429, {"retry_after": 0.01}), FakeResp(204)])
    _patch_http(monkeypatch, rec)

    res = _provider().apply(acct, add={"R1"}, remove=set())

    assert res.ok and res.applied_add == {"R1"}
    assert len(rec.calls) == 2  # retried once after the 429


# --- provider: kick ----------------------------------------------------------
@pytest.mark.django_db
def test_kick_disabled_by_default(django_user_model, settings, monkeypatch):
    settings.DISCORD_BOT_TOKEN = "botsecret"
    acct = _account(django_user_model)
    _patch_http(monkeypatch, Recorder([FakeResp(204)]))
    res = _provider().kick(acct)  # kick_enabled not in config
    assert res.skipped and not res.ok


@pytest.mark.django_db
def test_kick_when_enabled(django_user_model, settings, monkeypatch):
    settings.DISCORD_BOT_TOKEN = "botsecret"
    acct = _account(django_user_model)
    rec = Recorder([FakeResp(204)])
    _patch_http(monkeypatch, rec)
    res = DiscordAccessProvider({"guild_id": "42", "kick_enabled": True}).kick(acct)
    assert res.ok and rec.calls[0][0] == "DELETE" and rec.calls[0][1].endswith("/members/555")


# --- OAuth helper ------------------------------------------------------------
def test_authorize_url_contains_params(settings):
    settings.DISCORD_OAUTH_CLIENT_ID = "cid"
    settings.DISCORD_OAUTH_CALLBACK_URL = "https://grid.example.com/comms/discord/callback/"
    url = oauth.build_authorize_url("STATE123", "CHALLENGE")
    assert "client_id=cid" in url and "state=STATE123" in url
    assert "code_challenge=CHALLENGE" in url and "code_challenge_method=S256" in url


def test_display_handle_new_and_legacy():
    assert oauth.display_handle({"id": "1", "username": "Pilot", "discriminator": "0"}) == "Pilot"
    assert oauth.display_handle({"id": "1", "username": "Pilot", "discriminator": "1234"}) == "Pilot#1234"


# --- link flow (views) -------------------------------------------------------
def _member(django_user_model, uid=8100):
    u = django_user_model.objects.create(username=f"dcm-{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=uid, user=u, name="M", is_main=True, is_corp_member=True)
    return u


@pytest.mark.django_db
def test_connect_page_renders(client, django_user_model):
    client.force_login(_member(django_user_model))
    assert client.get(reverse("comms_access:connect")).status_code == 200


@pytest.mark.django_db
def test_begin_redirects_and_stores_verifier(client, django_user_model, settings):
    settings.DISCORD_OAUTH_ENABLED = True
    settings.DISCORD_OAUTH_CLIENT_ID = "cid"
    client.force_login(_member(django_user_model))
    resp = client.post(reverse("comms_access:discord_begin"))
    assert resp.status_code == 302 and resp["Location"].startswith(oauth.AUTHORIZE_URL)
    assert any(k.startswith("comms_discord_pkce:") for k in client.session.keys())


@pytest.mark.django_db
def test_callback_links_account(client, django_user_model, settings, monkeypatch):
    settings.DISCORD_OAUTH_ENABLED = True
    monkeypatch.setattr("apps.comms_access.oauth.exchange_code", lambda code, v: {"access_token": "x"})
    monkeypatch.setattr(
        "apps.comms_access.oauth.fetch_identity",
        lambda t: {"id": "999", "username": "Ace", "discriminator": "0"},
    )
    user = _member(django_user_model)
    client.force_login(user)
    session = client.session
    session["comms_discord_pkce:S1"] = "verifier"
    session.save()

    resp = client.get(reverse("comms_access:discord_callback"), {"state": "S1", "code": "C1"})

    assert resp.status_code == 302
    acct = CommsAccount.objects.get(user=user, platform="discord")
    assert acct.external_id == "999" and acct.verified and acct.external_handle == "Ace"


@pytest.mark.django_db
def test_callback_rejects_missing_state(client, django_user_model, settings):
    settings.DISCORD_OAUTH_ENABLED = True
    user = _member(django_user_model)
    client.force_login(user)
    # No verifier seeded ⇒ CSRF/state check fails ⇒ no account created.
    resp = client.get(reverse("comms_access:discord_callback"), {"state": "S1", "code": "C1"})
    assert resp.status_code == 302
    assert not CommsAccount.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_callback_rejects_account_claimed_by_another(client, django_user_model, settings, monkeypatch):
    settings.DISCORD_OAUTH_ENABLED = True
    monkeypatch.setattr("apps.comms_access.oauth.exchange_code", lambda code, v: {"access_token": "x"})
    monkeypatch.setattr(
        "apps.comms_access.oauth.fetch_identity", lambda t: {"id": "777", "username": "Dup"},
    )
    other = django_user_model.objects.create(username="dc-owner")
    CommsAccount.objects.create(user=other, platform=Platform.DISCORD, external_id="777", verified=True)

    user = _member(django_user_model)
    client.force_login(user)
    session = client.session
    session["comms_discord_pkce:S2"] = "verifier"
    session.save()

    client.get(reverse("comms_access:discord_callback"), {"state": "S2", "code": "C1"})
    assert not CommsAccount.objects.filter(user=user).exists()  # clash ⇒ not linked


@pytest.mark.django_db
def test_reconcile_resolves_discord_provider_from_registry(django_user_model, settings, monkeypatch):
    """End-to-end: config-armed discord + registry provider + mapping ⇒ role granted via PUT."""
    from apps.comms_access import config
    from apps.comms_access.models import EntitlementMapping, MappingMode
    from apps.comms_access.reconcile import reconcile_account

    settings.DISCORD_BOT_TOKEN = "botsecret"
    config.set("general", {"enabled": True, "global_dry_run": False, "revoke_grace_minutes": 0})
    config.set("platforms", {"discord": {"armed": True, "guild_id": "42", "kick_enabled": False}})
    u = _member(django_user_model, 8300)
    acct = CommsAccount.objects.create(
        user=u, platform=Platform.DISCORD, external_id="555", verified=True,
    )
    EntitlementMapping.objects.create(
        platform="discord", entitlement_key="member", target_ref="R_MEM",
        mode=MappingMode.ADDITIVE, dry_run=False, enabled=True,
    )
    # GET member (no roles) then PUT role → 204.
    _patch_http(monkeypatch, Recorder([FakeResp(200, {"roles": []}), FakeResp(204)]))

    # No provider passed ⇒ the engine resolves DiscordAccessProvider from the registry.
    res = reconcile_account(acct, source_ref="int1")

    assert res.added == {"R_MEM"}


@pytest.mark.django_db
def test_unlink_deletes_account(client, django_user_model):
    user = _member(django_user_model)
    CommsAccount.objects.create(user=user, platform=Platform.DISCORD, external_id="1", verified=True)
    client.force_login(user)
    resp = client.post(reverse("comms_access:discord_unlink"))
    assert resp.status_code == 302
    assert not CommsAccount.objects.filter(user=user, platform="discord").exists()
