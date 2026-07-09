"""ESI in-game notification relay: filter, store, idempotency, fresh-only alerts."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.recommendations import notifications as N
from apps.recommendations.models import CorpNotification
from apps.sso.services import ensure_role
from core import rbac


class _Resp:
    def __init__(self, data):
        self.data = data


class _Client:
    def __init__(self, rows):
        self._rows = rows

    def get(self, path, token=None):
        return _Resp(self._rows)


@pytest.mark.django_db
def test_sync_filters_stores_and_alerts_fresh(monkeypatch):
    fresh = timezone.now().isoformat()
    old = (timezone.now() - dt.timedelta(days=10)).isoformat()
    rows = [
        {"notification_id": 1, "type": "StructureUnderAttack", "timestamp": fresh, "text": ""},
        {"notification_id": 2, "type": "MoonminingExtractionStarted", "timestamp": fresh, "text": ""},
        {"notification_id": 3, "type": "CharLogonReminder", "timestamp": fresh, "text": ""},  # boring
        {"notification_id": 4, "type": "WarDeclared", "timestamp": old, "text": ""},  # too old to alert
    ]
    monkeypatch.setattr(N, "_token_character",
                        lambda corp_id: type("C", (), {"character_id": 99})())
    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token",
                        lambda ch, scopes: "tok")

    res = N.sync_corp_notifications(corp_id=1, client=_Client(rows))
    assert res["status"] == "ok" and res["new"] == 3  # 1, 2, 4 kept; 3 filtered out
    assert set(CorpNotification.objects.values_list("notification_id", flat=True)) == {1, 2, 4}
    # Only the fresh structure-attack fires a Pingboard corp alert (moon=no-alert, war=stale).
    from apps.pingboard.models import Alert
    assert res["alerted"] == 1
    alert = Alert.objects.get(source_service="recommendations")
    assert "under attack" in alert.title.lower()
    assert alert.category == "home_defence"
    assert alert.source_object_id == "esi-notif:1"

    # Idempotent: a second run adds nothing and fires no new alert.
    res2 = N.sync_corp_notifications(corp_id=1, client=_Client(rows))
    assert res2["new"] == 0 and CorpNotification.objects.count() == 3
    assert res2["alerted"] == 0 and Alert.objects.filter(source_service="recommendations").count() == 1


@pytest.mark.django_db
def test_sync_no_token(monkeypatch):
    monkeypatch.setattr(N, "_token_character", lambda corp_id: None)
    assert N.sync_corp_notifications(corp_id=1)["status"] == "no_token"


@pytest.mark.django_db
def test_notifications_view(client, django_user_model):
    officer = django_user_model.objects.create(username="eve:notif1")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    CorpNotification.objects.create(notification_id=1, type="StructureUnderAttack",
                                    timestamp=timezone.now())
    client.force_login(officer)
    resp = client.get("/recommendations/notifications/")
    assert resp.status_code == 200 and "Structure under attack" in resp.content.decode()
