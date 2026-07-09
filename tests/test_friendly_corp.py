"""Friendly corporations get the same alliance-service access as the home alliance,
plus the Director-gated Access-governance console CRUD (partner alliances + friendly corps).
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.admin_audit.models import AuditLog
from apps.corporation.access import is_service_alliance_pilot, service_corp_ids
from apps.corporation.models import (
    EveAlliance,
    EveCorporation,
    FriendlyCorporation,
    PartnerAlliance,
)
from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

HOME_CORP = 98000001
HOME_ALLIANCE = 99000001
FRIENDLY_CORP = 98007777
INACTIVE_CORP = 98008888
OTHER_CORP = 98009999


def _home_corp_in_alliance(alliance_id):
    alliance = EveAlliance.objects.create(alliance_id=alliance_id, name="Home Alliance")
    return EveCorporation.objects.create(
        corporation_id=HOME_CORP, name="Home Corp", alliance=alliance, is_home_corp=True)


def _corp_pilot(django_user_model, username, corporation_id):
    user = django_user_model.objects.create(username=username)
    corp, _ = EveCorporation.objects.get_or_create(corporation_id=corporation_id)
    EveCharacter.objects.create(
        character_id=int(username.split(":")[1]), user=user, name="P",
        is_main=True, is_corp_member=False, corporation=corp)
    return user


def _user(django_user_model, uid, role):
    u = django_user_model.objects.create(username=f"fc-{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


# --- access layer ------------------------------------------------------------
@pytest.mark.django_db
def test_service_corp_ids_active_only():
    FriendlyCorporation.objects.create(corporation_id=FRIENDLY_CORP, active=True)
    FriendlyCorporation.objects.create(corporation_id=INACTIVE_CORP, active=False)
    ids = service_corp_ids()
    assert FRIENDLY_CORP in ids and INACTIVE_CORP not in ids


@pytest.mark.django_db
def test_friendly_corp_pilot_recognised(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_corp_in_alliance(HOME_ALLIANCE)
    FriendlyCorporation.objects.create(corporation_id=FRIENDLY_CORP, active=True)
    pilot = _corp_pilot(django_user_model, "eve:8901", FRIENDLY_CORP)
    outsider = _corp_pilot(django_user_model, "eve:8902", OTHER_CORP)
    assert is_service_alliance_pilot(pilot) is True
    assert is_service_alliance_pilot(outsider) is False


@pytest.mark.django_db
def test_friendly_corp_pilot_can_access_every_service(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_corp_in_alliance(HOME_ALLIANCE)
    FriendlyCorporation.objects.create(corporation_id=FRIENDLY_CORP, active=True)
    pilot = _corp_pilot(django_user_model, "eve:8911", FRIENDLY_CORP)
    outsider = _corp_pilot(django_user_model, "eve:8912", OTHER_CORP)

    from apps.buyback.services import can_access as bb_access
    from apps.buyback.services import invalidate_audience_cache as bb_inv
    from apps.logistics.services import can_access as lg_access
    from apps.logistics.services import invalidate_audience_cache as lg_inv
    from apps.store.services import can_access as st_access
    from apps.store.services import invalidate_audience_cache as st_inv

    for inv in (bb_inv, lg_inv, st_inv):
        inv()
    for access in (bb_access, lg_access, st_access):
        assert access(pilot) is True
        assert access(outsider) is False


@pytest.mark.django_db
def test_deactivating_friendly_corp_revokes_access(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_corp_in_alliance(HOME_ALLIANCE)
    fc = FriendlyCorporation.objects.create(corporation_id=FRIENDLY_CORP, active=True)
    pilot = _corp_pilot(django_user_model, "eve:8921", FRIENDLY_CORP)
    assert is_service_alliance_pilot(pilot) is True
    fc.active = False
    fc.save(update_fields=["active"])
    assert is_service_alliance_pilot(pilot) is False  # access follows the toggle live


@pytest.mark.django_db
def test_friendly_corp_nav_flag(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_corp_in_alliance(HOME_ALLIANCE)
    FriendlyCorporation.objects.create(corporation_id=FRIENDLY_CORP, active=True)
    pilot = _corp_pilot(django_user_model, "eve:8931", FRIENDLY_CORP)

    from django.test import RequestFactory

    from core.context import roles

    request = RequestFactory().get("/")
    request.user = pilot
    assert roles(request)["is_alliance"] is True


# --- console CRUD (Director-gated) -------------------------------------------
@pytest.mark.django_db
def test_access_page_blocks_officer(client, django_user_model):
    client.force_login(_user(django_user_model, 1, rbac.ROLE_OFFICER))
    assert client.get(reverse("admin_audit:access_governance")).status_code == 403


@pytest.mark.django_db
def test_director_access_page_renders_both_sections(client, django_user_model):
    client.force_login(_user(django_user_model, 2, rbac.ROLE_DIRECTOR))
    resp = client.get(reverse("admin_audit:access_governance"))
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Partner alliances" in html and "Friendly corporations" in html


@pytest.mark.django_db
def test_director_crud_friendly_corp(client, django_user_model):
    client.force_login(_user(django_user_model, 3, rbac.ROLE_DIRECTOR))
    client.post(reverse("admin_audit:access_friendly_corp_create"),
                {"entity_id": FRIENDLY_CORP, "name": "Buddies", "active": "on"})
    fc = FriendlyCorporation.objects.get(corporation_id=FRIENDLY_CORP)
    assert fc.name == "Buddies" and fc.active is True
    assert AuditLog.objects.filter(action="access.friendly_corp.create").exists()

    # Update with no "active" checkbox → deactivates it.
    client.post(reverse("admin_audit:access_friendly_corp_update", args=[FRIENDLY_CORP]),
                {"name": "Buddies", "note": "trusted"})
    fc.refresh_from_db()
    assert fc.active is False and fc.note == "trusted"

    client.post(reverse("admin_audit:access_friendly_corp_delete", args=[FRIENDLY_CORP]))
    assert not FriendlyCorporation.objects.filter(corporation_id=FRIENDLY_CORP).exists()


@pytest.mark.django_db
def test_director_crud_partner_alliance(client, django_user_model):
    client.force_login(_user(django_user_model, 4, rbac.ROLE_DIRECTOR))
    client.post(reverse("admin_audit:access_partner_alliance_create"),
                {"entity_id": 99000123, "name": "Allies", "active": "on"})
    assert PartnerAlliance.objects.filter(alliance_id=99000123).exists()
    assert AuditLog.objects.filter(action="access.partner_alliance.create").exists()

    client.post(reverse("admin_audit:access_partner_alliance_delete", args=[99000123]))
    assert not PartnerAlliance.objects.filter(alliance_id=99000123).exists()
