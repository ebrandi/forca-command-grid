"""Operational Campaign planner (P4, design doc 08)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from apps.command_intel import campaign as C
from apps.command_intel.coa import accept_coa
from apps.command_intel.models import (
    Campaign,
    CampaignMilestone,
    CourseOfAction,
    IntelligenceSnapshot,
)
from apps.tasks.models import Task
from apps.tasks.services import set_status


def _snapshot(overall: int = 80) -> IntelligenceSnapshot:
    return IntelligenceSnapshot.objects.create(slices={"readiness": {"overall_index": overall}})


def _coa(slug: str, delta, *, priority: int = 50, confidence: float = 0.7) -> CourseOfAction:
    return CourseOfAction.objects.create(
        slug=slug, objective=f"do {slug}", readiness_delta=delta,
        confidence=confidence, priority=priority, state="proposed",
    )


@pytest.mark.django_db
def test_compose_creates_milestones_and_links_coas(django_user_model):
    snap = _snapshot(80)
    user = django_user_model.objects.create(username="c-u1")
    a, b = _coa("camp/a", 3), _coa("camp/b", 2)
    with patch("apps.command_intel.snapshot.latest_snapshot", return_value=snap):
        camp = C.compose_campaign(
            objective="Raise readiness", target_metric="readiness.overall",
            target_value=90, coa_ids=[a.pk, b.pk], user=user,
        )
    assert camp.status == Campaign.Status.DRAFT
    assert camp.milestones.count() == 2
    assert float(camp.baseline_value) == 80.0
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.campaign_id == camp.pk and b.campaign_id == camp.pk


@pytest.mark.django_db
def test_order_coas_dependencies_first():
    a, b = _coa("o/a", 1, priority=50), _coa("o/b", 1, priority=50)
    a.dependencies.add(b)  # a depends on b → b should be ordered first
    ordered = C.order_coas(CourseOfAction.objects.filter(pk__in=[a.pk, b.pk]))
    assert [c.pk for c in ordered] == [b.pk, a.pk]


@pytest.mark.django_db
def test_trajectory_accumulates_with_damping(django_user_model):
    snap = _snapshot(80)
    user = django_user_model.objects.create(username="c-u2")
    a, b = _coa("t/a", 3, priority=80), _coa("t/b", 2, priority=70)
    with patch("apps.command_intel.snapshot.latest_snapshot", return_value=snap):
        camp = C.compose_campaign(
            objective="o", target_metric="readiness.overall", coa_ids=[a.pk, b.pk], user=user,
        )
    # baseline 80, +3, then +2*0.7 (second hit on the same metric is damped) = 84.4
    values = [p["value"] for p in camp.expected_trajectory]
    assert values == [80.0, 83.0, 84.4]


@pytest.mark.django_db
def test_success_probability_in_unit_range(django_user_model):
    snap = _snapshot(80)
    user = django_user_model.objects.create(username="c-u3")
    a = _coa("s/a", 3)
    with patch("apps.command_intel.snapshot.latest_snapshot", return_value=snap):
        camp = C.compose_campaign(
            objective="o", target_metric="readiness.overall", target_value=90,
            coa_ids=[a.pk], user=user,
        )
    assert 0.0 <= camp.success_probability <= 1.0


@pytest.mark.django_db
def test_launch_activates_and_anchors_baseline(django_user_model):
    snap = _snapshot(82)
    user = django_user_model.objects.create(username="c-u4")
    a = _coa("l/a", 3)
    with patch("apps.command_intel.snapshot.latest_snapshot", return_value=snap):
        camp = C.compose_campaign(objective="o", coa_ids=[a.pk], user=user)
        C.launch_campaign(camp, user)
    camp.refresh_from_db()
    assert camp.status == Campaign.Status.ACTIVE
    assert camp.start_at is not None and camp.due_at is not None
    assert float(camp.baseline_value) == 82.0


@pytest.mark.django_db
def test_coa_completion_rolls_up_and_completes_campaign(django_user_model):
    snap = _snapshot(80)
    user = django_user_model.objects.create(username="c-u5")
    a = _coa("r/a", 3)
    accept_coa(a, user, baseline_snapshot=snap)  # in_progress + a task
    with patch("apps.command_intel.snapshot.latest_snapshot", return_value=snap):
        camp = C.compose_campaign(objective="o", coa_ids=[a.pk], user=user)
        C.launch_campaign(camp, user)
        set_status(a.linked_tasks().first(), user, Task.Status.DONE)  # → signal
    a.refresh_from_db()
    camp.refresh_from_db()
    assert a.state == CourseOfAction.State.COMPLETED
    assert camp.milestones.first().status == CampaignMilestone.Status.DONE
    assert camp.status == Campaign.Status.COMPLETED  # all milestones done
    assert camp.progress_pct == 100


@pytest.mark.django_db
def test_abandon(django_user_model):
    snap = _snapshot(80)
    user = django_user_model.objects.create(username="c-u6")
    a = _coa("ab/a", 3)
    with patch("apps.command_intel.snapshot.latest_snapshot", return_value=snap):
        camp = C.compose_campaign(objective="o", coa_ids=[a.pk], user=user)
    C.abandon_campaign(camp, user, note="deprioritised")
    camp.refresh_from_db()
    assert camp.status == Campaign.Status.ABANDONED
