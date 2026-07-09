"""Moon-extraction sync (names from SDE celestials) + member calendar."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone

from apps.corporation.models import MoonExtraction
from apps.identity.models import RoleAssignment
from apps.sde.models import SdeCelestial
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
def test_sync_resolves_moon_name_and_is_idempotent(monkeypatch):
    from apps.corporation import extractions as E

    SdeCelestial.objects.create(item_id=40009999, system_id=30000142,
                                kind=SdeCelestial.Kind.MOON, name="Jita IV - Moon 4")
    now = timezone.now()
    rows = [{
        "structure_id": 1000000000001, "moon_id": 40009999,
        "extraction_start_time": now.isoformat(),
        "chunk_arrival_time": (now + dt.timedelta(days=3)).isoformat(),
        "natural_decay_time": (now + dt.timedelta(days=4)).isoformat(),
    }]
    monkeypatch.setattr(E, "_token_character", lambda corp_id: type("X", (), {"character_id": 1})())
    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda ch, sc: "tok")

    res = E.sync_moon_extractions(corp_id=1, client=_Client(rows))
    assert res["status"] == "ok" and res["count"] == 1
    ex = MoonExtraction.objects.get()
    assert ex.moon_name == "Jita IV - Moon 4" and ex.structure_id == 1000000000001

    E.sync_moon_extractions(corp_id=1, client=_Client(rows))
    assert MoonExtraction.objects.count() == 1


@pytest.mark.django_db
def test_extractions_view(client, django_user_model):
    MoonExtraction.objects.create(structure_id=1, moon_name="Test Moon",
                                  chunk_arrival=timezone.now() + dt.timedelta(days=1))
    user = django_user_model.objects.create(username="eve:moon1")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(user)
    resp = client.get("/roster/extractions/")
    assert resp.status_code == 200 and "Test Moon" in resp.content.decode()
