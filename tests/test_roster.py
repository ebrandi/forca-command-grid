"""Corp roster (member tracking): import, registration status, access control."""
from __future__ import annotations

from datetime import timedelta

import pytest
import responses
from django.test import override_settings
from django.utils import timezone

from apps.corporation.models import CorpMember, EveCorporation
from apps.corporation.roster import (
    import_corp_members,
    pending_registration_count,
    roster,
)
from apps.identity.models import RoleAssignment
from apps.sso.models import AuthToken, EveCharacter
from apps.sso.services import ensure_role
from core import rbac

CORP = 98000001
TRACK = "esi-corporations.track_members.v1"


def _linked_character(django_user_model, character_id, name, *, with_token, corp_id=CORP):
    EveCorporation.objects.get_or_create(corporation_id=corp_id)
    user = django_user_model.objects.create(username=f"eve:{character_id}")
    char = EveCharacter.objects.create(
        character_id=character_id, user=user, name=name, is_main=True,
        is_corp_member=True, corporation_id=corp_id,
    )
    if with_token:
        token = AuthToken(
            character=char, scopes=[TRACK],
            access_expires_at=timezone.now() + timedelta(hours=1),
        )
        token.refresh_token = "r"
        token.access_token = "valid-access"
        token.save()
    return char


@responses.activate
@override_settings(FORCA_HOME_CORP_ID=CORP)
@pytest.mark.django_db
def test_import_corp_members_parses_tracking(django_user_model, sde):
    EveCorporation.objects.create(corporation_id=CORP)
    _linked_character(django_user_model, 1, "Director", with_token=True)

    responses.add(
        responses.GET,
        f"https://esi.evetech.net/corporations/{CORP}/membertracking/",
        json=[
            {"character_id": 1, "location_id": 60003760, "ship_type_id": 587,
             "start_date": "2020-01-01T00:00:00Z", "logon_date": "2026-06-20T10:00:00Z",
             "logoff_date": "2026-06-20T12:00:00Z", "base_id": 60003760},
            {"character_id": 2, "location_id": 60003760, "ship_type_id": 587,
             "logon_date": "2026-06-01T10:00:00Z", "logoff_date": "2026-06-01T11:00:00Z"},
        ],
        status=200,
    )

    result = import_corp_members()
    assert result["status"] == "ok" and result["count"] == 2
    m = CorpMember.objects.get(character_id=1)
    assert m.ship_type_id == 587
    assert m.logon_date is not None and m.location_id == 60003760


@override_settings(FORCA_HOME_CORP_ID=CORP)
@pytest.mark.django_db
def test_import_degrades_without_director_token(django_user_model, sde):
    # A corp member exists but holds no member-tracking token.
    _linked_character(django_user_model, 1, "Pilot", with_token=False)
    result = import_corp_members()
    assert result["status"] == "no_token"
    assert CorpMember.objects.count() == 0


@pytest.mark.django_db
def test_roster_marks_registered_vs_pending(django_user_model, sde):
    # Pilot 1: in the corp roster AND registered (linked + token).
    _linked_character(django_user_model, 1, "Registered", with_token=True)
    CorpMember.objects.create(character_id=1, corporation_id=CORP, name="Registered")
    # Pilot 2: on the roster but never connected to Command Grid.
    CorpMember.objects.create(character_id=2, corporation_id=CORP, name="Pending")
    # Pilot 3: linked but token revoked → still "to chase".
    _linked_character(django_user_model, 3, "Expired", with_token=False)
    CorpMember.objects.create(character_id=3, corporation_id=CORP, name="Expired")

    data = roster()
    by_id = {r["character_id"]: r for r in data["rows"]}
    assert by_id[1]["registered"] is True
    assert by_id[2]["registered"] is False and not by_id[2]["linked_no_token"]
    assert by_id[3]["registered"] is False and by_id[3]["linked_no_token"] is True
    assert data["registered"] == 1 and data["pending"] == 2
    assert pending_registration_count() == 2
    # Pending pilots are listed first (who to contact).
    assert data["rows"][0]["registered"] is False


@pytest.mark.django_db
def test_roster_page_is_officer_only(client, django_user_model, sde):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=9, user=member, name="M", is_corp_member=True)
    client.force_login(member)
    assert client.get("/roster/").status_code == 403

    officer = django_user_model.objects.create(username="fc")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    EveCharacter.objects.create(character_id=10, user=officer, name="FC", is_corp_member=True)
    client.force_login(officer)
    assert client.get("/roster/").status_code == 200
