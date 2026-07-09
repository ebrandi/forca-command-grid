"""Skill import service tests."""
from __future__ import annotations

import pytest
import responses
from django.utils import timezone

from apps.characters.services import import_character_skills
from apps.sso.models import AuthToken


@responses.activate
@pytest.mark.django_db
def test_import_character_skills(character):
    # Give the character a valid (non-expired) token with the skills scope.
    token = AuthToken(
        character=character,
        scopes=["esi-skills.read_skills.v1"],
        access_expires_at=timezone.now() + timezone.timedelta(hours=1),
    )
    token.refresh_token = "r"
    token.access_token = "valid-access"
    token.save()

    responses.add(
        responses.GET,
        "https://esi.evetech.net/characters/1001/skills/",
        json={
            "total_sp": 1_500_000,
            "skills": [
                {"skill_id": 3331, "trained_skill_level": 4, "skillpoints_in_skill": 90510},
                {"skill_id": 3301, "trained_skill_level": 3, "skillpoints_in_skill": 8000},
            ],
        },
        status=200,
    )

    snapshot = import_character_skills(character)
    assert snapshot.is_latest is True
    assert snapshot.total_sp == 1_500_000
    assert snapshot.trained_level(3331) == 4
    assert snapshot.trained_level(3301) == 3

    # Re-import marks the old snapshot non-latest.
    responses.add(
        responses.GET,
        "https://esi.evetech.net/characters/1001/skills/",
        json={"total_sp": 1_600_000, "skills": []},
        status=200,
    )
    new_snapshot = import_character_skills(character)
    assert character.skill_snapshots.filter(is_latest=True).count() == 1
    assert new_snapshot.pk != snapshot.pk
