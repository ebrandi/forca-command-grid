"""Corp structure monitoring: ESI sync (fuel/state/timers), prune, board, fuel flags."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.corporation.models import CorpStructure
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac

HOME = 98000001


class _PagedClient:
    def __init__(self, rows, names=None):
        self.rows = rows
        self.names = names or {}

    def get_paged(self, path, token=None, params=None):
        return self.rows

    def get(self, path, token=None, params=None):
        # /universe/structures/{id}/ name lookup
        sid = int(path.rstrip("/").split("/")[-1])
        return type("R", (), {"data": {"name": self.names.get(sid, "")}})()


@pytest.fixture
def _granted(monkeypatch):
    from apps.corporation import structures_esi

    monkeypatch.setattr(structures_esi, "_token_character",
                        lambda corp_id: type("C", (), {"character_id": 1})())
    monkeypatch.setattr("apps.sso.token_service.get_valid_access_token", lambda ch, sc: "tok")


def _user(django_user_model, name, role):
    user = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


@pytest.mark.django_db
def test_sync_stores_fuel_state_and_prunes(_granted, settings):
    settings.FORCA_HOME_CORP_ID = HOME
    from apps.corporation.structures_esi import sync_corp_structures

    now = timezone.now()
    rows = [
        {"structure_id": 1001, "type_id": 35832, "system_id": 30000142,
         "state": "shield_vulnerable", "fuel_expires": (now + timedelta(days=10)).isoformat(),
         "services": [{"name": "Market", "state": "online"}]},
        {"structure_id": 1002, "type_id": 35833, "system_id": 30000142,
         "state": "armor_reinforce", "fuel_expires": (now + timedelta(days=1)).isoformat(),
         "state_timer_end": (now + timedelta(hours=20)).isoformat()},
    ]
    res = sync_corp_structures(corp_id=HOME, client=_PagedClient(rows, names={1001: "Keepstar Alpha"}))
    assert res["status"] == "ok" and res["count"] == 2

    alpha = CorpStructure.objects.get(structure_id=1001)
    assert alpha.name == "Keepstar Alpha" and not alpha.is_low_fuel
    beta = CorpStructure.objects.get(structure_id=1002)
    assert beta.is_low_fuel and beta.is_reinforced

    # Prune: a later sync without 1002 removes it; 1001 keeps its resolved name.
    sync_corp_structures(corp_id=HOME, client=_PagedClient(rows[:1]))
    assert set(CorpStructure.objects.values_list("structure_id", flat=True)) == {1001}
    assert CorpStructure.objects.get(structure_id=1001).name == "Keepstar Alpha"


@pytest.mark.django_db
def test_reinforced_structures_become_timer_board_entries(_granted, settings):
    settings.FORCA_HOME_CORP_ID = HOME
    from apps.corporation.structures_esi import sync_corp_structures
    from apps.operations.models import StructureTimer

    now = timezone.now()
    reinforced = [{"structure_id": 3001, "type_id": 35832, "system_id": 30000142,
                   "state": "armor_reinforce", "fuel_expires": (now + timedelta(days=5)).isoformat(),
                   "state_timer_end": (now + timedelta(hours=30)).isoformat()}]
    sync_corp_structures(corp_id=HOME, client=_PagedClient(reinforced, names={3001: "Fortizar X"}))

    t = StructureTimer.objects.get(name="Fortizar X")
    assert t.timer_type == "armor" and t.side == StructureTimer.Side.FRIENDLY
    assert "Auto-imported" in t.notes

    # Idempotent: re-sync doesn't duplicate the auto timer.
    sync_corp_structures(corp_id=HOME, client=_PagedClient(reinforced, names={3001: "Fortizar X"}))
    assert StructureTimer.objects.filter(name="Fortizar X").count() == 1

    # No longer reinforced → the auto timer is pruned.
    calm = [{"structure_id": 3001, "type_id": 35832, "system_id": 30000142,
             "state": "shield_vulnerable", "fuel_expires": (now + timedelta(days=5)).isoformat()}]
    sync_corp_structures(corp_id=HOME, client=_PagedClient(calm))
    assert not StructureTimer.objects.filter(name="Fortizar X").exists()


@pytest.mark.django_db
def test_no_token_is_noop(monkeypatch):
    from apps.corporation import structures_esi

    monkeypatch.setattr(structures_esi, "_token_character", lambda corp_id: None)
    assert structures_esi.sync_corp_structures(corp_id=HOME)["status"] == "no_token"
    assert CorpStructure.objects.count() == 0


@pytest.mark.django_db
def test_structures_board_is_officer_gated(client, django_user_model, sde):
    CorpStructure.objects.create(structure_id=2001, type_id=35832, name="Fortizar",
                                 fuel_expires=timezone.now() + timedelta(days=2))
    client.force_login(_user(django_user_model, "m", rbac.ROLE_MEMBER))
    assert client.get("/roster/structures/").status_code == 403
    client.force_login(_user(django_user_model, "fc", rbac.ROLE_OFFICER))
    html = client.get("/roster/structures/").content.decode()
    assert html.count("Fortizar") and "low on fuel" in html  # <3 days → flagged
