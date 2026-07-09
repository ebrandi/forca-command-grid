"""Corp contracts oversight: snapshot sync + officer board."""
from __future__ import annotations

import pytest

from apps.corporation.models import EveName
from apps.identity.models import RoleAssignment
from apps.logistics.models import CorpContract
from apps.sso.services import ensure_role
from core import rbac

HOME = 98000001


class _Client:
    def __init__(self, rows):
        self._rows = rows

    def get_paged(self, path, token=None, params=None):
        return self._rows


def _user(django_user_model, name, role):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


@pytest.mark.django_db
def test_sync_snapshots_all_contract_types(monkeypatch, settings):
    settings.FORCA_HOME_CORP_ID = HOME
    from apps.logistics import corp_contracts as C

    monkeypatch.setattr(C, "_director_contract_token", lambda corp_id: "tok")
    EveName.objects.create(entity_id=1001, name="Issuer Ida", category="character")
    rows = [
        {"contract_id": 1, "type": "item_exchange", "status": "outstanding",
         "issuer_id": 1001, "assignee_id": None, "title": "Doctrine ships", "price": "5000000"},
        {"contract_id": 2, "type": "courier", "status": "in_progress",
         "issuer_id": 1001, "reward": "12000000", "volume": 30000},
    ]
    res = C.sync_corp_contracts(corp_id=HOME, client=_Client(rows))
    assert res["status"] == "ok" and res["count"] == 2
    c1 = CorpContract.objects.get(contract_id=1)
    assert c1.type == "item_exchange" and c1.is_open and c1.issuer_name == "Issuer Ida"

    # Snapshot-replace: a smaller later sync drops the missing one.
    C.sync_corp_contracts(corp_id=HOME, client=_Client(rows[:1]))
    assert set(CorpContract.objects.values_list("contract_id", flat=True)) == {1}


@pytest.mark.django_db
def test_no_token_is_noop(monkeypatch, settings):
    settings.FORCA_HOME_CORP_ID = HOME
    from apps.logistics import corp_contracts as C

    monkeypatch.setattr(C, "_director_contract_token", lambda corp_id: None)
    assert C.sync_corp_contracts(corp_id=HOME)["status"] == "no_token"
    assert CorpContract.objects.count() == 0


@pytest.mark.django_db
def test_board_is_officer_only(client, django_user_model, sde):
    CorpContract.objects.create(contract_id=9, type="courier", status="outstanding", title="Haul X")
    client.force_login(_user(django_user_model, "m", rbac.ROLE_MEMBER))
    assert client.get("/freight/corp-contracts/").status_code == 403
    client.force_login(_user(django_user_model, "fc", rbac.ROLE_OFFICER))
    html = client.get("/freight/corp-contracts/").content.decode()
    assert "Corp contracts" in html and "Haul X" in html
