"""Pingboard Phase 0 — emit, dispatch, providers, scheduling, retry, deliver-once."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.pingboard import services
from apps.pingboard.models import (
    Alert,
    AlertDelivery,
    AlertRecipient,
    AlertStatus,
    ChannelProvider,
    DeliveryStatus,
)
from apps.pingboard.providers import Recipient
from apps.pingboard.providers.discord import DiscordProvider
from apps.pingboard.providers.evemail import EveMailProvider


class FakeResp:
    def __init__(self, status=204, data=None):
        self.status_code = status
        self.data = data


# --- emit --------------------------------------------------------------------
@pytest.mark.django_db
def test_emit_applies_category_routing_defaults(character):
    a = services.emit_alert(category="pvp_fleet", title="Form up", body="now",
                            source_service="ops", source_object_id="7")
    assert a is not None
    assert a.priority == "high"                 # from routing defaults
    assert a.audience == {"kind": "corp"}
    assert "discord" in a.channels and "in_app" in a.channels
    assert a.status == AlertStatus.QUEUED
    assert a.source == "service"


@pytest.mark.django_db
def test_emit_idempotency_returns_same_alert(character):
    k = "ops:7:formup"
    a = services.emit_alert(category="pvp_fleet", title="x", body="y", idempotency_key=k)
    b = services.emit_alert(category="pvp_fleet", title="x2", body="y2", idempotency_key=k)
    assert a.id == b.id
    assert Alert.objects.filter(idempotency_key=k).count() == 1


@pytest.mark.django_db
def test_emit_urgent_requires_reason(character):
    # no reason → refused
    with pytest.raises(ValueError):
        services.emit_alert(category="emergency", title="HELP", body="hostiles",
                            priority="emergency", confirmation={"two_step": True})
    # reason but no two-step confirmation → refused (Phase 4 governance)
    with pytest.raises(ValueError):
        services.emit_alert(category="emergency", title="HELP", body="hostiles",
                            priority="emergency", reason="30 hostiles in home")
    # reason + two-step confirmation → accepted
    ok = services.emit_alert(category="emergency", title="HELP", body="hostiles",
                             priority="emergency", reason="30 hostiles in home",
                             confirmation={"two_step": True})
    assert ok is not None and ok.reason and ok.confirmation.get("two_step") is True


@pytest.mark.django_db
def test_emit_dry_run_does_not_queue(character):
    a = services.emit_alert(category="announcement", title="t", body="b", dry_run=True)
    assert a.status == AlertStatus.DRAFT


@pytest.mark.django_db
def test_emit_duplicate_suppressed(character):
    a = services.emit_alert(category="announcement", title="t", body="same", audience={"kind": "corp"})
    b = services.emit_alert(category="announcement", title="t", body="same", audience={"kind": "corp"})
    assert a is not None and b is None


@pytest.mark.django_db
def test_emit_rate_limited_returns_none(character):
    from apps.pingboard import config

    config.set("anti_abuse", {"max_per_category_per_hour": 1, "suppress_duplicates": False})
    a = services.emit_alert(category="announcement", title="t1", body="b1")
    b = services.emit_alert(category="announcement", title="t2", body="b2")
    assert a is not None and b is None


# --- dispatch: in-app --------------------------------------------------------
@pytest.mark.django_db
def test_dispatch_in_app(character):
    a = services.emit_alert(category="announcement", title="hi", body="body",
                            channels=["in_app"], audience={"kind": "corp"})
    services.dispatch_alert(a.id)
    a.refresh_from_db()
    assert a.status == AlertStatus.SENT
    d = a.deliveries.get(kind="in_app")
    assert d.status == DeliveryStatus.DELIVERED
    assert a.recipient_count == 1


# --- dispatch: discord (mocked) ---------------------------------------------
@pytest.mark.django_db
def test_dispatch_discord_preserves_guard(monkeypatch, character):
    captured = {}

    def fake_post(url, json=None, timeout=None, allow_redirects=None):
        captured.update(url=url, json=json, allow_redirects=allow_redirects)
        return FakeResp(204)

    monkeypatch.setattr("apps.pingboard.providers.discord.requests.post", fake_post)
    prov = ChannelProvider(kind="discord", label="corp", enabled=True, supports_channel=True)
    prov.secret = "https://discord.com/api/webhooks/1/token"
    prov.save()

    a = services.emit_alert(category="announcement", title="t", body="@everyone hi",
                            channels=["discord"], audience={"kind": "corp"})
    services.dispatch_alert(a.id)
    a.refresh_from_db()
    assert a.status == AlertStatus.SENT
    assert a.deliveries.get(kind="discord").status == DeliveryStatus.DELIVERED
    # the SSRF/abuse guard is preserved
    assert captured["allow_redirects"] is False
    assert captured["json"]["allowed_mentions"] == {"parse": []}
    prov.refresh_from_db()
    assert prov.last_ok_at is not None


@pytest.mark.django_db
def test_discord_refuses_non_webhook_host():
    p = ChannelProvider(kind="discord", label="evil")
    p.secret = "https://evil.example.com/api/webhooks/1/t"
    res = DiscordProvider(p).send(subject="", body="hi", recipients=[])
    assert res.ok is False and "invalid" in res.error


@pytest.mark.django_db
def test_dispatch_no_provider_is_skipped(character):
    a = services.emit_alert(category="announcement", title="t", body="b",
                            channels=["discord"], audience={"kind": "corp"})
    services.dispatch_alert(a.id)
    a.refresh_from_db()
    # nothing delivered → FAILED, and the channel is recorded SKIPPED (not an error)
    assert a.status == AlertStatus.FAILED
    assert a.deliveries.get(kind="discord").status == DeliveryStatus.SKIPPED


# --- dispatch: eve-mail (mocked) --------------------------------------------
def _mock_mail(monkeypatch):
    client = type("C", (), {"posts": []})()

    def post(path, json=None, token=None):
        client.posts.append((path, json))
        return FakeResp(201, data=999)

    client.post = post
    monkeypatch.setattr("core.esi.client.get_client", lambda: client)
    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda ch, scopes: "tok")
    return client


@pytest.mark.django_db
def test_dispatch_eve_mail(monkeypatch, user, character):
    client = _mock_mail(monkeypatch)
    ChannelProvider.objects.create(
        kind="eve_mail", label="sender", enabled=True,
        routing={"sender_character_id": 1001}, supports_direct=True,
    )
    a = services.emit_alert(category="announcement", title="Subject", body="Body",
                            channels=["eve_mail"], audience={"kind": "corp"})
    services.dispatch_alert(a.id)
    a.refresh_from_db()
    assert a.status == AlertStatus.SENT
    d = a.deliveries.get(kind="eve_mail")
    assert d.status == DeliveryStatus.DELIVERED
    assert d.provider_message_id == "999"
    # one post, recipient = the corp member's character id
    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == "/characters/1001/mail/"
    assert payload["recipients"] == [{"recipient_id": 1001, "recipient_type": "character"}]


@pytest.mark.django_db
def test_eve_mail_chunks_recipients(monkeypatch, character):
    client = _mock_mail(monkeypatch)
    prov = ChannelProvider(kind="eve_mail", label="s", enabled=True, routing={"sender_character_id": 1001})
    recipients = [Recipient("eve_mail", "character", str(9000 + i)) for i in range(60)]
    res = EveMailProvider(prov).send(subject="s", body="b", recipients=recipients)
    assert res.ok and res.recipients_ok == 60
    assert len(client.posts) == 2  # 60 recipients / 50 cap


@pytest.mark.django_db
def test_eve_mail_no_sender_degrades(character):
    prov = ChannelProvider(kind="eve_mail", label="s", enabled=True, routing={})
    res = EveMailProvider(prov).send(subject="s", body="b",
                                     recipients=[Recipient("eve_mail", "character", "1001")])
    assert res.ok is False and res.skipped is False


# --- partial failure / isolation / deliver-once ------------------------------
@pytest.mark.django_db
def test_partial_failure(monkeypatch, character):
    monkeypatch.setattr("apps.pingboard.providers.discord.requests.post",
                        lambda *a, **k: FakeResp(500))
    prov = ChannelProvider(kind="discord", label="c", enabled=True)
    prov.secret = "https://discord.com/api/webhooks/1/t"
    prov.save()
    a = services.emit_alert(category="announcement", title="t", body="b",
                            channels=["in_app", "discord"], audience={"kind": "corp"})
    services.dispatch_alert(a.id)
    a.refresh_from_db()
    assert a.status == AlertStatus.PARTIAL
    assert a.deliveries.get(kind="in_app").status == DeliveryStatus.DELIVERED
    assert a.deliveries.get(kind="discord").status == DeliveryStatus.FAILED


@pytest.mark.django_db
def test_provider_crash_is_isolated(monkeypatch, character):
    def boom(self, *a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(DiscordProvider, "send", boom)
    prov = ChannelProvider(kind="discord", label="c", enabled=True)
    prov.secret = "https://discord.com/api/webhooks/1/t"
    prov.save()
    a = services.emit_alert(category="announcement", title="t", body="b",
                            channels=["in_app", "discord"], audience={"kind": "corp"})
    # must not raise
    services.dispatch_alert(a.id)
    a.refresh_from_db()
    assert a.status == AlertStatus.PARTIAL  # in_app ok, discord failed
    assert a.deliveries.get(kind="discord").status == DeliveryStatus.FAILED


@pytest.mark.django_db
def test_deliver_once_no_duplicate_rows(character):
    a = services.emit_alert(category="announcement", title="t", body="b",
                            channels=["in_app"], audience={"kind": "corp"})
    services.dispatch_alert(a.id)
    # re-dispatch is a no-op (already terminal) — rows never duplicate
    services.dispatch_alert(a.id)
    assert AlertDelivery.objects.filter(alert=a, kind="in_app").count() == 1
    assert AlertRecipient.objects.filter(alert=a, kind="in_app").count() == 1


# --- scheduling / retry ------------------------------------------------------
@pytest.mark.django_db
def test_scheduled_alert_fires_only_when_due(character):
    future = timezone.now() + dt.timedelta(hours=2)
    a = services.emit_alert(category="announcement", title="t", body="b",
                            channels=["in_app"], scheduled_at=future)
    assert a.status == AlertStatus.SCHEDULED
    assert services.dispatch_due_alerts()["fired"] == 0  # not due yet
    Alert.objects.filter(pk=a.pk).update(scheduled_at=timezone.now() - dt.timedelta(minutes=1))
    assert services.dispatch_due_alerts()["fired"] == 1
    a.refresh_from_db()
    assert a.status == AlertStatus.SENT


@pytest.mark.django_db
def test_cancel_and_retry(character):
    a = services.emit_alert(category="announcement", title="t", body="b",
                            channels=["in_app"], scheduled_at=timezone.now() + dt.timedelta(hours=1))
    assert services.cancel_alert(a.id) is True
    a.refresh_from_db()
    assert a.status == AlertStatus.CANCELLED
    # a failed alert can be requeued
    a.status = AlertStatus.FAILED
    a.save()
    AlertDelivery.objects.create(alert=a, kind="discord", status=DeliveryStatus.FAILED)
    out = services.retry_alert(a.id)
    a.refresh_from_db()
    assert out["deliveries"] == 1 and a.status == AlertStatus.QUEUED


@pytest.mark.django_db
def test_retry_sweep_redelivers_only_failed_channel(monkeypatch, character):
    state = {"fail": True}

    def fake_post(url, json=None, timeout=None, allow_redirects=None):
        return FakeResp(500 if state["fail"] else 204)

    monkeypatch.setattr("apps.pingboard.providers.discord.requests.post", fake_post)
    prov = ChannelProvider(kind="discord", label="c", enabled=True)
    prov.secret = "https://discord.com/api/webhooks/1/t"
    prov.save()

    a = services.emit_alert(category="announcement", title="t", body="b",
                            channels=["in_app", "discord"], audience={"kind": "corp"})
    services.dispatch_alert(a.id)
    a.refresh_from_db()
    assert a.status == AlertStatus.PARTIAL
    inapp_attempts = a.deliveries.get(kind="in_app").attempts

    state["fail"] = False
    out = services.retry_failed_deliveries()
    a.refresh_from_db()
    assert out["retried"] == 1
    assert a.status == AlertStatus.SENT
    assert a.deliveries.get(kind="discord").status == DeliveryStatus.DELIVERED
    # deliver-once: the already-delivered in-app channel was NOT re-sent
    assert a.deliveries.get(kind="in_app").attempts == inapp_attempts


@pytest.mark.django_db
def test_retry_converges_after_attempt_cap(monkeypatch, character):
    monkeypatch.setattr("apps.pingboard.providers.discord.requests.post",
                        lambda *a, **k: FakeResp(500))
    prov = ChannelProvider(kind="discord", label="c", enabled=True)
    prov.secret = "https://discord.com/api/webhooks/1/t"
    prov.save()
    a = services.emit_alert(category="announcement", title="t", body="b",
                            channels=["discord"], audience={"kind": "corp"})
    services.dispatch_alert(a.id)
    # sweep more times than the cap; must stop retrying (no infinite loop)
    for _ in range(10):
        services.retry_failed_deliveries()
    d = a.deliveries.get(kind="discord")
    assert d.attempts <= d.max_attempts
    a.refresh_from_db()
    assert a.status == AlertStatus.FAILED


# --- retirement: legacy NotificationChannel path gone, primitives preserved --
def test_broadcast_discord_symbols_preserved_after_retirement():
    """The legacy NotificationChannel registry is retired, but the public
    broadcast_discord entry point and the SSRF-guarded _post_discord primitive
    (used by the security regression tests) are kept."""
    from apps.recommendations import notify

    assert callable(notify.broadcast_discord)
    assert callable(notify._post_discord)
    assert not hasattr(notify, "_legacy_broadcast_discord")  # legacy path removed


# --- the dispatcher is the last gate ------------------------------------------
@pytest.mark.django_db
def test_dispatcher_refuses_a_draft_or_scheduled_alert(character, monkeypatch):
    """Only a QUEUED alert may go out. A DRAFT (awaiting approval) or SCHEDULED (not due)
    alert handed straight to the dispatcher — a stray task, a retry on the wrong alert —
    must not reach any channel."""
    from apps.pingboard.dispatch import AlertDispatcher

    def _never(*args, **kwargs):
        raise AssertionError("the webhook must not be hit")

    monkeypatch.setattr("apps.pingboard.providers.discord.requests.post", _never)
    prov = ChannelProvider(kind="discord", label="corp", enabled=True)
    prov.secret = "https://discord.com/api/webhooks/1/tok"
    prov.save()
    draft = services.emit_alert(category="pvp_fleet", title="Draft op", body="b",
                                channels=["in_app", "discord"], dry_run=True)
    assert draft.status == AlertStatus.DRAFT
    assert AlertDispatcher().dispatch(draft.id) == {"status": AlertStatus.DRAFT, "noop": True}
    later = services.emit_alert(category="pvp_fleet", title="Later", body="later body",
                                channels=["in_app", "discord"],
                                scheduled_at=timezone.now() + dt.timedelta(hours=1))
    assert AlertDispatcher().dispatch(later.id) == {"status": AlertStatus.SCHEDULED, "noop": True}
    for a in (draft, later):
        a.refresh_from_db()
        assert a.deliveries.count() == 0 and a.recipients.count() == 0
    assert draft.status == AlertStatus.DRAFT and later.status == AlertStatus.SCHEDULED


@pytest.mark.django_db
def test_retry_only_requeues_a_failed_alert(character):
    draft = services.emit_alert(category="pvp_fleet", title="Draft op", body="b",
                                channels=["in_app"], dry_run=True)
    assert services.retry_alert(draft.id)["status"] == "not_retryable"
    draft.refresh_from_db()
    assert draft.status == AlertStatus.DRAFT
    later = services.emit_alert(category="pvp_fleet", title="Later", body="later body",
                                channels=["in_app"],
                                scheduled_at=timezone.now() + dt.timedelta(hours=1))
    assert services.retry_alert(later.id)["status"] == "not_retryable"
    later.refresh_from_db()
    assert later.status == AlertStatus.SCHEDULED


@pytest.mark.django_db
def test_dispatcher_retracts_a_queued_reminder_whose_event_closed(character, monkeypatch):
    """A reminder already QUEUED (or retrying) when its event is retracted — the operation
    demoted to draft between materialisation and delivery — is cancelled at the dispatcher,
    the one gate every channel still passes through."""
    from apps.pingboard import calendar as cal
    from apps.pingboard.dispatch import AlertDispatcher
    from apps.pingboard.models import CalendarEventStatus

    posts = []
    monkeypatch.setattr("apps.pingboard.providers.discord.requests.post",
                        lambda url, json=None, timeout=None, allow_redirects=None:
                        posts.append(json) or FakeResp(204))
    prov = ChannelProvider(kind="discord", label="corp", enabled=True)
    prov.secret = "https://discord.com/api/webhooks/1/tok"
    prov.save()
    e = cal.publish_event(source_system="operations", source_object_id="9",
                          event_type="fleet_op", title="Mercoxit dig",
                          start_at=timezone.now() + dt.timedelta(minutes=30))
    reminder = services.emit_alert(category="pvp_fleet", title=e.title, body="⏰",
                                   channels=["in_app", "discord"], calendar_event=e,
                                   bypass_ratelimit=True)
    # emit enqueued it; simulate the worker picking it up only after the op was demoted
    reminder.refresh_from_db()
    assert reminder.status == AlertStatus.QUEUED
    cal.cancel_event(source_system="operations", source_object_id="9")
    out = AlertDispatcher().dispatch(reminder.id)
    assert out["status"] == AlertStatus.CANCELLED
    reminder.refresh_from_db()
    assert reminder.status == AlertStatus.CANCELLED
    assert posts == []
    assert not reminder.deliveries.filter(status=DeliveryStatus.DELIVERED).exists()
    # a retry sweep must not resurrect it either
    assert services.retry_failed_deliveries()["retried"] == 0
    # positive control: the same reminder on a still-open event does go out
    e.status = CalendarEventStatus.SYNCED
    e.save()
    live = services.emit_alert(category="pvp_fleet", title=e.title, body="⏰ again",
                               channels=["discord"], calendar_event=e, bypass_ratelimit=True)
    AlertDispatcher().dispatch(live.id)
    live.refresh_from_db()
    assert live.status == AlertStatus.SENT and len(posts) == 1
