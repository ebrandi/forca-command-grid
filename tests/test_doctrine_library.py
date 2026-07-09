"""Doctrine library overview: facets, statistics, and the training plan that
power the improved /doctrines/ page."""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.fitparser import parse_eft
from apps.doctrines.library import build_library
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.doctrines.services import derive_skill_requirements
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac

RIFTER_EFT = """[Rifter, Tackle]
200mm AutoCannon I
Damage Control I
Fusion S x100
"""


def _doctrine(name, eft, *, category=None, priority=0):
    d = Doctrine.objects.create(name=name, category=category, priority=priority)
    parsed = parse_eft(eft)
    fit = DoctrineFit.objects.create(
        doctrine=d, name=name, ship_type_id=parsed["ship_type_id"],
        modules=parsed["modules"], eft_text=eft,
    )
    derive_skill_requirements(fit)
    return d, fit


@pytest.mark.django_db
def test_facets_and_stats_describe_whole_library(sde):
    cat = DoctrineCategory.objects.create(key="tackle", label="Tackle")
    _doctrine("Tackle Rifter", RIFTER_EFT, category=cat, priority=80)

    lib = build_library(character=None, has_skills=False)

    assert lib["headline"]["doctrines"] == 1
    assert lib["headline"]["fits"] == 1
    # Rifter folds into the Frigate hull class.
    assert any(h["name"] == "Frigate" for h in lib["stats"]["hull"])
    assert {c["label"] for c in lib["categories"]} == {"Tackle"}
    assert "Frigate" in lib["hull_classes"]
    # No skills imported -> training plan stays empty, not fabricated.
    assert lib["priority"]["configured"] is False
    assert lib["priority"]["skills"] == []


@pytest.mark.django_db
def test_training_plan_ranks_blocking_skills(sde, character):
    _doctrine("Tackle Rifter", RIFTER_EFT, priority=50)
    # A pilot with an empty skill sheet cannot fly the fit.
    CharacterSkillSnapshot.objects.create(character=character, skills={}, is_latest=True)

    lib = build_library(character=character, has_skills=True)

    assert lib["priority"]["configured"] is True
    skills = lib["priority"]["skills"]
    assert skills, "expected blocking skills to be surfaced"
    # Every blocking skill blocks the one fit in the library.
    assert all(s["blocks"] == 1 for s in skills)
    # Readiness reflects the gap rather than claiming flyability.
    assert lib["readiness"]["not_ready"] == 1
    assert lib["headline"]["can_fly"] == 0


@pytest.mark.django_db
def test_fully_trained_pilot_has_no_training_plan(sde, character):
    _doctrine("Tackle Rifter", RIFTER_EFT)
    CharacterSkillSnapshot.objects.create(
        character=character,
        skills={
            "3331": {"trained_level": 5}, "3301": {"trained_level": 5},
            "3300": {"trained_level": 5},
        },
        is_latest=True,
    )
    lib = build_library(character=character, has_skills=True)
    assert lib["priority"]["skills"] == []
    assert lib["readiness"]["optimal"] + lib["readiness"]["viable"] == 1
    assert lib["headline"]["can_fly"] == 1


@pytest.mark.django_db
def test_list_view_filters_by_category_and_hull(client, django_user_model, sde):
    armor = DoctrineCategory.objects.create(key="armor", label="Armor")
    _doctrine("Tackle Rifter", RIFTER_EFT, category=armor)

    user = django_user_model.objects.create(username="member")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(user)

    # No filter -> the doctrine shows.
    assert b"Tackle Rifter" in client.get("/doctrines/").content
    # Matching category -> shows.
    assert b"Tackle Rifter" in client.get(f"/doctrines/?category={armor.id}").content
    # A hull class the library doesn't contain -> filtered out.
    assert b"Tackle Rifter" not in client.get("/doctrines/?hull=Capital").content
    # Non-matching search -> filtered out.
    assert b"Tackle Rifter" not in client.get("/doctrines/?q=zzzznotreal").content


@pytest.mark.django_db
def test_doctrine_list_htmx_returns_results_fragment(client, django_user_model, sde):
    """D5: an htmx request returns only the #dc-results fragment (filtered grid),
    not the full page (form + charts stay put)."""
    armor = DoctrineCategory.objects.create(key="armor", label="Armor")
    _doctrine("Tackle Rifter", RIFTER_EFT, category=armor)
    user = django_user_model.objects.create(username="member")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(user)

    full = client.get("/doctrines/")
    assert full.status_code == 200
    assert b'id="dc-results"' in full.content and b'id="filters"' in full.content
    assert b"dc-stats-data" in full.content  # charts present on the full page

    frag = client.get("/doctrines/", HTTP_HX_REQUEST="true")
    assert frag.status_code == 200
    assert b'id="dc-results"' in frag.content and b"Tackle Rifter" in frag.content
    assert b'id="filters"' not in frag.content   # filter form is not swapped
    assert b"dc-stats-data" not in frag.content  # charts are not in the fragment
