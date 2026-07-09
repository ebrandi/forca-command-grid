"""4.5 — Saved-route camp / incursion push alerts.

Acceptance: an opt-in DM to the OWNER of a saved jump route when a camp/incursion appears
on one of its systems; only on a change in the threat set (signature dedup); off when the
governance event is disabled or the route isn't watched; per-pilot audience.
"""
from __future__ import annotations

import pytest

from apps.navigation.models import SavedJumpRoute
from apps.navigation.route_watch import scan_route_watches
from apps.pingboard import config
from apps.pingboard.models import Alert

pytestmark = pytest.mark.django_db
SYS_CAMP = 30000142
SYS_INCURSION = 30000144
SAFE = 30000143
CAMP_KILLS = {"ship_kills": 10, "pod_kills": 3, "npc_kills": 0}  # → gatecamp.assess "danger"


@pytest.fixture(autouse=True)
def _reset_config():
    config.reset("notifications")
    yield
    config.reset("notifications")


@pytest.fixture
def feeds(monkeypatch):
    monkeypatch.setattr("apps.navigation.map_overlays.system_kills", lambda: {SYS_CAMP: CAMP_KILLS})
    monkeypatch.setattr("apps.navigation.services.incursion_systems", lambda: {SYS_INCURSION})


def _route(django_user_model, *, origin=SYS_CAMP, dest=SAFE, watch=True):
    owner = django_user_model.objects.create(username=f"pilot-{origin}-{dest}-{watch}")
    return SavedJumpRoute.objects.create(
        owner=owner, name="Home run", origin_system_id=origin, origin_name="O",
        dest_system_id=dest, dest_name="D", ship_key="rhea", watch_enabled=watch,
    )


def _alerts(uid=None):
    qs = Alert.objects.filter(source_service="navigation")
    return qs.filter(audience={"kind": "user", "id": uid}) if uid else qs


def test_camp_on_route_alerts_owner(feeds, django_user_model):
    r = _route(django_user_model)
    assert scan_route_watches()["alerted"] == 1
    assert _alerts(r.owner_id).count() == 1
    assert "risk indicator" in _alerts().first().body.lower()


def test_incursion_on_route_alerts(feeds, django_user_model):
    _route(django_user_model, origin=SYS_INCURSION)
    assert scan_route_watches()["alerted"] == 1


def test_no_threat_no_alert(feeds, django_user_model):
    _route(django_user_model, origin=SAFE, dest=SAFE)
    assert scan_route_watches()["alerted"] == 0 and not _alerts().exists()


def test_watch_disabled_route_ignored(feeds, django_user_model):
    _route(django_user_model, watch=False)
    assert scan_route_watches().get("alerted", 0) == 0 and not _alerts().exists()


def test_disabled_event_noop(feeds, django_user_model):
    _route(django_user_model)
    config.set("notifications", {"events": {"navigation.route_watch": {"enabled": False}}})
    assert scan_route_watches()["status"] == "disabled"
    assert not _alerts().exists()


def test_watch_toggle_is_owner_only(client, django_user_model):
    from django.urls import reverse

    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac
    r = _route(django_user_model, watch=False)
    RoleAssignment.objects.create(user=r.owner, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(r.owner)
    assert client.post(reverse("navigation:jump_route_watch", args=[r.pk])).status_code == 302
    r.refresh_from_db()
    assert r.watch_enabled is True  # owner armed it
    intruder = django_user_model.objects.create(username="intruder")
    RoleAssignment.objects.create(user=intruder, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(intruder)
    assert client.post(reverse("navigation:jump_route_watch", args=[r.pk])).status_code == 404
    r.refresh_from_db()
    assert r.watch_enabled is True  # a non-owner can't touch it


def test_watched_route_cap(client, django_user_model):
    from django.urls import reverse

    from apps.identity.models import RoleAssignment
    from apps.navigation.views import _MAX_WATCHED_ROUTES
    from apps.sso.services import ensure_role
    from core import rbac
    owner = django_user_model.objects.create(username="capper")
    RoleAssignment.objects.create(user=owner, role=ensure_role(rbac.ROLE_MEMBER))
    for i in range(_MAX_WATCHED_ROUTES):
        SavedJumpRoute.objects.create(owner=owner, name=f"r{i}", origin_system_id=1, origin_name="O",
                                      dest_system_id=2, dest_name="D", ship_key="rhea", watch_enabled=True)
    extra = SavedJumpRoute.objects.create(owner=owner, name="extra", origin_system_id=1, origin_name="O",
                                          dest_system_id=2, dest_name="D", ship_key="rhea", watch_enabled=False)
    client.force_login(owner)
    assert client.post(reverse("navigation:jump_route_watch", args=[extra.pk])).status_code == 302
    extra.refresh_from_db()
    assert extra.watch_enabled is False  # capped — not armed beyond the limit


def test_signature_dedup_and_clear(feeds, django_user_model, monkeypatch):
    r = _route(django_user_model)
    assert scan_route_watches()["alerted"] == 1
    assert scan_route_watches()["alerted"] == 0  # unchanged threat set → no re-alert
    assert r.__class__.objects.get(pk=r.pk).alerted_sig != ""
    # threat clears → marker reset (no DM), so a fresh threat can re-alert
    monkeypatch.setattr("apps.navigation.map_overlays.system_kills", lambda: {})
    monkeypatch.setattr("apps.navigation.services.incursion_systems", lambda: set())
    scan_route_watches()
    assert r.__class__.objects.get(pk=r.pk).alerted_sig == ""
    # camp returns → re-fires
    monkeypatch.setattr("apps.navigation.map_overlays.system_kills", lambda: {SYS_CAMP: CAMP_KILLS})
    assert scan_route_watches()["alerted"] == 1
