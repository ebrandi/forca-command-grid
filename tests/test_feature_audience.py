"""Audience control for the doctrines + navigation features.

Same 4-state model as the member services: disabled / corp / corp+alliance / public,
with "alliance" including partner alliances and friendly corporations.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser
from django.urls import reverse

from apps.admin_audit.models import AuditLog
from apps.corporation.models import EveAlliance, EveCorporation, FriendlyCorporation
from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac
from core.features import (
    AUDIENCE_ALLIANCE,
    AUDIENCE_CORP,
    AUDIENCE_DISABLED,
    AUDIENCE_PUBLIC,
    feature_audience,
    feature_visible_to,
    set_feature_audiences,
)

HOME_CORP = 98000001
HOME_ALLIANCE = 99000001
FRIENDLY_CORP = 98007777


def _home_alliance():
    alliance = EveAlliance.objects.create(alliance_id=HOME_ALLIANCE, name="Home")
    EveCorporation.objects.create(corporation_id=HOME_CORP, name="Home Corp",
                                  alliance=alliance, is_home_corp=True)


def _member(dj, uid):
    u = dj.objects.create(username=f"fa-m{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=uid, user=u, name="M", is_main=True,
                                is_corp_member=True)
    return u


def _alliance_pilot(dj, uid):
    u = dj.objects.create(username=f"fa-a{uid}")
    EveCharacter.objects.create(character_id=uid, user=u, name="A", is_main=True,
                                is_corp_member=False, alliance_id=HOME_ALLIANCE)
    return u


def _friendly_pilot(dj, uid):
    u = dj.objects.create(username=f"fa-f{uid}")
    corp, _ = EveCorporation.objects.get_or_create(corporation_id=FRIENDLY_CORP)
    EveCharacter.objects.create(character_id=uid, user=u, name="F", is_main=True,
                                is_corp_member=False, corporation=corp)
    return u


def _outsider(dj, uid):
    return dj.objects.create(username=f"fa-o{uid}")  # logged-in, no corp/alliance/roles


# --- defaults + storage ------------------------------------------------------
@pytest.mark.django_db
def test_audience_defaults_preserve_current_behaviour():
    assert feature_audience("doctrines") == AUDIENCE_CORP     # library was member-only
    assert feature_audience("navigation") == AUDIENCE_PUBLIC  # tools were public


@pytest.mark.django_db
def test_set_feature_audiences_persists_and_merges():
    set_feature_audiences({"doctrines": AUDIENCE_ALLIANCE})
    assert feature_audience("doctrines") == AUDIENCE_ALLIANCE
    assert feature_audience("navigation") == AUDIENCE_PUBLIC  # untouched key kept
    set_feature_audiences({"navigation": AUDIENCE_DISABLED, "doctrines": "junk"})
    assert feature_audience("navigation") == AUDIENCE_DISABLED
    assert feature_audience("doctrines") == AUDIENCE_ALLIANCE  # junk ignored


# --- feature_visible_to (the can_access equivalent) --------------------------
@pytest.mark.django_db
def test_visible_disabled_is_nobody(django_user_model):
    set_feature_audiences({"doctrines": AUDIENCE_DISABLED})
    assert feature_visible_to("doctrines", _member(django_user_model, 1)) is False
    assert feature_visible_to("doctrines", AnonymousUser()) is False


@pytest.mark.django_db
def test_visible_public_is_everyone(django_user_model):
    set_feature_audiences({"navigation": AUDIENCE_PUBLIC})
    assert feature_visible_to("navigation", AnonymousUser()) is True
    assert feature_visible_to("navigation", _outsider(django_user_model, 2)) is True


@pytest.mark.django_db
def test_visible_corp_is_members_only(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_alliance()
    set_feature_audiences({"doctrines": AUDIENCE_CORP})
    assert feature_visible_to("doctrines", _member(django_user_model, 3)) is True
    assert feature_visible_to("doctrines", _alliance_pilot(django_user_model, 4)) is False
    assert feature_visible_to("doctrines", AnonymousUser()) is False


@pytest.mark.django_db
def test_visible_alliance_includes_partners_and_friendly_corps(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_alliance()
    FriendlyCorporation.objects.create(corporation_id=FRIENDLY_CORP, active=True)
    set_feature_audiences({"doctrines": AUDIENCE_ALLIANCE})
    assert feature_visible_to("doctrines", _member(django_user_model, 5)) is True
    assert feature_visible_to("doctrines", _alliance_pilot(django_user_model, 6)) is True
    assert feature_visible_to("doctrines", _friendly_pilot(django_user_model, 7)) is True
    assert feature_visible_to("doctrines", _outsider(django_user_model, 8)) is False


# --- middleware enforcement (integration) ------------------------------------
@pytest.mark.django_db
def test_navigation_public_reachable_by_anon(client):
    set_feature_audiences({"navigation": AUDIENCE_PUBLIC})
    assert client.get(reverse("navigation:route_planner")).status_code == 200


@pytest.mark.django_db
def test_navigation_corp_404s_anon_but_200s_member(client, django_user_model):
    set_feature_audiences({"navigation": AUDIENCE_CORP})
    assert client.get(reverse("navigation:route_planner")).status_code == 404
    client.force_login(_member(django_user_model, 10))
    assert client.get(reverse("navigation:route_planner")).status_code == 200


@pytest.mark.django_db
def test_navigation_disabled_404s_everyone(client, django_user_model):
    set_feature_audiences({"navigation": AUDIENCE_DISABLED})
    client.force_login(_member(django_user_model, 11))
    assert client.get(reverse("navigation:route_planner")).status_code == 404


@pytest.mark.django_db
def test_doctrines_corp_member_ok_outsider_404(client, django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_alliance()
    set_feature_audiences({"doctrines": AUDIENCE_CORP})
    client.force_login(_member(django_user_model, 12))
    assert client.get(reverse("doctrines:list")).status_code == 200
    client.force_login(_alliance_pilot(django_user_model, 13))
    assert client.get(reverse("doctrines:list")).status_code == 404


@pytest.mark.django_db
def test_doctrines_alliance_opens_to_alliance_pilot(client, django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_alliance()
    set_feature_audiences({"doctrines": AUDIENCE_ALLIANCE})
    client.force_login(_alliance_pilot(django_user_model, 14))
    assert client.get(reverse("doctrines:list")).status_code == 200


@pytest.mark.django_db
def test_doctrines_disabled_404s_member(client, django_user_model):
    set_feature_audiences({"doctrines": AUDIENCE_DISABLED})
    client.force_login(_member(django_user_model, 15))
    assert client.get(reverse("doctrines:list")).status_code == 404


@pytest.mark.django_db
def test_opened_audience_gets_content_but_not_personal_tools(client, django_user_model, settings):
    """When doctrines is opened to the alliance, an alliance pilot can browse the content
    (list + detail) but the personal readiness tools stay member-only (no dead links)."""
    from apps.doctrines.models import Doctrine

    settings.FORCA_HOME_CORP_ID = HOME_CORP
    _home_alliance()
    set_feature_audiences({"doctrines": AUDIENCE_ALLIANCE})
    doc = Doctrine.objects.create(name="Ferox Fleet")
    client.force_login(_alliance_pilot(django_user_model, 30))
    # Content views open to the alliance viewer.
    assert client.get(reverse("doctrines:list")).status_code == 200
    assert client.get(reverse("doctrines:detail", args=[doc.pk])).status_code == 200
    # Personal readiness tools stay member-only (403), even for the alliance viewer.
    assert client.get(reverse("doctrines:readiness", args=[doc.pk])).status_code == 403
    assert client.get(reverse("doctrines:my_readiness")).status_code == 403


# --- features page (Director) ------------------------------------------------
@pytest.mark.django_db
def test_features_page_renders_audience_dropdowns(client, django_user_model):
    u = django_user_model.objects.create(username="fa-dir")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_DIRECTOR))
    client.force_login(u)
    html = client.get(reverse("admin_audit:features")).content.decode()
    assert 'name="feature_audience:doctrines"' in html
    assert 'name="feature_audience:navigation"' in html
    # they must NOT also appear as an on/off checkbox
    assert 'value="doctrines"' not in html
    assert 'value="navigation"' not in html


@pytest.mark.django_db
def test_features_post_updates_feature_audience(client, django_user_model):
    u = django_user_model.objects.create(username="fa-dir2")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_DIRECTOR))
    client.force_login(u)
    resp = client.post(reverse("admin_audit:features"), {
        "feature_audience:doctrines": "alliance",
        "feature_audience:navigation": "disabled",
    })
    assert resp.status_code == 302
    assert feature_audience("doctrines") == AUDIENCE_ALLIANCE
    assert feature_audience("navigation") == AUDIENCE_DISABLED
    assert AuditLog.objects.filter(action="features.updated").exists()
