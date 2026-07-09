"""Corp-wide asset search: officer-gated lookup of where a type sits + who holds it."""
from __future__ import annotations

import pytest

from apps.corporation.models import EveName
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from apps.stockpile.models import Asset, AssetLocation
from core import rbac

RIFTER = 587  # present in the `sde` fixture as "Rifter"


def _user(django_user_model, name, role):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


@pytest.mark.django_db
def test_search_finds_corp_and_member_holdings(client, django_user_model, sde):
    loc = AssetLocation.objects.create(location_id=60003760, name="Jita IV-4", kind="station")
    Asset.objects.create(owner_type=Asset.Owner.CORPORATION, owner_id=98000001,
                         type_id=RIFTER, quantity=10, location=loc)
    Asset.objects.create(owner_type=Asset.Owner.CHARACTER, owner_id=1001,
                         type_id=RIFTER, quantity=2, location=loc)
    EveName.objects.create(entity_id=1001, name="Holder Hank", category="character")

    client.force_login(_user(django_user_model, "fc", rbac.ROLE_OFFICER))
    html = client.get("/stockpile/assets/search/?q=Rifter").content.decode()
    assert "Rifter" in html
    assert "Jita IV-4" in html
    assert "Corp" in html and "Holder Hank" in html


@pytest.mark.django_db
def test_search_is_officer_only(client, django_user_model, sde):
    client.force_login(_user(django_user_model, "m", rbac.ROLE_MEMBER))
    assert client.get("/stockpile/assets/search/?q=Rifter").status_code == 403


@pytest.mark.django_db
def test_short_query_returns_nothing(client, django_user_model, sde):
    AssetLocation.objects.create(location_id=1, name="X")
    Asset.objects.create(owner_type=Asset.Owner.CORPORATION, owner_id=1, type_id=RIFTER, quantity=1)
    client.force_login(_user(django_user_model, "fc", rbac.ROLE_OFFICER))
    html = client.get("/stockpile/assets/search/?q=R").content.decode()  # 1 char → not searched
    assert "Rifter</td>" not in html  # the asset row is not rendered
