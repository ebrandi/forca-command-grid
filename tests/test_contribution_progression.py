"""Skill-training + doctrine-unlock contribution credit (apps.pilots.progression)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.doctrines.fitparser import parse_eft
from apps.doctrines.models import Doctrine, DoctrineFit
from apps.doctrines.services import derive_skill_requirements
from apps.pilots.models import ContributionEvent, ContributionWeights
from apps.pilots.progression import award_progression

RIFTER_EFT = """[Rifter, T]
200mm AutoCannon I
Damage Control I
Fusion S x100
"""


def _doctrine_with_fit(priority=50):
    d = Doctrine.objects.create(name="Tackle", priority=priority)
    parsed = parse_eft(RIFTER_EFT)
    fit = DoctrineFit.objects.create(
        doctrine=d, name="Rifter", ship_type_id=parsed["ship_type_id"],
        modules=parsed["modules"], eft_text=RIFTER_EFT,
    )
    derive_skill_requirements(fit)
    return d, fit


def _snapshot(skills):
    return SimpleNamespace(skills=skills)


@pytest.mark.django_db
def test_first_import_is_baseline_no_credit(sde, character):
    _doctrine_with_fit()
    reqs = {str(r.skill_type_id): {"trained_level": r.min_level}
            for f in DoctrineFit.objects.all() for r in f.skill_requirements.all()}
    # prev=None → first import, establishes baseline, awards nothing retroactively.
    award_progression(character, None, _snapshot(reqs))
    assert not ContributionEvent.objects.exists()


@pytest.mark.django_db
def test_training_and_unlock_are_credited(sde, character):
    ContributionWeights.objects.create(name="t", is_active=True, train_points_per_level=2,
                                        doctrine_base=5, doctrine_priority_coef="0.1",
                                        doctrine_effort_per_mil_sp="1")
    _d, fit = _doctrine_with_fit(priority=50)
    reqs = list(fit.skill_requirements.all())
    full = {str(r.skill_type_id): {"trained_level": r.min_level} for r in reqs}

    # From "knows nothing" to "meets every requirement": every required skill is a
    # newly-trained recommended skill, and the doctrine becomes flyable.
    award_progression(character, {}, _snapshot(full))

    trains = ContributionEvent.objects.filter(kind="train", user=character.user)
    assert trains.count() == len(reqs)
    assert all(t.points == 2 for t in trains)

    unlock = ContributionEvent.objects.get(kind="doctrine", user=character.user)
    assert unlock.points >= 5            # base + priority + effort
    # The description carries ONLY what the kind cannot: the doctrine's name. The verb
    # ("Unlocked") is the kind, and the ledger renders that from the translated
    # ``get_kind_display`` — it is never frozen into the row.
    assert unlock.description == "Tackle"


@pytest.mark.django_db
def test_progression_is_idempotent(sde, character):
    ContributionWeights.objects.create(name="t", is_active=True)
    _d, fit = _doctrine_with_fit()
    full = {str(r.skill_type_id): {"trained_level": r.min_level}
            for r in fit.skill_requirements.all()}

    award_progression(character, {}, _snapshot(full))
    n_train = ContributionEvent.objects.filter(kind="train").count()
    n_doc = ContributionEvent.objects.filter(kind="doctrine").count()
    # Re-running the same delta must not double-credit.
    award_progression(character, {}, _snapshot(full))
    assert ContributionEvent.objects.filter(kind="train").count() == n_train
    assert ContributionEvent.objects.filter(kind="doctrine").count() == n_doc
    assert n_doc == 1
