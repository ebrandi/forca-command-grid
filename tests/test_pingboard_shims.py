"""Phase 1 — the compatibility shims route legacy Discord/EVE-mail through Pingboard.

Proves the *migration* works (traffic flows through Pingboard's providers when
configured) and stays behaviour-identical (legacy fallback + truthful int + no-op).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.pingboard import compat
from apps.pingboard.models import ChannelProvider
from apps.recommendations import notify
from apps.sso.models import AuthToken, EveCharacter


class FakeResp:
    def __init__(self, status=204, data=None):
        self.status_code = status
        self.data = data


@pytest.mark.django_db
def test_broadcast_discord_routes_through_pingboard_when_configured(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, allow_redirects=None):
        captured.update(url=url, json=json, allow_redirects=allow_redirects)
        return FakeResp(204)

    monkeypatch.setattr("apps.pingboard.providers.discord.requests.post", fake_post)
    # legacy sink must NOT be used when a Pingboard provider exists
    legacy_calls = []
    monkeypatch.setattr(notify, "_post_discord", lambda url, content: legacy_calls.append(url))

    p = ChannelProvider(kind="discord", label="corp", enabled=True)
    p.secret = "https://discord.com/api/webhooks/1/tok"
    p.save()

    n = notify.broadcast_discord("hello fleet")
    assert n == 1
    assert captured["url"] == "https://discord.com/api/webhooks/1/tok"
    assert captured["allow_redirects"] is False
    assert captured["json"]["allowed_mentions"] == {"parse": []}
    assert legacy_calls == []  # routed through Pingboard, not the legacy path


@pytest.mark.django_db
def test_broadcast_discord_is_noop_without_any_provider():
    """With the legacy NotificationChannel path retired, an unconfigured corp simply
    delivers to nothing (returns 0) rather than falling back to a legacy registry."""
    assert notify.broadcast_discord("nobody home") == 0


@pytest.mark.django_db
def test_broadcast_text_reaches_multiple_channel_kinds(monkeypatch):
    """broadcast_text is not Discord-only — it posts to every armed chat kind."""
    from apps.pingboard.services import broadcast_text

    dposts = []
    monkeypatch.setattr(
        "apps.pingboard.providers.discord.requests.post",
        lambda url, json=None, timeout=None, allow_redirects=None: dposts.append(url) or FakeResp(204),
    )
    tposts = []

    class _TgResp:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"message_id": 7}}

    def fake_tg_post(url, allowed, *, json=None, headers=None, timeout=10):
        tposts.append(json["chat_id"])
        return _TgResp(), ""

    monkeypatch.setattr("apps.pingboard.providers.telegram.post_json", fake_tg_post)

    d = ChannelProvider(kind="discord", label="d", enabled=True)
    d.secret = "https://discord.com/api/webhooks/1/tok"
    d.save()
    t = ChannelProvider(kind="telegram", label="t", enabled=True, routing={"chat_id": "-100"})
    t.secret = "bottoken"
    t.save()

    assert broadcast_text("hello fleet") == 2
    assert dposts == ["https://discord.com/api/webhooks/1/tok"]
    assert tposts == ["-100"]


@pytest.mark.django_db
def test_broadcast_text_honours_classification_ceiling(monkeypatch):
    """Fail-safe ceiling: a blank/corp_internal channel never carries officer-tier intel;
    a director must raise a channel to high_command for it to receive that tier."""
    from apps.pingboard.services import broadcast_text

    posts = []
    monkeypatch.setattr(
        "apps.pingboard.providers.discord.requests.post",
        lambda url, json=None, timeout=None, allow_redirects=None: posts.append(url) or FakeResp(204),
    )
    officers = ChannelProvider(kind="discord", label="officers", enabled=True,
                               max_classification="high_command")
    officers.secret = "https://discord.com/api/webhooks/1/officer"
    officers.save()
    corp = ChannelProvider(kind="discord", label="corp", enabled=True)  # blank = corp_internal
    corp.secret = "https://discord.com/api/webhooks/2/corp"
    corp.save()

    # high_command intel reaches ONLY the explicitly-raised officer channel.
    assert broadcast_text("intel", classification="high_command") == 1
    assert posts == ["https://discord.com/api/webhooks/1/officer"]

    # corp_internal reaches both.
    posts.clear()
    assert broadcast_text("notice", classification="corp_internal") == 2
    assert set(posts) == {"https://discord.com/api/webhooks/1/officer",
                          "https://discord.com/api/webhooks/2/corp"}

    # An unclassified (None) service broadcast is treated as corp_internal → reaches both.
    posts.clear()
    assert broadcast_text("fleet up") == 2


@pytest.mark.django_db
def test_send_eve_mail_compat_delivers(monkeypatch, django_user_model):
    user = django_user_model.objects.create(username="u2001")
    ch = EveCharacter.objects.create(character_id=2001, name="Sender", is_main=True,
                                     is_corp_member=True, user=user)
    AuthToken.objects.create(character=ch, scopes=["esi-mail.send_mail.v1"])

    calls = {}

    class FakeClient:
        def post(self, path, *, json=None, token=None):
            calls.update(path=path, json=json, token=token)
            return SimpleNamespace(status=201, data=77)

    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda c, s: "tok")
    monkeypatch.setattr("core.esi.client.get_client", lambda: FakeClient())

    assert compat.send_eve_mail("Subject", "Body", [3001, 3002], 2001) is True
    assert calls["path"] == "/characters/2001/mail/"
    assert calls["json"]["recipients"] == [
        {"recipient_id": 3001, "recipient_type": "character"},
        {"recipient_id": 3002, "recipient_type": "character"},
    ]


@pytest.mark.django_db
def test_send_eve_mail_compat_no_sender_is_noop():
    assert compat.send_eve_mail("s", "b", [3001], None) is False
    assert compat.send_eve_mail("s", "b", [], 2001) is False


@pytest.mark.django_db
def test_readiness_send_mail_flows_through_provider(monkeypatch, django_user_model):
    """The readiness shim keeps its own sender config but delivers via Pingboard."""
    from apps.readiness import config as rconfig
    from apps.readiness import mail as rmail

    user = django_user_model.objects.create(username="u2001")
    ch = EveCharacter.objects.create(character_id=2001, name="Sender", is_main=True,
                                     is_corp_member=True, user=user)
    AuthToken.objects.create(character=ch, scopes=["esi-mail.send_mail.v1"])
    rconfig.set("notifications", {"eve_mail_sender_character_id": 2001})

    seen = {}

    class FakeClient:
        def post(self, path, *, json=None, token=None):
            seen["path"] = path
            return SimpleNamespace(status=201, data=1)

    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda c, s: "tok")
    monkeypatch.setattr("core.esi.client.get_client", lambda: FakeClient())

    assert rmail.send_mail("s", "b", [3001]) is True
    assert seen["path"] == "/characters/2001/mail/"
