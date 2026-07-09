"""Corp standings/contacts sync + member-facing blue/red board."""
from __future__ import annotations

import pytest

from apps.corporation.models import Contact, EveName
from apps.identity.models import RoleAssignment
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


def _member(django_user_model, cid):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    return user


@pytest.mark.django_db
def test_sync_stores_and_prunes_contacts(monkeypatch):
    from apps.corporation import contacts as C

    rows = [
        {"contact_id": 99, "contact_type": "corporation", "standing": 10.0},
        {"contact_id": 50, "contact_type": "alliance", "standing": -10.0},
    ]
    monkeypatch.setattr(C, "_token_character", lambda corp_id: type("X", (), {"character_id": 1})())
    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda ch, sc: "tok")
    monkeypatch.setattr("core.esi.names.resolve_ids", lambda ids: 0)
    EveName.objects.create(entity_id=99, name="Blue Corp", category="corporation")

    res = C.sync_corp_contacts(corp_id=1, client=_Client(rows))
    assert res["status"] == "ok" and res["count"] == 2
    assert Contact.objects.get(contact_id=99).standing == 10.0
    assert Contact.objects.get(contact_id=99).name == "Blue Corp"

    # A contact no longer on the corp list is pruned.
    C.sync_corp_contacts(corp_id=1, client=_Client([rows[0]]))
    assert not Contact.objects.filter(contact_id=50).exists()
    assert Contact.objects.count() == 1


@pytest.mark.django_db
def test_standings_view_splits_blue_red(client, django_user_model):
    Contact.objects.create(contact_id=99, contact_type="corporation", standing=10.0, name="Friendlies")
    Contact.objects.create(contact_id=50, contact_type="alliance", standing=-10.0, name="Hostiles")
    client.force_login(_member(django_user_model, "st1"))
    resp = client.get("/roster/standings/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Friendlies" in body and "Hostiles" in body
