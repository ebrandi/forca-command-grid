"""Sov ADM tracking: sync filters to our alliance, ADM + soft flag, officer board."""
from __future__ import annotations

import pytest

from apps.corporation.models import EveAlliance, EveCorporation
from apps.identity.models import RoleAssignment
from apps.operations.models import SovStructure
from apps.sso.services import ensure_role
from core import rbac

HOME_CORP = 98000001
HOME_ALLI = 99000001


class _Client:
    def __init__(self, rows):
        self._rows = rows

    def get(self, path, token=None, params=None):
        return type("R", (), {"data": self._rows})()


def _home_alliance(settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    alli = EveAlliance.objects.create(alliance_id=HOME_ALLI, name="Home Alliance")
    EveCorporation.objects.create(corporation_id=HOME_CORP, name="Home", alliance=alli)


def _user(django_user_model, name, role):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


def _sys(structure_id, alliance_id, system_id, adm):
    """A /sovereignty/systems/ row in the current ESI shape (ADM under development)."""
    return {
        "solar_system_id": system_id,
        "claim": {"alliance": {
            "alliance_id": alliance_id,
            "sovereignty_hub": {
                "id": structure_id,
                "vulnerability_window": {"start": "2026-06-27T17:00:00Z", "end": "2026-06-27T21:00:00Z"},
            },
            "development": {"activity_defense_multiplier": adm},
        }},
    }


def _payload(rows):
    return {"solar_systems": rows}


@pytest.mark.django_db
def test_sync_keeps_only_our_alliance_structures(settings):
    _home_alliance(settings)
    from apps.operations.sov_esi import sync_sovereignty

    rows = [
        _sys(1, HOME_ALLI, 30000142, 1.5),   # ours, soft
        _sys(2, HOME_ALLI, 30000142, 5.0),   # ours, strong
        _sys(3, 42, 30000143, 1.0),          # someone else
    ]
    res = sync_sovereignty(client=_Client(_payload(rows)))
    assert res["status"] == "ok" and res["count"] == 2
    assert set(SovStructure.objects.values_list("structure_id", flat=True)) == {1, 2}
    soft = SovStructure.objects.get(structure_id=1)
    assert soft.is_soft and soft.adm == 1.5
    assert soft.vulnerable_start is not None
    assert not SovStructure.objects.get(structure_id=2).is_soft

    # Snapshot replace on re-sync.
    sync_sovereignty(client=_Client(_payload(rows[:1])))
    assert set(SovStructure.objects.values_list("structure_id", flat=True)) == {1}


@pytest.mark.django_db
def test_no_alliance_is_noop(settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP  # no EveCorporation row → no alliance
    from apps.operations.sov_esi import sync_sovereignty

    assert sync_sovereignty(client=_Client([]))["status"] == "no_alliance"


@pytest.mark.django_db
def test_sov_board_is_officer_only(client, django_user_model, sde):
    SovStructure.objects.create(structure_id=7, alliance_id=HOME_ALLI, solar_system_id=30000142,
                                system_name="Jita", structure_type_id=32458, adm=2.0)
    client.force_login(_user(django_user_model, "m", rbac.ROLE_MEMBER))
    assert client.get("/operations/sov/").status_code == 403
    client.force_login(_user(django_user_model, "fc", rbac.ROLE_OFFICER))
    html = client.get("/operations/sov/").content.decode()
    assert "Sovereignty" in html and "Jita" in html and "soft" in html  # ADM 2.0 < 3
