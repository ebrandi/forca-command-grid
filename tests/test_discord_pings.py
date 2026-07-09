"""Discord broadcast helper + operations announce ping."""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.operations.models import Operation
from apps.sso.services import ensure_role
from core import rbac


def _officer(django_user_model):
    user = django_user_model.objects.create(username="eve:disc1")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


@pytest.mark.django_db
def test_broadcast_discord_only_active_channels(monkeypatch):
    """broadcast_discord now routes through Pingboard ChannelProvider rows (the legacy
    NotificationChannel registry is retired): only enabled Discord channels are posted to."""
    from apps.pingboard.models import ChannelProvider
    from apps.recommendations import notify

    posts = []
    monkeypatch.setattr(
        "apps.pingboard.providers.discord.requests.post",
        lambda url, json=None, timeout=None, allow_redirects=None: posts.append(url)
        or type("R", (), {"status_code": 204})(),
    )
    enabled = ChannelProvider(kind="discord", label="corp", enabled=True)
    enabled.secret = "https://discord.com/api/webhooks/1/abc"
    enabled.save()
    disabled = ChannelProvider(kind="discord", label="off", enabled=False)
    disabled.secret = "https://discord.com/api/webhooks/2/def"
    disabled.save()

    n = notify.broadcast_discord("hello fleet")
    assert n == 1
    assert posts == ["https://discord.com/api/webhooks/1/abc"]


def test_op_ping_text_format(db):
    from apps.operations.views import _op_ping_text

    op = Operation.objects.create(name="Home Defence", type=Operation.Type.HOME_DEFENCE)
    text = _op_ping_text(op, "https://grid.example.com/operations/1/")
    assert "Home Defence" in text and "https://grid.example.com/operations/1/" in text


@pytest.mark.django_db
def test_op_announce_view_creates_pingboard_alert(client, django_user_model):
    """Announce now fans out through Pingboard (every armed channel) instead of a
    Discord-only webhook: it records a corp Alert with the op's mapped category."""
    from apps.pingboard.models import Alert

    officer = _officer(django_user_model)
    op = Operation.objects.create(name="Strat Timer", type=Operation.Type.STRUCTURE_TIMER)
    client.force_login(officer)

    # The picker renders pre-checked; submitting the form sends the ticked channels.
    resp = client.post(f"/operations/{op.pk}/announce/", {"channel_in_app": "on"})
    assert resp.status_code == 302
    alert = Alert.objects.get(source_service="operations")
    assert alert.title == "Strat Timer"
    assert alert.category == "structure_timer"
    assert alert.source_object_id == f"announce:{op.pk}"
    # A fleet announcement is corp-wide even for an officer-routed op category.
    assert alert.audience == {"kind": "corp"}
    assert "in_app" in alert.channels


@pytest.mark.django_db
def test_op_announce_honours_channel_picker(client, django_user_model, monkeypatch):
    """An officer can narrow the announce to a subset of the armed channels."""
    from apps.pingboard.models import Alert, ChannelProvider

    # Arm a Discord channel so the picker offers more than in-app.
    p = ChannelProvider(kind="discord", label="corp", enabled=True)
    p.secret = "https://discord.com/api/webhooks/1/tok"
    p.save()

    officer = _officer(django_user_model)
    op = Operation.objects.create(name="Roam", type=Operation.Type.ROAM)
    client.force_login(officer)

    # Tick only in-app; Discord is left off.
    resp = client.post(f"/operations/{op.pk}/announce/", {"channel_in_app": "on"})
    assert resp.status_code == 302
    alert = Alert.objects.get(source_service="operations")
    assert alert.channels == ["in_app"]
    assert alert.category == "roaming_gang"


@pytest.mark.django_db
def test_op_announce_empty_selection_is_a_noop(client, django_user_model):
    """Un-ticking every channel is an explicit 'send nothing', not a broadcast-to-all."""
    from apps.pingboard.models import Alert

    officer = _officer(django_user_model)
    op = Operation.objects.create(name="Roam", type=Operation.Type.ROAM)
    client.force_login(officer)
    # Picker is shown (in-app is always offered) but nothing is ticked.
    resp = client.post(f"/operations/{op.pk}/announce/", {})
    assert resp.status_code == 302
    assert not Alert.objects.filter(source_service="operations").exists()
