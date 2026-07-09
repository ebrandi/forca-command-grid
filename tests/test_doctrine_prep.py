"""Pilot pre-fleet prep: shopping list diffed against owned assets."""
from __future__ import annotations

import pytest

from apps.characters.models import CharacterSkillSnapshot
from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.doctrines.prep import fit_shopping, owned_by_type
from apps.stockpile.models import Asset

RIFTER = 587
AUTOCANNON = 484


@pytest.mark.django_db
def test_shopping_list_diffs_against_owned_assets(character, sde):
    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    doctrine = Doctrine.objects.create(name="Rifter Roam", category=cat)
    fit = DoctrineFit.objects.create(
        doctrine=doctrine,
        name="Rifter",
        ship_type_id=RIFTER,
        modules=[{"type_id": AUTOCANNON, "quantity": 2, "name": "200mm AutoCannon I"}],
    )
    # The pilot already owns the hull, but none of the guns.
    Asset.objects.create(
        owner_type=Asset.Owner.CHARACTER, owner_id=character.character_id,
        type_id=RIFTER, quantity=1,
    )
    CharacterSkillSnapshot.objects.create(character=character, is_latest=True, skills={})

    owned = owned_by_type([character.character_id])
    assert owned.get(RIFTER) == 1

    result = fit_shopping(character, fit, owned)
    by_type = {line["type_id"]: line for line in result["lines"]}
    assert by_type[RIFTER]["short"] == 0       # owned → nothing to buy
    assert by_type[AUTOCANNON]["need"] == 2
    assert by_type[AUTOCANNON]["short"] == 2    # owns none
    # Multibuy only lists what's short, with quantities.
    assert "2" in result["multibuy"]
    assert str(RIFTER) not in result["multibuy"]  # hull not in the buy list
    assert result["all_owned"] is False


@pytest.mark.django_db
def test_shopping_list_all_owned(character, sde):
    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    doctrine = Doctrine.objects.create(name="Solo", category=cat)
    fit = DoctrineFit.objects.create(doctrine=doctrine, name="Rifter", ship_type_id=RIFTER, modules=[])
    Asset.objects.create(
        owner_type=Asset.Owner.CHARACTER, owner_id=character.character_id,
        type_id=RIFTER, quantity=3,
    )
    CharacterSkillSnapshot.objects.create(character=character, is_latest=True, skills={})

    result = fit_shopping(character, fit, owned_by_type([character.character_id]))
    assert result["all_owned"] is True
    assert result["multibuy"] == ""
