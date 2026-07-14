"""A signed-out visitor on a corp-only page gets sent to log in, not told it does not exist.

``FeatureGateMiddleware`` runs in ``process_view`` — *before* the view's own
``@login_required``. A signed-out pilot fails every audience except ``public``, so the gate
used to raise ``Http404`` and the view's login redirect never ran. A member who followed a
link to ``/doctrines/`` while logged out was told the page did not exist, with nothing to
suggest that signing in would have shown it. That is the bug these tests pin.

The non-leak property is deliberate and is kept: an *identified* pilot who is outside the
audience still gets a 404, because for them authenticating has already happened and the
answer is genuinely "not for you". Only the "we do not know who you are yet" case redirects.

A ``disabled`` audience is different in kind — the feature is off for everybody, so logging
in cannot change the answer and a login round-trip would be a lie. That stays a 404.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.corporation.models import EveAlliance, EveCorporation
from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac
from core.features import (
    AUDIENCE_ALLIANCE,
    AUDIENCE_CORP,
    AUDIENCE_DISABLED,
    AUDIENCE_PUBLIC,
    set_feature_audiences,
)

HOME_CORP = 98000001
HOME_ALLIANCE = 99000001

# Both audience-controlled features that have a plain, no-argument landing page.
CORP_ONLY_URLS = ["/doctrines/", "/raffle/"]


@pytest.fixture
def home_corp(settings):
    settings.FORCA_HOME_CORP_ID = HOME_CORP
    alliance = EveAlliance.objects.create(alliance_id=HOME_ALLIANCE, name="Home")
    EveCorporation.objects.create(
        corporation_id=HOME_CORP, name="Home Corp", alliance=alliance, is_home_corp=True
    )


def _member(dj):
    u = dj.objects.create(username="fga-member")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(
        character_id=7001, user=u, name="M", is_main=True, is_corp_member=True
    )
    return u


def _alliance_pilot(dj):
    u = dj.objects.create(username="fga-alliance")
    EveCharacter.objects.create(
        character_id=7002, user=u, name="A", is_main=True,
        is_corp_member=False, alliance_id=HOME_ALLIANCE,
    )
    return u


@pytest.mark.django_db
@pytest.mark.parametrize("url", CORP_ONLY_URLS)
def test_anonymous_visitor_is_redirected_to_login_not_404ed(client, home_corp, url):
    set_feature_audiences({"doctrines": AUDIENCE_CORP, "raffle": AUDIENCE_CORP})

    resp = client.get(url)

    assert resp.status_code == 302, f"{url} must redirect a signed-out visitor, not {resp.status_code}"
    assert resp.url.startswith(reverse("sso:login")), resp.url
    assert f"next={url}" in resp.url, f"the login redirect must come back to {url}: {resp.url}"


@pytest.mark.django_db
@pytest.mark.parametrize("audience", [AUDIENCE_CORP, AUDIENCE_ALLIANCE])
def test_the_redirect_applies_to_every_audience_that_authentication_could_satisfy(
    client, home_corp, audience
):
    """corp and alliance both mean "sign in and we will see" — only public lets you straight in."""
    set_feature_audiences({"doctrines": audience})
    resp = client.get("/doctrines/")
    assert resp.status_code == 302, f"audience={audience} must redirect, got {resp.status_code}"


@pytest.mark.django_db
def test_a_disabled_feature_still_404s_an_anonymous_visitor(client, home_corp):
    """Logging in cannot reveal a feature that is off for everyone — do not send them on a trip."""
    set_feature_audiences({"doctrines": AUDIENCE_DISABLED})
    assert client.get("/doctrines/").status_code == 404


@pytest.mark.django_db
def test_an_identified_outsider_still_gets_404_not_a_login_loop(client, django_user_model, home_corp):
    """The non-leak property survives: we tell a known non-member nothing, and we do not
    bounce an already-authenticated pilot to a login page they have just come from."""
    set_feature_audiences({"doctrines": AUDIENCE_CORP})
    client.force_login(_alliance_pilot(django_user_model))
    assert client.get("/doctrines/").status_code == 404


@pytest.mark.django_db
def test_a_member_still_reaches_the_page(client, django_user_model, home_corp):
    set_feature_audiences({"doctrines": AUDIENCE_CORP})
    client.force_login(_member(django_user_model))
    assert client.get("/doctrines/").status_code == 200


@pytest.mark.django_db
def test_a_public_audience_is_untouched_by_the_gate(client, home_corp):
    """The gate must not start redirecting anonymous visitors away from public pages."""
    set_feature_audiences({"navigation": AUDIENCE_PUBLIC})
    assert client.get("/tools/route/").status_code == 200
