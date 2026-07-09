"""Notification-governance tests: the event registry, the audience→classification gate
that keeps leadership traffic off mass chat channels, and the console page.

The guarantee under test: a leadership-audience notification is delivered to leadership
(in-app / EVE-mail / a leadership-cleared chat channel) but NEVER posted to a corp-wide
chat channel — enforced at BOTH sinks (``broadcast_text`` and the ``AlertDispatcher``).
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.pingboard import config, notifications
from apps.pingboard.models import (
    Alert,
    AlertPriority,
    AlertSource,
    AlertStatus,
    ChannelProvider,
    DeliveryStatus,
)
from apps.pingboard.services import audience_classification
from core import rbac
from tests._raffle_utils import enrol_pilot, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_notifications_config():
    """Each test starts from shipped defaults (config is cached + versioned)."""
    config.reset("notifications")
    yield
    config.reset("notifications")


# --- registry + policy resolution -------------------------------------------- #
def test_leadership_events_resolve_to_restricted_classification():
    for key in ("recommendations.officer_digest", "pilots.leadership_briefing",
                "readiness.alert", "readiness.weekly_report", "mentorship.review"):
        pol = notifications.resolve(key)
        assert pol["sensitive"] is True
        assert pol["audience"] == "officer"
        assert pol["classification"] == "high_command"  # blocked from corp channels


def test_corp_events_resolve_to_corp_internal():
    for key in ("killboard.killfeed", "esi.corp_alert", "raffle.announce"):
        pol = notifications.resolve(key)
        assert pol["classification"] is None  # corp-internal → any channel


def test_override_can_disable_and_rewiden_an_event():
    config.set("notifications", {"events": {
        "recommendations.officer_digest": {"enabled": False, "audience": "corp", "min_severity": 70},
    }})
    pol = notifications.resolve("recommendations.officer_digest")
    assert pol["enabled"] is False
    assert pol["audience"] == "corp"
    assert pol["classification"] is None       # widened to corp → mass broadcast allowed
    assert pol["min_severity"] == 70


def test_unknown_event_key_is_enabled_corp_passthrough():
    pol = notifications.resolve("some.future.event")
    assert pol["enabled"] is True
    assert pol["classification"] is None


# --- config validation ------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    {"leadership_role": "overlord"},
    {"leadership_user_ids": ["nope"]},
    {"events": {"x": {"audience": "everyone"}}},
    {"events": {"x": {"min_severity": 500}}},
])
def test_config_validator_rejects_bad_input(bad):
    with pytest.raises(config.ConfigError):
        config.set("notifications", bad)


# --- audience → classification ----------------------------------------------- #
def test_audience_classification_mapping():
    assert audience_classification({"kind": "corp"}) is None
    assert audience_classification({"kind": "officer"}) == "high_command"
    assert audience_classification({"kind": "director"}) == "director_eyes_only"
    assert audience_classification({"kind": "user", "id": 1}) == "director_eyes_only"
    assert audience_classification({"kind": "role", "role": "officer"}) == "high_command"


# --- Path B: the dispatcher never posts a restricted alert to a corp channel -- #
def _discord_provider(label, ceiling=""):
    p = ChannelProvider(kind="discord", label=label, enabled=True, max_classification=ceiling)
    p.secret = "https://discord.com/api/webhooks/1/tok"
    p.save()
    return p


def _make_alert(audience):
    return Alert.objects.create(
        title="t", body="b", category="system", priority=AlertPriority.NORMAL,
        source=AlertSource.SERVICE, status=AlertStatus.QUEUED,
        audience=audience, channels=["in_app", "discord"],
    )


def test_officer_alert_skips_corp_discord_but_reaches_a_leadership_channel(django_user_model, monkeypatch):
    posts = []
    monkeypatch.setattr(
        "apps.pingboard.providers.discord.requests.post",
        lambda url, json=None, timeout=None, allow_redirects=None: posts.append(json["content"])
        or type("R", (), {"status_code": 204})(),
    )
    # An officer who should receive the in-app leg.
    enrol_pilot(django_user_model, 7001, roles=(rbac.ROLE_OFFICER,))
    corp_ch = _discord_provider("corp-general")                       # corp-wide ceiling
    lead_ch = _discord_provider("command", ceiling="high_command")    # leadership channel

    from apps.pingboard.dispatch import AlertDispatcher

    alert = _make_alert({"kind": "officer"})
    AlertDispatcher().dispatch(alert.pk)

    corp_del = alert.deliveries.get(kind="discord", provider=corp_ch)
    lead_del = alert.deliveries.get(kind="discord", provider=lead_ch)
    assert corp_del.status == DeliveryStatus.SKIPPED          # NOT posted to the mass channel
    assert "ceiling too low" in corp_del.last_error
    assert lead_del.status == DeliveryStatus.DELIVERED        # posted to the leadership channel
    assert len(posts) == 1                                    # exactly one webhook hit
    # …and the officer still gets the in-app leg (audience resolved to a real recipient).
    assert alert.recipients.filter(kind="in_app").exists()


def test_corp_alert_reaches_the_mass_discord_channel(monkeypatch):
    posts = []
    monkeypatch.setattr(
        "apps.pingboard.providers.discord.requests.post",
        lambda url, json=None, timeout=None, allow_redirects=None: posts.append(json["content"])
        or type("R", (), {"status_code": 204})(),
    )
    corp_ch = _discord_provider("corp-general")
    from apps.pingboard.dispatch import AlertDispatcher

    alert = _make_alert({"kind": "corp"})
    AlertDispatcher().dispatch(alert.pk)
    assert alert.deliveries.get(kind="discord", provider=corp_ch).status == DeliveryStatus.DELIVERED
    assert len(posts) == 1


# --- Path A: broadcast_text honours the event classification ----------------- #
def test_recommendations_digest_is_kept_off_a_corp_channel(sde, monkeypatch):
    from apps.killboard.ingest import ingest_killmail
    from apps.recommendations import engine
    from apps.recommendations.notify import dispatch_alerts

    posts = []
    monkeypatch.setattr(
        "apps.pingboard.providers.discord.requests.post",
        lambda url, json=None, timeout=None, allow_redirects=None: posts.append(json["content"])
        or type("R", (), {"status_code": 204})(),
    )
    _discord_provider("corp-general")  # only a corp-wide channel is armed

    import datetime as dt

    from django.utils import timezone
    recent = (timezone.now() - dt.timedelta(days=1)).isoformat()
    for i in range(5):  # 5 losses → severity 50 clears the floor
        ingest_killmail(9100 + i, f"h{i}", body={
            "killmail_id": 9100 + i, "killmail_time": recent, "solar_system_id": 30002053,
            "victim": {"corporation_id": 98000001, "ship_type_id": 587, "items": []},
            "attackers": [{"character_id": 1, "corporation_id": 99}],
        })
    engine.run_all()
    assert dispatch_alerts() >= 1                # in-app alerts still created
    assert posts == []                           # but NOTHING hit the corp Discord channel

    # Arm a leadership channel → the very same digest now delivers there.
    _discord_provider("command", ceiling="high_command")
    from apps.recommendations.models import Alert as RecAlert
    RecAlert.objects.all().delete()              # clear per-rec dedup so it re-broadcasts
    dispatch_alerts()
    assert posts and "Lost 5" in posts[0]


def test_disabling_the_digest_event_stops_the_broadcast(sde, monkeypatch):
    from apps.killboard.ingest import ingest_killmail
    from apps.recommendations import engine
    from apps.recommendations.notify import dispatch_alerts

    posts = []
    monkeypatch.setattr(
        "apps.pingboard.providers.discord.requests.post",
        lambda url, json=None, timeout=None, allow_redirects=None: posts.append(1)
        or type("R", (), {"status_code": 204})(),
    )
    _discord_provider("command", ceiling="high_command")  # a leadership channel exists
    config.set("notifications", {"events": {"recommendations.officer_digest": {"enabled": False}}})

    import datetime as dt

    from django.utils import timezone
    recent = (timezone.now() - dt.timedelta(days=1)).isoformat()
    for i in range(5):
        ingest_killmail(9200 + i, f"g{i}", body={
            "killmail_id": 9200 + i, "killmail_time": recent, "solar_system_id": 30002053,
            "victim": {"corporation_id": 98000001, "ship_type_id": 587, "items": []},
            "attackers": [{"character_id": 1, "corporation_id": 99}],
        })
    engine.run_all()
    assert dispatch_alerts() >= 1     # in-app record still stands
    assert posts == []                # broadcast suppressed by the console toggle


# --- console page ------------------------------------------------------------ #
def test_console_requires_director(client, django_user_model):
    officer = make_user(django_user_model, "off1", rbac.ROLE_OFFICER)
    client.force_login(officer)
    assert client.get(reverse("admin_audit:notifications")).status_code == 403


def test_console_saves_event_overrides(client, django_user_model):
    director = make_user(django_user_model, "dir1", rbac.ROLE_DIRECTOR)
    client.force_login(director)
    assert client.get(reverse("admin_audit:notifications")).status_code == 200

    resp = client.post(reverse("admin_audit:notifications"), {
        "form": "events",
        "enabled__killboard.killfeed": "on",
        "audience__recommendations.officer_digest": "director",
        "severity__recommendations.officer_digest": "70",
    })
    assert resp.status_code == 302
    pol = notifications.resolve("recommendations.officer_digest")
    assert pol["audience"] == "director"
    assert pol["classification"] == "director_eyes_only"
    assert pol["min_severity"] == 70
    # An event whose checkbox was absent is now disabled.
    assert notifications.resolve("raffle.announce")["enabled"] is False


def test_console_saves_leadership_distribution(client, django_user_model):
    director = make_user(django_user_model, "dir2", rbac.ROLE_DIRECTOR)
    lead_user, _ = enrol_pilot(django_user_model, 7100, roles=(rbac.ROLE_OFFICER,))
    client.force_login(director)
    resp = client.post(reverse("admin_audit:notifications"), {
        "form": "leadership",
        "leadership_role": "director",
        "leadership_user_ids": [str(lead_user.id)],
    })
    assert resp.status_code == 302
    assert notifications.leadership_role() == "director"
    assert notifications.leadership_audience() == {"kind": "users", "ids": [lead_user.id]}
