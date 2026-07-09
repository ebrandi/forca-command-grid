"""Gap C2 — fitting-state from ESI assets + pilot asset/fit recommendations.

Verifies ``extract_fitted_ships`` (which modules are fitted to which hull, from the raw
ESI assets) and the readiness asset/fit recos: a doctrine hull the pilot owns but hasn't
fitted to a doctrine (unfitted, or fitted but missing doctrine modules).
"""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterFittedShip
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.identity.models import RoleAssignment
from apps.readiness.pilot import _asset_fit
from apps.sde.models import SdeCategory, SdeGroup, SdeType
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.stockpile.assets import extract_fitted_ships
from apps.stockpile.models import Asset
from core import rbac

RIFTER = 587
AUTOCANNON = 486
SCRAM = 5443
STATION = 60003760


# --- extract_fitted_ships ----------------------------------------------------
def test_extract_fitted_ships_picks_slot_modules_only():
    assets = [
        {"item_id": 1000, "type_id": RIFTER, "location_id": STATION, "location_flag": "Hangar", "quantity": 1},
        {"item_id": 1001, "type_id": AUTOCANNON, "location_id": 1000, "location_flag": "HiSlot0", "quantity": 1},
        {"item_id": 1002, "type_id": AUTOCANNON, "location_id": 1000, "location_flag": "HiSlot1", "quantity": 1},
        {"item_id": 1003, "type_id": SCRAM, "location_id": 1000, "location_flag": "MedSlot0", "quantity": 1},
        # loose in the same station hangar — NOT fitted
        {"item_id": 1004, "type_id": AUTOCANNON, "location_id": STATION, "location_flag": "Hangar", "quantity": 5},
    ]
    fits = extract_fitted_ships(assets)
    assert set(fits) == {1000}
    ship = fits[1000]
    assert ship["ship_type_id"] == RIFTER and ship["location_id"] == STATION
    assert ship["modules"] == {str(AUTOCANNON): 2, str(SCRAM): 1}  # the loose 5 are excluded


def test_extract_handles_no_ships():
    assert extract_fitted_ships([{"item_id": 1, "type_id": 34, "location_id": 9, "location_flag": "Hangar"}]) == {}


# --- _asset_fit recommendations ---------------------------------------------
def _pilot(django_user_model, cid=8401):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    ch = EveCharacter.objects.create(character_id=cid, name=f"P{cid}", is_main=True,
                                     is_corp_member=True, user=user)
    return user, ch


def _rifter_doctrine(modules):
    SdeCategory.objects.get_or_create(category_id=6, defaults={"name": "Ship"})
    grp, _ = SdeGroup.objects.get_or_create(
        group_id=25, defaults={"category": SdeCategory.objects.get(category_id=6), "name": "Frigate"})
    SdeType.objects.get_or_create(type_id=RIFTER, defaults={"name": "Rifter", "group": grp, "published": True})
    cat, _ = DoctrineCategory.objects.get_or_create(key="tackle", label="Tackle")
    d = Doctrine.objects.create(name="Tackle", category=cat, priority=100)
    DoctrineFit.objects.create(doctrine=d, name="Newbro Rifter", ship_type_id=RIFTER, modules=modules)
    return d


def _own_hull(ch, type_id):
    Asset.objects.create(owner_type=Asset.Owner.CHARACTER, owner_id=ch.character_id,
                         location=None, type_id=type_id, quantity=1)


@pytest.mark.django_db
def test_unfitted_owned_hull_gets_reco(django_user_model):
    _, ch = _pilot(django_user_model)
    _rifter_doctrine([{"type_id": AUTOCANNON, "quantity": 3}])
    _own_hull(ch, RIFTER)  # owns it, but no CharacterFittedShip → unfitted
    recos = _asset_fit(ch)
    assert len(recos) == 1 and recos[0]["category"] == "asset"
    assert "Fit your Rifter" in recos[0]["title"]


@pytest.mark.django_db
def test_complete_fit_no_reco(django_user_model):
    _, ch = _pilot(django_user_model, 8402)
    _rifter_doctrine([{"type_id": AUTOCANNON, "quantity": 3}])
    _own_hull(ch, RIFTER)
    CharacterFittedShip.objects.create(character=ch, item_id=1, ship_type_id=RIFTER,
                                       modules={str(AUTOCANNON): 3}, is_latest=True)
    assert _asset_fit(ch) == []  # required module present → satisfied


@pytest.mark.django_db
def test_incomplete_fit_gets_reco(django_user_model):
    _, ch = _pilot(django_user_model, 8403)
    _rifter_doctrine([{"type_id": AUTOCANNON, "quantity": 3}, {"type_id": SCRAM, "quantity": 1}])
    _own_hull(ch, RIFTER)
    CharacterFittedShip.objects.create(character=ch, item_id=1, ship_type_id=RIFTER,
                                       modules={str(AUTOCANNON): 3}, is_latest=True)  # missing SCRAM
    recos = _asset_fit(ch)
    assert len(recos) == 1 and "Finish your Rifter fit" in recos[0]["title"]


@pytest.mark.django_db
def test_not_owned_no_reco(django_user_model):
    _, ch = _pilot(django_user_model, 8404)
    _rifter_doctrine([{"type_id": AUTOCANNON, "quantity": 3}])
    # owns nothing → no nag
    assert _asset_fit(ch) == []
