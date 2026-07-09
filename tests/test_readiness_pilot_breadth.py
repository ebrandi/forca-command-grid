"""Milestone — pilot recommendation breadth: ship / logistics / role + the two
previously-empty facets (logistics, strategic).

Backs G3 from the completeness report: the quest log now serves the mandatory-ship,
staging-logistics and strategic-role personas, not only trainers. Industry/asset-fit
categories remain a documented follow-up (no per-pilot build/fit-completeness signal).
"""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.identity.models import RoleAssignment
from apps.readiness.models import MandatoryShip, PilotRecommendation, StrategicRoleTarget
from apps.readiness.pilot import compute_pilot
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

GUNNERY = 3300
RIFTER = 587
JITA = 30000142
AMARR = 30002187


def _pilot(django_user_model, cid=9001, gunnery_level=5):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    ch = EveCharacter.objects.create(
        character_id=cid, user=user, name=f"P{cid}", is_main=True, is_corp_member=True
    )
    CharacterSkillSnapshot.objects.create(
        character=ch, is_latest=True, skills={str(GUNNERY): {"trained_level": gunnery_level, "sp": 0}}
    )
    return user, ch


def _asset(character_id, type_id, *, system_id=None, quantity=1):
    from apps.stockpile.models import Asset, AssetLocation

    loc = None
    if system_id is not None:
        loc, _ = AssetLocation.objects.get_or_create(
            location_id=60000000 + system_id, defaults={"system_id": system_id, "name": "Stn"}
        )
    return Asset.objects.create(
        owner_type=Asset.Owner.CHARACTER, owner_id=character_id, location=loc,
        type_id=type_id, quantity=quantity,
    )


# --- strategic facet + role recommendation -----------------------------------
@pytest.mark.django_db
def test_strategic_facet_none_without_role_targets(django_user_model, sde):
    _, ch = _pilot(django_user_model)
    assert compute_pilot(ch, persist=False)["facets"]["strategic"] is None


@pytest.mark.django_db
def test_strategic_facet_and_volunteer_reco(django_user_model, sde):
    # A scarce skill-detected role the pilot already qualifies for.
    StrategicRoleTarget.objects.create(
        role_key="logi", label="Logistics", desired_count=3,
        detection=StrategicRoleTarget.Detection.SKILLS, detection_params={"skills": {str(GUNNERY): 5}},
    )
    user, ch = _pilot(django_user_model, gunnery_level=5)  # meets the skill
    result = compute_pilot(ch)
    assert result["facets"]["strategic"] == 100  # qualifies for the one configured role
    assert PilotRecommendation.objects.filter(
        user=user, category=PilotRecommendation.Category.ROLE, ref_id="logi"
    ).exists()


@pytest.mark.django_db
def test_strategic_no_reco_when_role_already_staffed(django_user_model, sde):
    # desired_count=1 and the pilot qualifies → not scarce → no volunteer reco, facet still 100.
    StrategicRoleTarget.objects.create(
        role_key="logi", label="Logistics", desired_count=1,
        detection=StrategicRoleTarget.Detection.SKILLS, detection_params={"skills": {str(GUNNERY): 5}},
    )
    user, ch = _pilot(django_user_model, gunnery_level=5)
    result = compute_pilot(ch)
    assert result["facets"]["strategic"] == 100
    assert not PilotRecommendation.objects.filter(
        user=user, category=PilotRecommendation.Category.ROLE, ref_id="logi"
    ).exists()


@pytest.mark.django_db
def test_strategic_facet_zero_when_unqualified(django_user_model, sde):
    StrategicRoleTarget.objects.create(
        role_key="logi", label="Logistics", desired_count=3,
        detection=StrategicRoleTarget.Detection.SKILLS, detection_params={"skills": {str(GUNNERY): 5}},
    )
    _, ch = _pilot(django_user_model, gunnery_level=1)  # below the skill
    assert compute_pilot(ch, persist=False)["facets"]["strategic"] == 0


# --- ship + logistics facet/recommendations ----------------------------------
@pytest.mark.django_db
def test_logistics_facet_none_without_mandatory_ships(django_user_model, sde):
    _, ch = _pilot(django_user_model)
    assert compute_pilot(ch, persist=False)["facets"]["logistics"] is None


@pytest.mark.django_db
def test_ship_reco_when_hull_not_owned(django_user_model, sde):
    MandatoryShip.objects.create(label="Rifter", ship_type_id=RIFTER, required_quantity=1)
    user, ch = _pilot(django_user_model)
    compute_pilot(ch)
    assert PilotRecommendation.objects.filter(
        user=user, category=PilotRecommendation.Category.SHIP
    ).exists()


@pytest.mark.django_db
def test_logistics_facet_full_when_hull_at_staging(django_user_model, sde):
    MandatoryShip.objects.create(label="Rifter", ship_type_id=RIFTER, required_quantity=1,
                                 required_system_id=JITA)
    user, ch = _pilot(django_user_model)
    _asset(ch.character_id, RIFTER, system_id=JITA, quantity=1)
    result = compute_pilot(ch)
    assert result["facets"]["logistics"] == 100
    # Owns it and it's home → neither a ship nor a logistics reco.
    assert not PilotRecommendation.objects.filter(
        user=user, category__in=[PilotRecommendation.Category.SHIP, PilotRecommendation.Category.LOGISTICS]
    ).exists()


@pytest.mark.django_db
def test_logistics_reco_when_hull_not_at_staging(django_user_model, sde):
    MandatoryShip.objects.create(label="Rifter", ship_type_id=RIFTER, required_quantity=1,
                                 required_system_id=JITA)
    user, ch = _pilot(django_user_model)
    _asset(ch.character_id, RIFTER, system_id=AMARR, quantity=1)  # owned, wrong system
    result = compute_pilot(ch)
    assert result["facets"]["logistics"] == 0
    assert PilotRecommendation.objects.filter(
        user=user, category=PilotRecommendation.Category.LOGISTICS
    ).exists()


@pytest.mark.django_db
def test_quest_log_capped_and_ranked(django_user_model, sde):
    # Many mandatory ships → many ship recos, but the log keeps only the strongest dozen.
    for i in range(20):
        MandatoryShip.objects.create(label=f"Hull{i}", ship_type_id=1000 + i, required_quantity=1)
    user, ch = _pilot(django_user_model)
    result = compute_pilot(ch)
    assert len(result["recommendations"]) <= 12
    # Ranked by priority (descending).
    pr = [r["priority"] for r in result["recommendations"]]
    assert pr == sorted(pr, reverse=True)
