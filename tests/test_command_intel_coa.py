"""Command Intelligence — Course-of-Action lifecycle (doc 07).

Accepting a COA converts it into a ``tasks.Task`` via the shared create_task factory
(soft-linked by related_type/related_id) and advances it to in_progress; re-accepting
is idempotent (the create_task dedupe), and dismissing records the decision note.
"""
from __future__ import annotations

import pytest

from apps.command_intel import coa as coa_mod
from apps.command_intel.models import CourseOfAction
from apps.tasks.models import Task


def _coa(**kwargs):
    defaults = {
        "slug": "fuel_runway/refuel-structures",
        "objective": "Refuel low-fuel structures",
        "reasoning": "Fuel runway below the watch margin.",
        "priority": 90,
        "provenance": {"task_type": "deliver"},
    }
    defaults.update(kwargs)
    return CourseOfAction.objects.create(**defaults)


@pytest.mark.django_db
def test_accept_coa_creates_linked_task_and_advances(django_user_model):
    user = django_user_model.objects.create(username="ci-officer")
    coa = _coa()

    coa_mod.accept_coa(coa, user)
    coa.refresh_from_db()

    assert coa.state == CourseOfAction.State.IN_PROGRESS
    task = Task.objects.get(related_type=CourseOfAction.RELATED_TYPE, related_id=str(coa.pk))
    assert task.title == "Refuel low-fuel structures"


@pytest.mark.django_db
def test_accept_coa_is_idempotent(django_user_model):
    user = django_user_model.objects.create(username="ci-officer")
    coa = _coa()

    coa_mod.accept_coa(coa, user)
    coa_mod.accept_coa(coa, user)        # second accept must NOT fork a duplicate task

    assert coa.linked_tasks().count() == 1


@pytest.mark.django_db
def test_dismiss_coa_records_note_and_state(django_user_model):
    user = django_user_model.objects.create(username="ci-officer")
    coa = _coa()

    coa_mod.dismiss_coa(coa, user, note="Not a priority this cycle")
    coa.refresh_from_db()

    assert coa.state == CourseOfAction.State.DISMISSED
    assert coa.decision_note == "Not a priority this cycle"
