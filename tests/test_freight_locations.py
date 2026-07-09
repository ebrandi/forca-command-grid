"""Freight location picker: station/structure/system search, resolution, identity."""
from __future__ import annotations

import json

import pytest
import responses
from django.utils import timezone

from apps.sde.models import SdeRegion, SdeSolarSystem, SdeStation

JITA = 30000142
AMARR = 30002187


@pytest.fixture
def systems(db):
    region, _ = SdeRegion.objects.get_or_create(region_id=10000002, defaults={"name": "The Forge"})
    SdeSolarSystem.objects.update_or_create(
        system_id=JITA, defaults={"region": region, "name": "Jita", "security": 0.9}
    )
    SdeSolarSystem.objects.update_or_create(
        system_id=AMARR, defaults={"region": region, "name": "Amarr", "security": 1.0}
    )
    SdeStation.objects.create(
        station_id=60003760, name="Jita IV - Moon 4 - Caldari Navy Assembly Plant",
        system_id=JITA, system_name="Jita",
    )
    return region


# --- Local search (stations + systems) --------------------------------------
@pytest.mark.django_db
def test_station_search_matches_by_name(systems):
    from apps.sde.search import search_stations

    rows = search_stations("Caldari Navy")
    assert rows and rows[0]["system_id"] == JITA
    assert "Caldari Navy" in rows[0]["name"]


@pytest.mark.django_db
def test_location_search_returns_stations_and_systems(django_user_model, systems):
    from apps.logistics.locations import search_locations

    user = django_user_model.objects.create(username="eve:9001")
    results = search_locations(user, "Jita")
    kinds = {r["kind"] for r in results}
    assert "station" in kinds and "system" in kinds
    # Every item carries a routable system id.
    assert all(r["system_id"] for r in results)


# --- Server-side resolution (trust the SDE, not the client) -----------------
@pytest.mark.django_db
def test_resolve_location_rederives_station_and_system(systems):
    from apps.logistics.locations import resolve_location

    # Station: name/system re-derived from the SDE, client name ignored.
    st = resolve_location("station", 60003760, "SPOOFED", 999999)
    assert st["kind"] == "station" and st["system_id"] == JITA and st["name"].startswith("Jita IV")

    # System by id.
    sy = resolve_location("system", JITA, "", None)
    assert sy == {"kind": "system", "id": JITA, "name": "Jita", "system_id": JITA}

    # Structure: name trusted (bounded) but the system id must be real.
    good = resolve_location("structure", 1234567890123, "1DQ - The Keepstar", JITA)
    assert good["kind"] == "structure" and good["system_id"] == JITA
    assert resolve_location("structure", 1, "Ghost", 7777777) is None  # unknown system → rejected

    # Free-typed system name fallback.
    assert resolve_location("", "", "Amarr", None)["system_id"] == AMARR
    assert resolve_location("", "", "Nowhere-IX", None) is None


# --- ESI structure search (pilot-scoped) ------------------------------------
@responses.activate
@pytest.mark.django_db
def test_structure_search_via_pilot_token(django_user_model, systems):
    from apps.logistics.structures import READ_SCOPE, SEARCH_SCOPE, search_structures
    from apps.sso.models import AuthToken, EveCharacter

    user = django_user_model.objects.create(username="eve:9100")
    char = EveCharacter.objects.create(character_id=9100, user=user, name="Pilot", is_main=True)
    token = AuthToken(
        character=char, scopes=[SEARCH_SCOPE, READ_SCOPE],
        access_expires_at=timezone.now() + timezone.timedelta(hours=1),
    )
    token.refresh_token = "r"
    token.access_token = "a"
    token.save()

    responses.add(
        responses.GET, "https://esi.evetech.net/characters/9100/search/",
        body=json.dumps({"structure": [1035466617946]}), status=200,
        content_type="application/json",
    )
    responses.add(
        responses.GET, "https://esi.evetech.net/universe/structures/1035466617946/",
        body=json.dumps({"name": "Jita - The Fortizar", "solar_system_id": JITA, "type_id": 35833}),
        status=200, content_type="application/json",
    )
    results = search_structures(user, "Fortizar")
    assert len(results) == 1
    assert results[0]["kind"] == "structure"
    assert results[0]["name"] == "Jita - The Fortizar"
    assert results[0]["system_id"] == JITA and results[0]["system_name"] == "Jita"


@pytest.mark.django_db
def test_structure_search_empty_without_scope(django_user_model, systems):
    """A pilot with no scoped token gets no structures (and no error)."""
    from apps.logistics.structures import has_structure_search, search_structures

    user = django_user_model.objects.create(username="eve:9200")
    assert has_structure_search(user) is False
    assert search_structures(user, "Fortizar") == []


# --- Posting identity (own pilot vs corp) -----------------------------------
@pytest.mark.django_db
def test_poster_identity_self_and_corp(django_user_model, systems):
    from apps.corporation.models import EveCorporation
    from apps.logistics.services import poster_identity
    from apps.sso.models import EveCharacter

    corp = EveCorporation.objects.create(corporation_id=98000001, name="Forças Armadas")
    user = django_user_model.objects.create(username="eve:9300")
    EveCharacter.objects.create(
        character_id=9300, user=user, name="Captain", is_main=True, corporation=corp,
    )
    me = poster_identity(user, "character")
    assert me == {"kind": "character", "id": 9300, "name": "Captain"}
    corp_id = poster_identity(user, "corporation")
    assert corp_id["kind"] == "corporation" and corp_id["name"] == "Forças Armadas"
