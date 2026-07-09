"""EVE Ref killmail backfill: archive parsing + ingesting a body with no hash."""
from __future__ import annotations

import datetime as dt
import io
import json
import tarfile

import pytest

from apps.killboard.everef import day_url, iter_matching_killmails


def _archive(bodies: list[dict]) -> io.BytesIO:
    """Build an in-memory killmails-*.tar.bz2 like EVE Ref's (one killmails/{id}.json each)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for b in bodies:
            data = json.dumps(b).encode()
            info = tarfile.TarInfo(name=f"killmails/{b['killmail_id']}.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf


def test_day_url():
    assert day_url(dt.date(2021, 6, 15)) == \
        "https://data.everef.net/killmails/2021/killmails-2021-06-15.tar.bz2"


def test_iter_matches_victim_and_attacker():
    bodies = [
        {"killmail_id": 1, "victim": {"corporation_id": 999}, "attackers": [{"corporation_id": 1}]},
        {"killmail_id": 2, "victim": {"corporation_id": 5}, "attackers": [{"corporation_id": 999}]},
        {"killmail_id": 3, "victim": {"corporation_id": 7}, "attackers": [{"corporation_id": 8}]},
    ]
    got = {b["killmail_id"] for b in iter_matching_killmails(_archive(bodies), {999})}
    assert got == {1, 2}  # victim match + attacker match, not the unrelated one


def test_prefilter_does_not_cause_false_positive():
    # 9990 contains the digits "999" (passes the byte pre-filter) but is not corp 999,
    # so the precise check must still reject it.
    bodies = [
        {"killmail_id": 10, "victim": {"corporation_id": 9990, "ship_type_id": 999},
         "attackers": [{"corporation_id": 12399}]},
    ]
    assert list(iter_matching_killmails(_archive(bodies), {999})) == []


def test_empty_corp_set_yields_nothing():
    bodies = [{"killmail_id": 1, "victim": {"corporation_id": 999}, "attackers": []}]
    assert list(iter_matching_killmails(_archive(bodies), set())) == []


@pytest.mark.django_db
def test_ingest_everef_body_without_hash(settings):
    """A EVE Ref body (no hash) ingests as a real, home-corp killmail."""
    from apps.killboard.ingest import ingest_killmail
    from apps.killboard.models import Killmail
    from apps.sde.models import SdeRegion, SdeSolarSystem
    from core.mixins import Source

    settings.FORCA_HOME_CORP_ID = 999
    SdeRegion.objects.create(region_id=10000002, name="The Forge")
    SdeSolarSystem.objects.create(system_id=30000142, region_id=10000002, name="Jita", security=0.9)
    body = {
        "killmail_id": 555, "killmail_time": "2020-01-01T00:00:00Z", "solar_system_id": 30000142,
        "victim": {"character_id": 1, "corporation_id": 999, "ship_type_id": 587,
                   "damage_taken": 100, "items": []},
        "attackers": [{"character_id": 2, "corporation_id": 888, "ship_type_id": 590,
                       "final_blow": True, "damage_done": 100}],
    }
    km = ingest_killmail(555, "", source=Source.EVEREF, body=body)
    assert km.killmail_id == 555 and km.killmail_hash == ""
    assert km.source == Source.EVEREF
    assert km.involves_home_corp is True
    assert km.home_corp_role == Killmail.HomeRole.VICTIM
    # Idempotent: a second call returns the same row, no duplicate.
    again = ingest_killmail(555, "", source=Source.EVEREF, body=body)
    assert again.pk == km.pk and Killmail.objects.filter(killmail_id=555).count() == 1
