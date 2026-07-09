"""Extra (partner) alliances granted access to the corp's alliance services.

Corp members and pilots of the corp's own alliance always have access; an
admin-registered active partner alliance grants the same access to its pilots.
"""
from __future__ import annotations

import pytest

from apps.corporation.access import is_service_alliance_pilot, service_alliance_ids
from apps.corporation.models import EveAlliance, EveCorporation, PartnerAlliance
from apps.sso.models import EveCharacter

HOME_CORP = 98000001
HOME_ALLIANCE = 99000001
PARTNER_ALLIANCE = 99000777
INACTIVE_ALLIANCE = 99000888
OTHER_ALLIANCE = 99009999


def _home_corp_in_alliance(alliance_id):
    alliance = EveAlliance.objects.create(alliance_id=alliance_id, name="Home Alliance")
    return EveCorporation.objects.create(
        corporation_id=HOME_CORP, name="Home Corp", alliance=alliance, is_home_corp=True
    )


def _pilot(django_user_model, username, alliance_id, *, corp_member=False):
    user = django_user_model.objects.create(username=username)
    EveCharacter.objects.create(
        character_id=int(username.split(":")[1]), user=user, name="P",
        is_main=True, is_corp_member=corp_member, alliance_id=alliance_id,
    )
    return user


@pytest.mark.django_db
def test_service_alliance_ids_includes_home_and_active_partners(settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_corp_in_alliance(HOME_ALLIANCE)
    PartnerAlliance.objects.create(alliance_id=PARTNER_ALLIANCE, name="Partner", active=True)
    PartnerAlliance.objects.create(alliance_id=INACTIVE_ALLIANCE, name="Inactive", active=False)

    ids = service_alliance_ids()
    assert HOME_ALLIANCE in ids
    assert PARTNER_ALLIANCE in ids
    assert INACTIVE_ALLIANCE not in ids  # inactive partners don't grant access


@pytest.mark.django_db
def test_partner_alliance_pilot_is_recognised(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_corp_in_alliance(HOME_ALLIANCE)
    PartnerAlliance.objects.create(alliance_id=PARTNER_ALLIANCE, active=True)

    partner_pilot = _pilot(django_user_model, "eve:8501", PARTNER_ALLIANCE)
    home_ally_pilot = _pilot(django_user_model, "eve:8502", HOME_ALLIANCE)
    outsider = _pilot(django_user_model, "eve:8503", OTHER_ALLIANCE)

    assert is_service_alliance_pilot(partner_pilot) is True
    assert is_service_alliance_pilot(home_ally_pilot) is True
    assert is_service_alliance_pilot(outsider) is False


@pytest.mark.django_db
def test_partner_pilot_can_access_every_alliance_service(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_corp_in_alliance(HOME_ALLIANCE)
    PartnerAlliance.objects.create(alliance_id=PARTNER_ALLIANCE, active=True)
    pilot = _pilot(django_user_model, "eve:8601", PARTNER_ALLIANCE)
    outsider = _pilot(django_user_model, "eve:8602", OTHER_ALLIANCE)

    # All three default to the ALLIANCE audience; clear their audience caches.
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
def test_deactivating_partner_revokes_access(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_corp_in_alliance(HOME_ALLIANCE)
    partner = PartnerAlliance.objects.create(alliance_id=PARTNER_ALLIANCE, active=True)
    pilot = _pilot(django_user_model, "eve:8701", PARTNER_ALLIANCE)
    assert is_service_alliance_pilot(pilot) is True

    partner.active = False
    partner.save(update_fields=["active"])
    assert is_service_alliance_pilot(pilot) is False  # access follows the toggle live


@pytest.mark.django_db
def test_alliance_nav_flag_true_for_partner_pilot(client, django_user_model, settings):
    """A registered partner-alliance pilot sees the alliance services in the nav."""
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_corp_in_alliance(HOME_ALLIANCE)
    PartnerAlliance.objects.create(alliance_id=PARTNER_ALLIANCE, active=True)
    pilot = _pilot(django_user_model, "eve:8801", PARTNER_ALLIANCE)

    from django.test import RequestFactory

    from core.context import roles

    request = RequestFactory().get("/")
    request.user = pilot
    ctx = roles(request)
    assert ctx["is_alliance"] is True
    assert ctx["is_member"] is False
