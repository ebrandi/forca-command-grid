"""zKillboard enrichment adapter + import task tests."""
from __future__ import annotations

import pytest
import responses

from apps.killboard.models import Killmail
from apps.killboard.tasks import import_from_zkill
from core.esi.adapters import zkill


@responses.activate
@pytest.mark.django_db
def test_corp_refs_parsing():
    responses.add(
        responses.GET,
        "https://zkillboard.com/api/corporationID/98493095/",
        json=[
            {"killmail_id": 111, "zkb": {"hash": "h1"}},
            {"killmail_id": 222, "zkb": {"hash": "h2"}},
            {"killmail_id": 333, "zkb": {}},  # no hash -> skipped
        ],
        status=200,
    )
    refs = zkill.corporation_killmail_refs(98493095)
    assert refs == [(111, "h1"), (222, "h2")]


@responses.activate
@pytest.mark.django_db
def test_import_from_zkill_ingests_via_esi(sde):
    responses.add(
        responses.GET,
        "https://zkillboard.com/api/corporationID/98493095/",
        json=[{"killmail_id": 111, "zkb": {"hash": "h1"}}],
        status=200,
    )
    responses.add(
        responses.GET,
        "https://esi.evetech.net/killmails/111/h1/",
        json={
            "killmail_id": 111,
            "killmail_time": "2026-06-20T10:00:00Z",
            "solar_system_id": 30002053,
            "victim": {"corporation_id": 98493095, "ship_type_id": 587, "items": []},
            "attackers": [{"character_id": 9, "corporation_id": 99}],
        },
        status=200,
    )
    n = import_from_zkill("corporation", 98493095)
    assert n == 1
    km = Killmail.objects.get(killmail_id=111)
    assert km.source == "zkill"
    assert km.victim_corporation_id == 98493095
