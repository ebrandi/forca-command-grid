"""Doctrine readiness, requirement derivation, and fit parsing tests."""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.fitparser import export_eft, parse_eft
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.doctrines.services import (
    character_readiness,
    derive_skill_requirements,
    doctrine_coverage,
)

RIFTER_EFT = """[Rifter, Tackle]
200mm AutoCannon I
200mm AutoCannon I
Damage Control I
Fusion S x100
"""


def _make_fit():
    doctrine = Doctrine.objects.create(
        name="Tackle", category=DoctrineCategory.objects.create(key="tackle", label="Tackle")
    )
    parsed = parse_eft(RIFTER_EFT)
    return DoctrineFit.objects.create(
        doctrine=doctrine,
        name="Tackle",
        ship_type_id=parsed["ship_type_id"],
        modules=parsed["modules"],
        eft_text=RIFTER_EFT,
    )


@pytest.mark.django_db
def test_parse_eft_resolves_types(sde):
    parsed = parse_eft(RIFTER_EFT)
    assert parsed["ship_type_id"] == 587  # Rifter
    type_ids = {m["type_id"] for m in parsed["modules"]}
    assert 484 in type_ids  # 200mm AutoCannon I
    assert 192 in type_ids  # Fusion S
    assert not parsed["unresolved"]


@pytest.mark.django_db
def test_export_eft_roundtrip(sde):
    fit = _make_fit()
    text = export_eft(fit)
    assert text.startswith("[Rifter, Tackle]")
    assert "200mm AutoCannon I" in text


@pytest.mark.django_db
def test_derive_skill_requirements(sde):
    fit = _make_fit()
    created = derive_skill_requirements(fit)
    assert created >= 2
    reqs = {r.skill_type_id: r.min_level for r in fit.skill_requirements.all()}
    # Rifter -> Minmatar Frigate; AutoCannon -> Small Projectile Turret; DC -> Gunnery
    assert 3331 in reqs
    assert 3301 in reqs


@pytest.mark.django_db
def test_readiness_unknown_without_snapshot(sde, character):
    fit = _make_fit()
    derive_skill_requirements(fit)
    r = character_readiness(character, fit)
    assert r.status == "unknown"  # never "not_ready" when un-imported


@pytest.mark.django_db
def test_readiness_optimal_and_not_ready(sde, character):
    fit = _make_fit()
    derive_skill_requirements(fit)

    # No relevant skills -> not ready.
    CharacterSkillSnapshot.objects.create(character=character, skills={}, is_latest=True)
    assert character_readiness(character, fit).status == "not_ready"

    # All required skills trained -> optimal.
    character.skill_snapshots.update(is_latest=False)
    CharacterSkillSnapshot.objects.create(
        character=character,
        skills={"3331": {"trained_level": 5}, "3301": {"trained_level": 5}, "3300": {"trained_level": 5}},
        is_latest=True,
    )
    assert character_readiness(character, fit).status == "optimal"


@pytest.mark.django_db
def test_doctrine_coverage_counts(sde, character):
    fit = _make_fit()
    derive_skill_requirements(fit)
    CharacterSkillSnapshot.objects.create(
        character=character,
        skills={"3331": {"trained_level": 5}, "3301": {"trained_level": 5}, "3300": {"trained_level": 5}},
        is_latest=True,
    )
    counts = doctrine_coverage(fit.doctrine, [character])
    assert counts["optimal"] == 1
