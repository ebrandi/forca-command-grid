"""Phase 2 — per-pilot identity linking + the inbound Telegram verify webhook."""
from __future__ import annotations

import json

import pytest
from django.urls import reverse

from apps.pingboard import linking
from apps.pingboard.models import PilotContactChannel


@pytest.mark.django_db
def test_start_and_verify_by_code(user):
    row = linking.start_link(user, "telegram")
    assert row.verified is False and row.verify_code
    code = row.verify_code

    done = linking.verify_by_code("telegram", code, "chat-123")
    assert done is not None and done.verified is True
    assert done.handle == "chat-123" and done.verify_code == ""
    # code is single-use — a replay finds nothing
    assert linking.verify_by_code("telegram", code, "chat-999") is None


@pytest.mark.django_db
def test_confirm_own_code(user):
    row = linking.start_link(user, "slack", handle="U777")
    assert linking.confirm(user, "slack", "wrong") is False
    assert linking.confirm(user, "slack", row.verify_code) is True
    row.refresh_from_db()
    assert row.verified is True and row.handle == "U777"


@pytest.mark.django_db
def test_unlink_and_bad_kind(user):
    linking.start_link(user, "telegram")
    assert linking.unlink(user, "telegram") >= 1
    assert not PilotContactChannel.objects.filter(user=user, kind="telegram").exists()
    with pytest.raises(ValueError):
        linking.start_link(user, "not_a_channel")


def test_telegram_deeplink(settings):
    settings.PINGBOARD_TELEGRAM_BOT_USERNAME = "forca_bot"
    assert linking.telegram_deeplink("abcd") == "https://t.me/forca_bot?start=abcd"
    settings.PINGBOARD_TELEGRAM_BOT_USERNAME = ""
    assert linking.telegram_deeplink("abcd") == ""


# --- inbound webhook ---------------------------------------------------------
@pytest.mark.django_db
def test_webhook_verifies_via_start(client, settings, user):
    settings.PINGBOARD_TELEGRAM_WEBHOOK_SECRET = "s3cret"
    row = linking.start_link(user, "telegram")
    url = reverse("pingboard:telegram_webhook", args=["s3cret"])
    payload = {"message": {"text": f"/start {row.verify_code}", "chat": {"id": 424242}}}
    resp = client.post(url, data=json.dumps(payload), content_type="application/json")
    assert resp.status_code == 200
    row.refresh_from_db()
    assert row.verified is True and row.handle == "424242"


@pytest.mark.django_db
def test_webhook_rejects_bad_secret(client, settings):
    settings.PINGBOARD_TELEGRAM_WEBHOOK_SECRET = "s3cret"
    url = reverse("pingboard:telegram_webhook", args=["wrong"])
    resp = client.post(url, data="{}", content_type="application/json")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_webhook_ignores_malformed_and_non_start(client, settings, user):
    settings.PINGBOARD_TELEGRAM_WEBHOOK_SECRET = "s3cret"
    linking.start_link(user, "telegram")
    url = reverse("pingboard:telegram_webhook", args=["s3cret"])
    # malformed body → still 200, no crash
    assert client.post(url, data="not json", content_type="application/json").status_code == 200
    # a non-/start message verifies nothing
    payload = {"message": {"text": "hello", "chat": {"id": 1}}}
    assert client.post(url, data=json.dumps(payload), content_type="application/json").status_code == 200
    assert not PilotContactChannel.objects.filter(user=user, verified=True).exists()


@pytest.mark.django_db
def test_webhook_accepts_header_secret(client, settings, user):
    """L4: Telegram's X-Telegram-Bot-Api-Secret-Token header is honoured (keeps the
    secret out of the URL/access logs); it takes precedence over the path segment."""
    settings.PINGBOARD_TELEGRAM_WEBHOOK_SECRET = "s3cret"
    row = linking.start_link(user, "telegram")
    url = reverse("pingboard:telegram_webhook", args=["ignored"])
    payload = {"message": {"text": f"/start {row.verify_code}", "chat": {"id": 55}}}
    resp = client.post(url, data=json.dumps(payload), content_type="application/json",
                       headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"})
    assert resp.status_code == 200
    row.refresh_from_db()
    assert row.verified is True and row.handle == "55"


@pytest.mark.django_db
def test_webhook_fails_closed_when_secret_unset(client, settings):
    """L4: with no configured secret the endpoint refuses everything (never open)."""
    settings.PINGBOARD_TELEGRAM_WEBHOOK_SECRET = ""
    url = reverse("pingboard:telegram_webhook", args=["anything"])
    assert client.post(url, data="{}", content_type="application/json").status_code == 403


@pytest.mark.django_db
def test_webhook_non_ascii_secret_is_403_not_500(client, settings):
    """L4 regression: a non-ASCII secret header must yield a clean 403, not a
    TypeError-500 from comparing non-ASCII strs (compare happens on bytes)."""
    settings.PINGBOARD_TELEGRAM_WEBHOOK_SECRET = "s3cret"
    url = reverse("pingboard:telegram_webhook", args=["x"])
    resp = client.post(url, data="{}", content_type="application/json",
                       headers={"X-Telegram-Bot-Api-Secret-Token": "ÿÿÿ"})
    assert resp.status_code == 403


@pytest.mark.django_db
def test_verify_code_expires(user):
    """L5: a stale verify code (past its TTL) can no longer bind a chat id."""
    import datetime as dt

    from django.utils import timezone

    from apps.pingboard.models import PilotContactChannel as PCC

    row = linking.start_link(user, "telegram")
    code = row.verify_code
    PCC.objects.filter(pk=row.pk).update(
        verify_code_expires_at=timezone.now() - dt.timedelta(minutes=1))
    assert linking.verify_by_code("telegram", code, "chat-1") is None
    row.refresh_from_db()
    assert row.verified is False


@pytest.mark.django_db
def test_confirm_rejects_expired_code(user):
    import datetime as dt

    from django.utils import timezone

    from apps.pingboard.models import PilotContactChannel as PCC

    row = linking.start_link(user, "slack", handle="U1")
    PCC.objects.filter(pk=row.pk).update(
        verify_code_expires_at=timezone.now() - dt.timedelta(minutes=1))
    assert linking.confirm(user, "slack", row.verify_code) is False
