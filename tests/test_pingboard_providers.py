"""Phase 2 — Slack / Telegram / WhatsApp adapters + DM dispatch + SSRF allowlist."""
from __future__ import annotations

import pytest

from apps.pingboard import services
from apps.pingboard.models import AlertStatus, ChannelProvider, DeliveryStatus, PilotContactChannel
from apps.pingboard.providers import Recipient
from apps.pingboard.providers._http import host_allowed
from apps.pingboard.providers.slack import SlackProvider
from apps.pingboard.providers.telegram import TelegramProvider
from apps.pingboard.providers.whatsapp import WhatsAppProvider


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _mock_http(monkeypatch, resp, capture=None):
    def fake_post(url, json=None, headers=None, data=None, auth=None, timeout=None, allow_redirects=None):
        if capture is not None:
            capture.update(url=url, json=json, headers=headers, data=data, allow_redirects=allow_redirects)
        return resp

    monkeypatch.setattr("apps.pingboard.providers._http.requests.post", fake_post)


# --- SSRF allowlist ----------------------------------------------------------
def test_host_allowed_guard():
    assert host_allowed("https://api.telegram.org/botX/sendMessage", ["api.telegram.org"])
    assert not host_allowed("https://evil.example.com/x", ["api.telegram.org"])
    assert not host_allowed("http://api.telegram.org/x", ["api.telegram.org"])  # http refused


# --- Slack -------------------------------------------------------------------
@pytest.mark.django_db
def test_slack_bot_dm(monkeypatch, settings):
    settings.PINGBOARD_SLACK_BOT_TOKEN = "xoxb-test"
    settings.PINGBOARD_SLACK_ENABLED = True
    cap = {}
    _mock_http(monkeypatch, Resp(200, {"ok": True, "ts": "1699.1"}), cap)
    res = SlackProvider(None).send(subject="", body="hi",
                                   recipients=[Recipient("slack", "slack_user", "U123")])
    assert res.ok and res.provider_message_id == "1699.1"
    assert cap["url"] == "https://slack.com/api/chat.postMessage"
    assert cap["allow_redirects"] is False
    assert cap["headers"]["Authorization"] == "Bearer xoxb-test"


@pytest.mark.django_db
def test_slack_logical_error_is_failure(monkeypatch, settings):
    settings.PINGBOARD_SLACK_BOT_TOKEN = "xoxb-test"
    settings.PINGBOARD_SLACK_ENABLED = True
    _mock_http(monkeypatch, Resp(200, {"ok": False, "error": "channel_not_found"}))
    res = SlackProvider(None).send(subject="", body="hi",
                                   recipients=[Recipient("slack", "slack_user", "U1")])
    assert res.ok is False


@pytest.mark.django_db
def test_slack_disabled_is_skipped(settings):
    settings.PINGBOARD_SLACK_BOT_TOKEN = ""
    settings.PINGBOARD_SLACK_ENABLED = False
    res = SlackProvider(None).send(subject="", body="hi",
                                   recipients=[Recipient("slack", "slack_user", "U1")])
    assert res.skipped is True and res.ok is False


@pytest.mark.django_db
def test_slack_webhook_mode(monkeypatch, settings):
    cap = {}
    _mock_http(monkeypatch, Resp(200), cap)
    row = ChannelProvider(kind="slack", label="wh")
    row.secret = "https://hooks.slack.com/services/T/B/x"
    res = SlackProvider(row).send(subject="", body="hi", recipients=[])
    assert res.ok
    assert cap["url"] == "https://hooks.slack.com/services/T/B/x"
    assert cap["json"] == {"text": "hi"}


# --- Telegram ----------------------------------------------------------------
@pytest.mark.django_db
def test_telegram_send(monkeypatch, settings):
    settings.PINGBOARD_TELEGRAM_BOT_TOKEN = "123:abc"
    settings.PINGBOARD_TELEGRAM_ENABLED = True
    cap = {}
    _mock_http(monkeypatch, Resp(200, {"ok": True, "result": {"message_id": 42}}), cap)
    res = TelegramProvider(None).send(subject="", body="hi",
                                      recipients=[Recipient("telegram", "chat", "555")])
    assert res.ok and res.provider_message_id == "42"
    assert cap["url"] == "https://api.telegram.org/bot123:abc/sendMessage"
    assert cap["json"] == {"chat_id": "555", "text": "hi"}


@pytest.mark.django_db
def test_telegram_host_allowlist_blocks_send(monkeypatch, settings):
    settings.PINGBOARD_TELEGRAM_BOT_TOKEN = "123:abc"
    settings.PINGBOARD_TELEGRAM_ENABLED = True
    settings.PINGBOARD_TELEGRAM_ALLOWED_HOSTS = ["example.com"]  # not api.telegram.org
    _mock_http(monkeypatch, Resp(200, {"ok": True}))
    res = TelegramProvider(None).send(subject="", body="hi",
                                      recipients=[Recipient("telegram", "chat", "555")])
    assert res.ok is False  # refused: outbound host not allowlisted


@pytest.mark.django_db
def test_telegram_disabled_is_skipped(settings):
    settings.PINGBOARD_TELEGRAM_BOT_TOKEN = ""
    settings.PINGBOARD_TELEGRAM_ENABLED = False
    res = TelegramProvider(None).send(subject="", body="hi",
                                      recipients=[Recipient("telegram", "chat", "5")])
    assert res.skipped is True


# --- WhatsApp ----------------------------------------------------------------
@pytest.mark.django_db
def test_whatsapp_meta(monkeypatch, settings):
    settings.PINGBOARD_WHATSAPP_BACKEND = "meta"
    settings.PINGBOARD_WHATSAPP_META_TOKEN = "tok"
    settings.PINGBOARD_WHATSAPP_META_PHONE_ID = "PHONE"
    settings.PINGBOARD_WHATSAPP_ENABLED = True
    cap = {}
    _mock_http(monkeypatch, Resp(200, {"messages": [{"id": "wamid.1"}]}), cap)
    res = WhatsAppProvider(None).send(subject="", body="hi",
                                      recipients=[Recipient("whatsapp", "phone", "+15551234")])
    assert res.ok and res.provider_message_id == "wamid.1"
    assert "graph.facebook.com" in cap["url"]
    assert cap["json"]["to"] == "+15551234"


@pytest.mark.django_db
def test_whatsapp_twilio(monkeypatch, settings):
    settings.PINGBOARD_WHATSAPP_BACKEND = "twilio"
    settings.PINGBOARD_WHATSAPP_TWILIO_SID = "AC1"
    settings.PINGBOARD_WHATSAPP_TWILIO_TOKEN = "tok"
    settings.PINGBOARD_WHATSAPP_TWILIO_FROM = "+15550000"
    settings.PINGBOARD_WHATSAPP_ENABLED = True
    cap = {}
    _mock_http(monkeypatch, Resp(201, {"sid": "SM1"}), cap)
    res = WhatsAppProvider(None).send(subject="", body="hi",
                                      recipients=[Recipient("whatsapp", "phone", "+15551234")])
    assert res.ok and res.provider_message_id == "SM1"
    assert cap["data"]["To"] == "whatsapp:+15551234"


@pytest.mark.django_db
def test_whatsapp_disabled_is_skipped(settings):
    settings.PINGBOARD_WHATSAPP_BACKEND = "none"
    settings.PINGBOARD_WHATSAPP_ENABLED = False
    res = WhatsAppProvider(None).send(subject="", body="hi",
                                      recipients=[Recipient("whatsapp", "phone", "+1")])
    assert res.skipped is True


# --- fully-UI config: credentials on the channel row, no env ----------------
@pytest.mark.django_db
def test_telegram_uses_row_token_without_env(monkeypatch, settings):
    settings.PINGBOARD_TELEGRAM_BOT_TOKEN = ""
    settings.PINGBOARD_TELEGRAM_ENABLED = False
    cap = {}
    _mock_http(monkeypatch, Resp(200, {"ok": True, "result": {"message_id": 9}}), cap)
    row = ChannelProvider(kind="telegram", label="grp", enabled=True, routing={"chat_id": "-100777"})
    row.secret = "ROWTOKEN:abc"
    row.save()
    res = TelegramProvider(row).send(subject="", body="hi", recipients=[])
    assert res.ok and res.provider_message_id == "9"
    assert cap["url"] == "https://api.telegram.org/botROWTOKEN:abc/sendMessage"
    assert cap["json"] == {"chat_id": "-100777", "text": "hi"}


@pytest.mark.django_db
def test_telegram_dm_falls_back_to_enabled_row_token(monkeypatch, settings):
    # No env token, but a configured Telegram channel exists → the global-token DM
    # path (provider=None) reuses that row's stored token.
    settings.PINGBOARD_TELEGRAM_BOT_TOKEN = ""
    settings.PINGBOARD_TELEGRAM_ENABLED = False
    row = ChannelProvider(kind="telegram", label="grp", enabled=True, routing={"chat_id": "-1"})
    row.secret = "FALLBACK:tok"
    row.save()
    cap = {}
    _mock_http(monkeypatch, Resp(200, {"ok": True, "result": {"message_id": 3}}), cap)
    res = TelegramProvider(None).send(subject="", body="hi",
                                      recipients=[Recipient("telegram", "chat", "555")])
    assert res.ok
    assert "botFALLBACK:tok" in cap["url"]
    assert cap["json"]["chat_id"] == "555"


@pytest.mark.django_db
def test_telegram_validate_row():
    row = ChannelProvider(kind="telegram", label="g", routing={"chat_id": "-1"})
    row.secret = "T"
    assert TelegramProvider(row).validate_configuration()[0] is True
    no_chat = ChannelProvider(kind="telegram", label="g2")  # token, no chat id
    no_chat.secret = "T"
    ok, why = TelegramProvider(no_chat).validate_configuration()
    assert not ok and "chat id" in why.lower()


@pytest.mark.django_db
def test_telegram_validate_no_token_no_env(settings):
    settings.PINGBOARD_TELEGRAM_BOT_TOKEN = ""
    settings.PINGBOARD_TELEGRAM_ENABLED = False
    row = ChannelProvider(kind="telegram", label="g", routing={"chat_id": "-1"})  # no secret
    ok, why = TelegramProvider(row).validate_configuration()
    assert not ok and "token" in why.lower()


@pytest.mark.django_db
def test_whatsapp_meta_from_row_without_env(monkeypatch, settings):
    settings.PINGBOARD_WHATSAPP_BACKEND = "none"
    settings.PINGBOARD_WHATSAPP_META_TOKEN = ""
    settings.PINGBOARD_WHATSAPP_META_PHONE_ID = ""
    settings.PINGBOARD_WHATSAPP_ENABLED = False
    row = ChannelProvider(kind="whatsapp", label="wa", enabled=True,
                          routing={"backend": "meta", "meta_phone_id": "PHONE9", "to": "+15551230000"})
    row.secret = "META-TOKEN"
    row.save()
    cap = {}
    _mock_http(monkeypatch, Resp(200, {"messages": [{"id": "wamid.9"}]}), cap)
    res = WhatsAppProvider(row).send(subject="", body="hi", recipients=[])
    assert res.ok and res.provider_message_id == "wamid.9"
    assert "graph.facebook.com/v21.0/PHONE9/messages" in cap["url"]
    assert cap["headers"]["Authorization"] == "Bearer META-TOKEN"
    assert cap["json"]["to"] == "+15551230000"


@pytest.mark.django_db
def test_whatsapp_twilio_from_row_without_env(monkeypatch, settings):
    settings.PINGBOARD_WHATSAPP_BACKEND = "none"
    settings.PINGBOARD_WHATSAPP_TWILIO_SID = ""
    settings.PINGBOARD_WHATSAPP_TWILIO_TOKEN = ""
    settings.PINGBOARD_WHATSAPP_TWILIO_FROM = ""
    settings.PINGBOARD_WHATSAPP_ENABLED = False
    row = ChannelProvider(kind="whatsapp", label="wa", enabled=True,
                          routing={"backend": "twilio", "twilio_sid": "AC9",
                                   "twilio_from": "+15550000", "to": "+15551111"})
    row.secret = "TW-TOKEN"
    row.save()
    cap = {}
    _mock_http(monkeypatch, Resp(201, {"sid": "SM9"}), cap)
    res = WhatsAppProvider(row).send(subject="", body="hi", recipients=[])
    assert res.ok and res.provider_message_id == "SM9"
    assert "api.twilio.com/2010-04-01/Accounts/AC9/Messages.json" in cap["url"]
    assert cap["data"]["To"] == "whatsapp:+15551111"
    assert cap["data"]["From"] == "whatsapp:+15550000"


@pytest.mark.django_db
def test_whatsapp_validate_row(settings):
    settings.PINGBOARD_WHATSAPP_BACKEND = "none"
    settings.PINGBOARD_WHATSAPP_META_TOKEN = ""
    settings.PINGBOARD_WHATSAPP_META_PHONE_ID = ""
    settings.PINGBOARD_WHATSAPP_ENABLED = False
    good = ChannelProvider(kind="whatsapp", label="m", routing={"backend": "meta", "meta_phone_id": "P"})
    good.secret = "TOK"
    assert WhatsAppProvider(good).validate_configuration()[0] is True
    no_phone = ChannelProvider(kind="whatsapp", label="m2", routing={"backend": "meta"})
    no_phone.secret = "TOK"
    ok, why = WhatsAppProvider(no_phone).validate_configuration()
    assert not ok and "phone" in why.lower()
    no_backend = ChannelProvider(kind="whatsapp", label="m3", routing={})
    ok2, why2 = WhatsAppProvider(no_backend).validate_configuration()
    assert not ok2 and "backend" in why2.lower()


# --- DM dispatch through the full pipeline -----------------------------------
@pytest.mark.django_db
def test_dispatch_telegram_dm(monkeypatch, settings, user, character):
    settings.PINGBOARD_TELEGRAM_BOT_TOKEN = "123:abc"
    settings.PINGBOARD_TELEGRAM_ENABLED = True
    _mock_http(monkeypatch, Resp(200, {"ok": True, "result": {"message_id": 7}}))
    PilotContactChannel.objects.create(user=user, kind="telegram", handle="999", verified=True)

    a = services.emit_alert(category="announcement", title="t", body="b",
                            channels=["telegram"], audience={"kind": "corp"})
    services.dispatch_alert(a.id)
    a.refresh_from_db()
    assert a.status == AlertStatus.SENT
    d = a.deliveries.get(kind="telegram", provider__isnull=True)
    assert d.status == DeliveryStatus.DELIVERED


@pytest.mark.django_db
def test_dispatch_telegram_no_linked_pilot_is_skipped(settings, user, character):
    settings.PINGBOARD_TELEGRAM_BOT_TOKEN = "123:abc"
    settings.PINGBOARD_TELEGRAM_ENABLED = True
    # no verified PilotContactChannel → no recipients, no provider row → skipped
    a = services.emit_alert(category="announcement", title="t", body="b",
                            channels=["telegram"], audience={"kind": "corp"})
    services.dispatch_alert(a.id)
    a.refresh_from_db()
    assert a.deliveries.get(kind="telegram").status == DeliveryStatus.SKIPPED
