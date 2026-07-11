"""Capsuleer Path Tasks integration + quest-queue adapter (ADR-0008, doc 05 §5.2, doc 08 §10).

The explicit make-corp-task action (owner-only, neutral title, soft link), the DONE roll-up signal
(step evidence, never milestone/goal completion, no double contribution credit), and
``career_quests`` (at most one row, no PathSuggestion rows, url_name/args for Stage 4).
"""
from __future__ import annotations

import pytest

from apps.capsuleer import briefing, services
from apps.capsuleer.models import (
    CareerActionStep,
    GoalStatus,
    MilestoneKind,
    StepStatus,
    Verification,
    Visibility,
)
from apps.tasks.models import Task

from ._capsuleer_utils import _character, _goal, _member, _milestone

pytestmark = pytest.mark.django_db


def _pilot(django_user_model, cid=9701):
    user = _member(django_user_model, str(cid))
    return user, _character(user, cid, "Task Pilot")


def _step(goal, **kw):
    fields = {"title": "Haul 5 units to staging", "status": StepStatus.OPEN}
    fields.update(kw)
    return CareerActionStep.objects.create(goal=goal, **fields)


# --- make corp task (ADR-0008) ----------------------------------------------
def test_make_corp_task_links_step_and_is_neutral(django_user_model):
    user, char = _pilot(django_user_model)
    goal = _goal(user, character=char, title="Secret ambition", motivation="don't tell anyone",
                 visibility=Visibility.PRIVATE)
    step = _step(goal)
    task = services.make_corp_task_from_step(goal, step, user)
    assert task.related_type == "capsuleer_goal"
    assert task.related_id == f"{goal.pk}:{step.pk}"
    step.refresh_from_db()
    assert step.task_id == task.pk
    # Neutral: the private goal's title/motivation never leak into the corp task.
    blob = f"{task.title} {task.description}"
    assert "Secret ambition" not in blob and "don't tell anyone" not in blob
    assert goal.activity_log.filter(verb="task_created").exists()


def test_make_corp_task_owner_only(django_user_model):
    from django.core.exceptions import ValidationError

    owner, char = _pilot(django_user_model, 9702)
    other = _member(django_user_model, "9703")
    goal = _goal(owner, character=char)
    step = _step(goal)
    with pytest.raises(ValidationError):
        services.make_corp_task_from_step(goal, step, other)


def test_make_corp_task_dedupes(django_user_model):
    user, char = _pilot(django_user_model, 9704)
    goal = _goal(user, character=char)
    step = _step(goal)
    t1 = services.make_corp_task_from_step(goal, step, user)
    t2 = services.make_corp_task_from_step(goal, step, user)
    assert t1.pk == t2.pk  # active_task_exists returns the existing task, never a duplicate


# --- DONE roll-up signal -----------------------------------------------------
def test_task_done_marks_step_and_adds_evidence(django_user_model):
    user, char = _pilot(django_user_model, 9705)
    goal = _goal(user, character=char)
    step = _step(goal)
    task = services.make_corp_task_from_step(goal, step, user)
    task.status = Task.Status.DONE
    task.save()
    step.refresh_from_db()
    assert step.status == StepStatus.DONE and step.completed_at is not None
    assert goal.activity_log.filter(verb="step.task_done").exists()


def test_task_cancel_leaves_step(django_user_model):
    user, char = _pilot(django_user_model, 9706)
    goal = _goal(user, character=char)
    step = _step(goal)
    task = services.make_corp_task_from_step(goal, step, user)
    task.status = Task.Status.CANCELLED
    task.save()
    step.refresh_from_db()
    assert step.status == StepStatus.OPEN  # cancel never completes the step
    assert not goal.activity_log.filter(verb="step.task_done").exists()


def test_task_done_resave_does_not_duplicate_evidence(django_user_model):
    user, char = _pilot(django_user_model, 9707)
    goal = _goal(user, character=char)
    step = _step(goal)
    task = services.make_corp_task_from_step(goal, step, user)
    task.status = Task.Status.DONE
    task.save()
    task.title = "edited title"
    task.save()  # a re-save of an already-done task is not a transition
    assert goal.activity_log.filter(verb="step.task_done").count() == 1


# --- career_quests adapter ---------------------------------------------------
def test_career_quests_empty_without_active_goal(django_user_model):
    user, char = _pilot(django_user_model, 9708)
    _goal(user, character=char, status=GoalStatus.CONSIDERING)
    assert briefing.career_quests(user) == []


def test_career_quests_returns_open_step(django_user_model):
    user, char = _pilot(django_user_model, 9709)
    goal = _goal(user, character=char, status=GoalStatus.ACTIVE)
    step = _step(goal, title="Read the logi primer")
    rows = briefing.career_quests(user)
    assert len(rows) == 1
    row = rows[0]
    assert row["engine"] == "capsuleer" and row["corp_order"] is False
    assert row["title"] == "Read the logi primer"
    assert row["form_url_name"] == "capsuleer:quest_action"
    assert row["id"] == f"s{step.pk}"  # step id is prefixed so it can't collide with a milestone pk
    assert row["rank"] >= 500


def test_career_quests_falls_back_to_pending_milestone(django_user_model):
    user, char = _pilot(django_user_model, 9710)
    goal = _goal(user, character=char, status=GoalStatus.ACTIVE)
    ms = _milestone(goal, title="Qualify for the doctrine", kind=MilestoneKind.MANUAL,
                    verification=Verification.SELF, required=True)
    rows = briefing.career_quests(user)
    assert len(rows) == 1 and rows[0]["id"] == f"m{ms.pk}"
    assert rows[0]["title"] == "Qualify for the doctrine"


def test_career_quests_only_one_row_for_primary_goal(django_user_model):
    user, char = _pilot(django_user_model, 9711)
    primary = _goal(user, character=char, status=GoalStatus.ACTIVE, priority="primary",
                    title="Primary")
    _step(primary, title="Primary step")
    secondary = _goal(user, character=char, status=GoalStatus.ACTIVE, priority="secondary",
                      title="Secondary")
    _step(secondary, title="Secondary step")
    rows = briefing.career_quests(user)
    assert len(rows) == 1 and rows[0]["title"] == "Primary step"
