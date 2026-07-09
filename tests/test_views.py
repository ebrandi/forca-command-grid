"""View/permission smoke tests."""
from __future__ import annotations

from urllib.parse import urlparse

import pytest

from apps.doctrines.models import Doctrine, DoctrineCategory


@pytest.mark.django_db
def test_landing_ok(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Command Grid" in resp.content


@pytest.mark.django_db
def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.django_db
def test_killboard_list_ok(client):
    assert client.get("/killboard/").status_code == 200


@pytest.mark.django_db
def test_login_redirects_to_ccp(client):
    resp = client.get("/auth/eve/login/")
    assert resp.status_code == 302
    assert urlparse(resp["Location"]).hostname == "login.eveonline.com"


@pytest.mark.django_db
def test_dashboard_requires_login(client):
    resp = client.get("/dashboard/")
    assert resp.status_code == 302
    assert "/auth/eve/login/" in resp["Location"]


@pytest.mark.django_db
def test_doctrines_are_member_only(client, django_user_model):
    """Doctrines default to the 'corp' audience (Ships & doctrines feature): only corp
    members can see them. Anonymous and logged-in outsiders get the audience gate's 404;
    members get 200. (Leadership can open this to alliance/public on the Features page —
    see tests/test_feature_audience.py.)
    """
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac

    cat = DoctrineCategory.objects.create(key="c", label="C")
    doc = Doctrine.objects.create(name="Shield Ferox", category=cat)

    # Anonymous -> not in the default corp audience -> 404 (audience gate).
    assert client.get("/doctrines/").status_code == 404
    assert client.get(f"/doctrines/{doc.pk}/").status_code == 404

    # Logged-in but not a corp member -> also 404 under the default corp audience.
    outsider = django_user_model.objects.create(username="outsider")
    client.force_login(outsider)
    assert client.get("/doctrines/").status_code == 404
    assert client.get(f"/doctrines/{doc.pk}/").status_code == 404

    # Corp member -> full access.
    member = django_user_model.objects.create(username="member")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    resp = client.get("/doctrines/")
    assert resp.status_code == 200 and b"Shield Ferox" in resp.content


@pytest.mark.django_db
def test_killboard_and_onboarding_stay_public(client):
    """The only pages an anonymous visitor may see: killboard and recruitment."""
    assert client.get("/killboard/").status_code == 200
    resp = client.get("/onboarding/")
    assert resp.status_code == 200
    assert b"recruiting" in resp.content.lower() or b"Join" in resp.content
