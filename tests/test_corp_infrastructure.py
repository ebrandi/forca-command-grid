"""CORP-2 (roadmap 2.4) — unified corp infrastructure board.

Acceptance: a single board merges structure fuel/state, sov ADM and timers, ranked
by urgency (critical → warning → ok); leadership thresholds decide low/soft.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.corporation.infra import infrastructure_board
from apps.corporation.models import CorpStructure, StructureAlertConfig
from apps.operations.models import SovStructure, StructureTimer
from core import rbac
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear():
    cache.delete(StructureAlertConfig._CACHE_KEY)
    yield
    cache.delete(StructureAlertConfig._CACHE_KEY)


def _cfg(*, fuel_days=3, adm_floor=3.0):
    StructureAlertConfig.objects.create(
        is_active=True, fuel_alert_days=fuel_days, adm_alert_floor=adm_floor
    )


def _structure(sid, *, days, name="Keep"):
    return CorpStructure.objects.create(
        structure_id=sid, type_id=35834, name=name, system_name="J1",
        fuel_expires=timezone.now() + timedelta(days=days),
    )


def test_board_merges_all_sources_and_ranks_by_urgency():
    _cfg()
    _structure(1, days=0, name="Empty")     # out of fuel → critical
    _structure(2, days=10, name="Healthy")  # ok
    _structure(3, days=1, name="Low")       # low fuel → warning
    SovStructure.objects.create(
        structure_id=100, alliance_id=1, solar_system_id=5, system_name="Sov", adm=2.0
    )  # soft ADM → warning
    StructureTimer.objects.create(
        name="Fort", system_name="J2", exits_at=timezone.now() + timedelta(hours=10)
    )  # < 48h → critical

    board = infrastructure_board()
    assert len(board) == 5
    sevs = [i["severity"] for i in board]
    assert sevs[0] == "critical" and sevs[-1] == "ok"  # ranked
    crit = {i["name"] for i in board if i["severity"] == "critical"}
    warn = {i["name"] for i in board if i["severity"] == "warning"}
    assert {"Empty", "Fort"} <= crit
    assert {"Low", "Sov"} <= warn
    assert {i["kind"] for i in board} == {"structure", "sov", "timer"}


def test_thresholds_drive_severity():
    _cfg(fuel_days=5)  # raise threshold → a 4-day structure is now low
    _structure(1, days=4)
    assert infrastructure_board()[0]["severity"] == "warning"


def test_view_renders_for_officer(client, django_user_model):
    _cfg()
    _structure(1, days=0, name="Empty")
    user, _ = enrol_pilot(django_user_model, 8800, roles=(rbac.ROLE_OFFICER,))
    client.force_login(user)
    r = client.get(reverse("corporation:infrastructure"))
    assert r.status_code == 200
    assert b"Empty" in r.content and b"Infrastructure" in r.content


def test_view_forbidden_for_member(client, django_user_model):
    user, _ = enrol_pilot(django_user_model, 8801, roles=(rbac.ROLE_MEMBER,))
    client.force_login(user)
    r = client.get(reverse("corporation:infrastructure"))
    assert r.status_code in (302, 403, 404)
