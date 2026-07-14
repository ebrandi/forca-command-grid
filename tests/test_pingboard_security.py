"""Phase 7 — adversarial: permission bypass, secret non-exposure, spam, retention."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.admin_audit.models import AuditLog
from apps.identity.models import RoleAssignment
from apps.pingboard import config, services
from apps.pingboard.models import Alert, AlertStatus, CalendarEvent, CalendarEventStatus, ChannelProvider
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

_WEBHOOK = "https://discord.com/api/webhooks/1/SUPERSECRETTOKEN"

_ADMIN_URLS = [
    "admin_audit:pingboard_settings", "admin_audit:pingboard_channels",
    "admin_audit:pingboard_automation", "admin_audit:pingboard_templates",
]


def _user(dj, uid, role):
    u = dj.objects.create(username=f"u{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    # is_corp_director: since LP-4 the app's Director role is only exercisable from a pilot who
    # holds the in-game Director role, so a director fixture needs the seat that proves it.
    EveCharacter.objects.create(character_id=uid, user=u, name=f"P{uid}", is_main=True,
                                is_corp_member=True, is_corp_director=role == rbac.ROLE_DIRECTOR)
    return u


# --- permission bypass -------------------------------------------------------
@pytest.mark.django_db
def test_member_cannot_reach_admin_get_or_post(client, django_user_model):
    client.force_login(_user(django_user_model, 8001, rbac.ROLE_MEMBER))
    for name in _ADMIN_URLS:
        assert client.get(reverse(name)).status_code == 403
        assert client.post(reverse(name), {}).status_code == 403


@pytest.mark.django_db
def test_officer_cannot_reach_director_admin(client, django_user_model):
    client.force_login(_user(django_user_model, 8002, rbac.ROLE_OFFICER))
    for name in _ADMIN_URLS:
        assert client.get(reverse(name)).status_code == 403


@pytest.mark.django_db
def test_member_cannot_open_officer_alert_detail(client, django_user_model):
    a = services.emit_alert(category="announcement", title="t", body="b", channels=["in_app"])
    client.force_login(_user(django_user_model, 8003, rbac.ROLE_MEMBER))
    assert client.get(reverse("pingboard:alert_detail", args=[a.id])).status_code == 403


# --- dispatch-authority floor (M1: officers may not exceed the configured floor) ---
@pytest.mark.django_db
def test_officer_cannot_dispatch_emergency(django_user_model):
    """An officer-attributed manual dispatch may not send a director-floored priority,
    even with the reason + two-step confirmation supplied."""
    officer = _user(django_user_model, 8101, rbac.ROLE_OFFICER)
    with pytest.raises(ValueError):
        services.emit_alert(
            category="home_defence", priority="emergency", title="x", body="y",
            channels=["in_app"], created_by=officer, source="manual", reason="reds",
            confirmation={"two_step": True, "large_audience_ack": True})


@pytest.mark.django_db
def test_officer_cannot_announce_corpwide(django_user_model):
    """Corp-wide announcements are director-floored regardless of priority."""
    officer = _user(django_user_model, 8102, rbac.ROLE_OFFICER)
    with pytest.raises(ValueError):
        services.emit_alert(category="announcement", priority="low", title="x", body="y",
                            channels=["in_app"], created_by=officer, source="manual")


@pytest.mark.django_db
def test_officer_can_dispatch_routine(django_user_model):
    """Routine (non-announcement) traffic within the officer floor still goes through."""
    officer = _user(django_user_model, 8104, rbac.ROLE_OFFICER)
    a = services.emit_alert(category="pvp_fleet", priority="high", title="x", body="y",
                            channels=["in_app"], created_by=officer, source="manual")
    assert a is not None


@pytest.mark.django_db
def test_director_can_dispatch_emergency(django_user_model):
    director = _user(django_user_model, 8103, rbac.ROLE_DIRECTOR)
    a = services.emit_alert(
        category="home_defence", priority="emergency", title="x", body="y",
        channels=["in_app"], created_by=director, source="manual", reason="reds",
        confirmation={"two_step": True, "large_audience_ack": True})
    assert a is not None


@pytest.mark.django_db
def test_service_alert_bypasses_dispatch_floor(character):
    """A system/service alert (ESI structure attack, auto-cancel …) is not user-attributed
    and must still fire at urgent/emergency priority — the floor is manual-dispatch only."""
    a = services.emit_broadcast(category="home_defence", title="Hostiles", body="Form up",
                                source_service="test", channels=["in_app"])
    assert a is not None and a.priority == "urgent"  # home_defence routing priority


# --- secret non-exposure -----------------------------------------------------
@pytest.mark.django_db
def test_provider_secret_never_rendered_or_audited(client, django_user_model):
    client.force_login(_user(django_user_model, 8004, rbac.ROLE_DIRECTOR))
    client.post(reverse("admin_audit:pingboard_channels"), {
        "action": "create", "kind": "discord", "label": "Corp", "secret": _WEBHOOK, "enabled": "on",
    })
    prov = ChannelProvider.objects.get(kind="discord")
    assert prov.has_secret and prov.secret == _WEBHOOK  # stored (encrypted) + usable
    # never rendered back to the page
    html = client.get(reverse("admin_audit:pingboard_channels")).content.decode()
    assert "SUPERSECRETTOKEN" not in html
    # never written into an audit record
    blob = json.dumps([{"a": a.action, "m": a.metadata, "t": a.target_id} for a in AuditLog.objects.all()])
    assert "SUPERSECRETTOKEN" not in blob


@pytest.mark.django_db
def test_encrypted_secret_not_plaintext_in_column():
    p = ChannelProvider(kind="discord", label="x")
    p.secret = _WEBHOOK
    assert "SUPERSECRETTOKEN" not in p._secret  # ciphertext column


# --- spam / duplicate protection ---------------------------------------------
@pytest.mark.django_db
def test_rate_limit_blocks_a_burst(character):
    config.set("anti_abuse", {"max_per_category_per_hour": 3, "suppress_duplicates": False})
    made = [services.emit_alert(category="announcement", title=f"t{i}", body=f"b{i}",
                                channels=["in_app"], created_by=None) for i in range(6)]
    assert sum(1 for a in made if a is not None) == 3  # only 3 got through


@pytest.mark.django_db
def test_duplicate_dispatch_suppressed(character):
    a = services.emit_alert(category="announcement", title="t", body="same", channels=["in_app"])
    b = services.emit_alert(category="announcement", title="t", body="same", channels=["in_app"])
    assert a is not None and b is None


# --- retention ---------------------------------------------------------------
@pytest.mark.django_db
def test_housekeeping_prunes_old_but_keeps_recent(character):
    now = timezone.now()
    old = services.emit_alert(category="announcement", title="old", body="x", channels=["in_app"])
    old.status = AlertStatus.SENT
    old.save()
    Alert.objects.filter(pk=old.pk).update(created_at=now - dt.timedelta(days=400))
    recent = Alert.objects.create(title="recent", category="announcement", status=AlertStatus.SENT)

    old_event = CalendarEvent.objects.create(title="past", event_type="custom",
                                             start_at=now - dt.timedelta(days=200),
                                             status=CalendarEventStatus.COMPLETED)
    fresh_event = CalendarEvent.objects.create(title="soon", event_type="custom",
                                               start_at=now + dt.timedelta(days=2),
                                               status=CalendarEventStatus.SCHEDULED)

    out = services.housekeeping()
    assert out["alerts"] >= 1 and out["events"] >= 1
    assert not Alert.objects.filter(pk=old.pk).exists()
    assert Alert.objects.filter(pk=recent.pk).exists()
    assert not CalendarEvent.objects.filter(pk=old_event.pk).exists()
    assert CalendarEvent.objects.filter(pk=fresh_event.pk).exists()
