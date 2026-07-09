"""Phase 5 — pilot/officer UI + Admin Console (RBAC, rendering, actions)."""
from __future__ import annotations

import datetime as dt

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.pingboard import calendar as pcal
from apps.pingboard import config, services
from apps.pingboard.models import (
    Alert,
    AlertStatus,
    AutomationRule,
    CalendarEvent,
    ChannelProvider,
    PilotContactChannel,
)
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, uid, role):
    u = django_user_model.objects.create(username=f"u{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    EveCharacter.objects.create(character_id=uid, user=u, name=f"P{uid}", is_main=True, is_corp_member=True)
    return u


class FakeResp:
    status_code = 204

    def json(self):
        return {"ok": True}


# --- RBAC / rendering --------------------------------------------------------
@pytest.mark.django_db
def test_member_sees_dashboard_and_calendar(client, django_user_model):
    client.force_login(_user(django_user_model, 5001, rbac.ROLE_MEMBER))
    assert client.get(reverse("pingboard:dashboard")).status_code == 200
    assert client.get(reverse("pingboard:calendar")).status_code == 200
    assert client.get(reverse("pingboard:calendar") + "?view=month").status_code == 200
    assert client.get(reverse("pingboard:my_channels")).status_code == 200


@pytest.mark.django_db
def test_member_cannot_compose_or_admin(client, django_user_model):
    client.force_login(_user(django_user_model, 5001, rbac.ROLE_MEMBER))
    assert client.get(reverse("pingboard:compose")).status_code == 403
    assert client.get(reverse("pingboard:history")).status_code == 403
    assert client.get(reverse("admin_audit:pingboard_settings")).status_code == 403


@pytest.mark.django_db
def test_officer_can_compose_and_history(client, django_user_model):
    client.force_login(_user(django_user_model, 5002, rbac.ROLE_OFFICER))
    assert client.get(reverse("pingboard:compose")).status_code == 200
    assert client.get(reverse("pingboard:history")).status_code == 200


@pytest.mark.django_db
def test_feature_gate_hides_pingboard(client, django_user_model):
    from core import features

    features.set_disabled(["pingboard"])
    client.force_login(_user(django_user_model, 5003, rbac.ROLE_MEMBER))
    assert client.get(reverse("pingboard:dashboard")).status_code == 404


# --- composer ----------------------------------------------------------------
@pytest.mark.django_db
def test_compose_creates_alert(client, django_user_model):
    client.force_login(_user(django_user_model, 5002, rbac.ROLE_OFFICER))
    # Routine (non-announcement, normal-priority) traffic is within the officer floor.
    resp = client.post(reverse("pingboard:compose"), {
        "category": "pvp_fleet", "priority": "normal", "audience": "corp",
        "channel_in_app": "on", "title": "Fleet up", "body": "Now.",
    })
    assert resp.status_code == 302
    a = Alert.objects.latest("created_at")
    assert a.title == "Fleet up" and a.status == AlertStatus.QUEUED


@pytest.mark.django_db
def test_compose_offers_every_armed_channel(client, django_user_model):
    """Regression: a Telegram channel armed entirely in the web UI (creds on the
    ChannelProvider row, no env flag) must appear in the composer — not just in-app +
    Discord. A disabled channel with no env flag stays hidden."""
    tg = ChannelProvider(kind="telegram", label="Corp TG", enabled=True,
                         routing={"chat_id": "-100123"})
    tg.secret = "123456:bottoken"
    tg.save()
    ChannelProvider.objects.create(kind="discord", label="Corp", enabled=True)
    ChannelProvider.objects.create(kind="eve_mail", label="Off", enabled=False)

    client.force_login(_user(django_user_model, 5009, rbac.ROLE_OFFICER))
    html = client.get(reverse("pingboard:compose")).content.decode()
    assert 'name="channel_in_app"' in html
    assert 'name="channel_discord"' in html
    assert 'name="channel_telegram"' in html            # web-UI armed → offered
    assert 'name="channel_eve_mail"' not in html        # disabled, no env flag → hidden


@pytest.mark.django_db
def test_enabled_channel_kinds_is_db_driven():
    """A channel appears from an enabled ChannelProvider row alone — no env var needed."""
    before = set(services.enabled_channel_values())
    assert "in_app" in before and "whatsapp" not in before

    wa = ChannelProvider(kind="whatsapp", label="WA", enabled=True,
                         routing={"backend": "meta", "meta_phone_id": "1", "to": "+100"})
    wa.secret = "metatoken"
    wa.save()
    assert "whatsapp" in set(services.enabled_channel_values())


@pytest.mark.django_db
def test_emit_broadcast_audience_follows_category_routing():
    """A service broadcast inherits the category's configured audience — officer-only
    categories are NOT silently fanned out corp-wide."""
    officer_alert = services.emit_broadcast(
        category="structure_timer", title="Fuel low", body="Top it up",
        source_service="test", channels=["in_app"])
    assert officer_alert is not None and officer_alert.audience == {"kind": "officer"}

    corp_alert = services.emit_broadcast(
        category="home_defence", title="Hostiles", body="Form up",
        source_service="test", channels=["in_app"])
    assert corp_alert.audience == {"kind": "corp"}

    # An explicit audience still wins (a fleet announcement is corp-wide).
    explicit = services.emit_broadcast(
        category="structure_timer", title="Rally", body="Timer!",
        audience={"kind": "corp"}, source_service="test", channels=["in_app"])
    assert explicit.audience == {"kind": "corp"}


@pytest.mark.django_db
def test_compose_urgent_without_confirmation_reprompts(client, django_user_model):
    # A director clears the emergency dispatch floor, so this isolates the *confirmation*
    # requirement (missing two-step / reason → re-render, no alert).
    client.force_login(_user(django_user_model, 5011, rbac.ROLE_DIRECTOR))
    resp = client.post(reverse("pingboard:compose"), {
        "category": "emergency", "priority": "emergency", "audience": "corp",
        "channel_in_app": "on", "title": "HELP", "body": "hostiles",
    })
    assert resp.status_code == 200  # re-rendered with the confirmation requirement
    assert not Alert.objects.filter(priority="emergency").exists()


@pytest.mark.django_db
def test_compose_urgent_with_confirmation_sends(client, django_user_model):
    # Emergency dispatch is director-floored (see test_pingboard_security), so the
    # authorized dispatcher here is a director.
    client.force_login(_user(django_user_model, 5012, rbac.ROLE_DIRECTOR))
    resp = client.post(reverse("pingboard:compose"), {
        "category": "emergency", "priority": "emergency", "audience": "corp",
        "channel_in_app": "on", "title": "HELP", "body": "hostiles",
        "reason": "30 reds in home", "confirm_two_step": "on", "confirm_large": "on",
    })
    assert resp.status_code == 302
    assert Alert.objects.filter(priority="emergency", reason="30 reds in home").exists()


# --- alert actions -----------------------------------------------------------
@pytest.mark.django_db
def test_alert_approve_action(client, django_user_model):
    client.force_login(_user(django_user_model, 5002, rbac.ROLE_OFFICER))
    a = services.emit_alert(category="announcement", title="t", body="b",
                            channels=["in_app"], dry_run=True)
    resp = client.post(reverse("pingboard:alert_action", args=[a.id, "approve"]))
    assert resp.status_code == 302
    a.refresh_from_db()
    assert a.status == AlertStatus.QUEUED


# --- calendar actions --------------------------------------------------------
@pytest.mark.django_db
def test_officer_creates_event_and_reminder(client, django_user_model):
    client.force_login(_user(django_user_model, 5002, rbac.ROLE_OFFICER))
    start = (timezone.now() + dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    resp = client.post(reverse("pingboard:event_create"), {
        "title": "Corp meeting", "event_type": "announcement", "start_at": start,
        "visibility": "member",
    })
    assert resp.status_code == 302
    event = CalendarEvent.objects.get(title="Corp meeting")
    r2 = client.post(reverse("pingboard:event_action", args=[event.id, "reminder"]),
                     {"offset_minutes_before": "30"})
    assert r2.status_code == 302
    assert event.alert_schedules.filter(offset_minutes_before=30).exists()


@pytest.mark.django_db
def test_event_visibility_gate(client, django_user_model):
    # a director-only event is 404 for a plain member
    event = pcal.create_manual_event(title="Secret", event_type="custom",
                                     start_at=timezone.now() + dt.timedelta(days=1),
                                     visibility="director")
    client.force_login(_user(django_user_model, 5001, rbac.ROLE_MEMBER))
    assert client.get(reverse("pingboard:calendar_event", args=[event.id])).status_code == 404


# --- pilot channel linking ---------------------------------------------------
@pytest.mark.django_db
def test_channel_link_and_confirm(client, django_user_model):
    u = _user(django_user_model, 5001, rbac.ROLE_MEMBER)
    client.force_login(u)
    client.post(reverse("pingboard:channel_link"), {"kind": "slack", "handle": "U9"})
    row = PilotContactChannel.objects.get(user=u, kind="slack")
    assert not row.verified
    client.post(reverse("pingboard:channel_confirm"), {"kind": "slack", "code": row.verify_code})
    row.refresh_from_db()
    assert row.verified is True


# --- admin console -----------------------------------------------------------
@pytest.mark.django_db
def test_admin_settings_saves_config(client, django_user_model):
    client.force_login(_user(django_user_model, 5004, rbac.ROLE_DIRECTOR))
    assert client.get(reverse("admin_audit:pingboard_settings")).status_code == 200
    resp = client.post(reverse("admin_audit:pingboard_settings"), {
        "domain": "anti_abuse", "max_per_officer_per_hour": "7",
        "max_per_category_per_hour": "30", "max_urgent_per_day": "10",
        "cooldown_minutes": "15", "duplicate_window_minutes": "10",
        "large_audience_threshold": "50", "suppress_duplicates": "on",
    })
    assert resp.status_code == 302
    assert config.get("anti_abuse")["max_per_officer_per_hour"] == 7


@pytest.mark.django_db
def test_admin_channel_create_and_test(client, django_user_model, monkeypatch):
    monkeypatch.setattr("apps.pingboard.providers.discord.requests.post", lambda *a, **k: FakeResp())
    client.force_login(_user(django_user_model, 5004, rbac.ROLE_DIRECTOR))
    client.post(reverse("admin_audit:pingboard_channels"), {
        "action": "create", "kind": "discord", "label": "Corp",
        "secret": "https://discord.com/api/webhooks/1/tok", "enabled": "on",
    })
    prov = ChannelProvider.objects.get(kind="discord")
    assert prov.enabled and prov.has_secret
    client.post(reverse("admin_audit:pingboard_channels"),
                {"action": "test", "provider_id": prov.id})
    prov.refresh_from_db()
    assert prov.last_ok_at is not None


@pytest.mark.django_db
def test_admin_eve_mail_test_addresses_the_officer(client, django_user_model, monkeypatch):
    """Regression: 'Send test message' for EVE-mail must address the officer running it
    (a per-recipient channel), not fail with 'no resolvable recipients'."""
    from types import SimpleNamespace

    from apps.sso.models import AuthToken, EveCharacter

    director = _user(django_user_model, 5100, rbac.ROLE_DIRECTOR)  # main character 5100
    sender_user = django_user_model.objects.create(username="mailer")
    sender = EveCharacter.objects.create(character_id=9001, name="Mailer", is_main=True,
                                         is_corp_member=True, user=sender_user)
    AuthToken.objects.create(character=sender, scopes=["esi-mail.send_mail.v1"])
    prov = ChannelProvider.objects.create(kind="eve_mail", label="Mail", enabled=True,
                                          routing={"sender_character_id": 9001})

    captured = {}

    class FakeClient:
        def post(self, path, *, json=None, token=None):
            captured["path"] = path
            captured["json"] = json
            return SimpleNamespace(status=201, data=555)

    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda c, s: "tok")
    monkeypatch.setattr("core.esi.client.get_client", lambda: FakeClient())

    client.force_login(director)
    client.post(reverse("admin_audit:pingboard_channels"),
                {"action": "test", "provider_id": prov.id})

    # Mailed FROM the sender TO the officer's own character — not nothing.
    assert captured.get("path") == "/characters/9001/mail/"
    assert captured["json"]["recipients"] == [{"recipient_id": 5100, "recipient_type": "character"}]
    prov.refresh_from_db()
    assert prov.last_ok_at is not None and not prov.last_error


@pytest.mark.django_db
def test_admin_automation_and_template_create(client, django_user_model):
    client.force_login(_user(django_user_model, 5004, rbac.ROLE_DIRECTOR))
    client.post(reverse("admin_audit:pingboard_automation"), {
        "action": "create", "key": "srp-sub", "label": "SRP submitted",
        "trigger_source": "srp.submitted", "category": "logistics", "priority": "normal",
        "audience": '{"kind": "officer"}', "title": "SRP", "body": "New claim",
        "condition": "", "channels": "",
        "cooldown_minutes": "0", "max_per_window": "0", "window_minutes": "60", "expires_at": "",
    })
    rule = AutomationRule.objects.get(key="srp-sub")
    assert rule.enabled is False  # ships disabled
    client.post(reverse("admin_audit:pingboard_automation"), {"action": "toggle", "rule_id": rule.id})
    rule.refresh_from_db()
    assert rule.enabled is True

    from apps.pingboard.models import AlertTemplate

    client.post(reverse("admin_audit:pingboard_templates"), {
        "action": "create", "key": "op-formup", "label": "Op form-up",
        "category": "pvp_fleet", "default_priority": "high", "body": "Form up now.",
    })
    assert AlertTemplate.objects.filter(key="op-formup").exists()
