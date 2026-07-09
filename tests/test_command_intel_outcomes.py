"""Outcome measurement + calibration loop (P3, design doc 10 §3/§4)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from apps.command_intel import outcomes
from apps.command_intel.coa import accept_coa
from apps.command_intel.models import (
    ActionOutcome,
    CourseOfAction,
    IntelligenceSnapshot,
    OperationalConstraint,
)
from apps.tasks.models import Task
from apps.tasks.services import set_status


def _snapshot(hulls: int) -> IntelligenceSnapshot:
    """A snapshot whose doctrine slice yields a computable fleet_size constraint."""
    return IntelligenceSnapshot.objects.create(slices={
        "doctrine": {"doctrines": [{
            "name": "Ferox", "slug": "ferox", "primary": True,
            "flyable": 22, "viable": 0, "not_ready": 0,
            "hulls_in_stock": hulls, "min_pilots": 22,
        }]},
    })


def _constraint(snapshot) -> OperationalConstraint:
    return OperationalConstraint.objects.create(
        snapshot=snapshot, key="fleet_size.ferox", category="combat", label="Max Ferox",
        binding_metric=18, unit="pilots", status="computed",
    )


@pytest.mark.django_db
def test_task_done_completes_coa_via_signal(django_user_model):
    base = _snapshot(18)
    coa = CourseOfAction.objects.create(
        slug="fleet_size.ferox/t1", objective="Stage hulls", constraint=_constraint(base),
        readiness_delta=4, state="proposed", priority=80,
    )
    user = django_user_model.objects.create(username="p3-u1")
    accept_coa(coa, user, baseline_snapshot=base)
    coa.refresh_from_db()
    assert coa.state == CourseOfAction.State.IN_PROGRESS
    assert coa.linked_tasks().count() == 1

    set_status(coa.linked_tasks().first(), user, Task.Status.DONE)
    coa.refresh_from_db()
    assert coa.state == CourseOfAction.State.COMPLETED  # the signal closed the loop


@pytest.mark.django_db
def test_task_cancelled_returns_coa_to_accepted(django_user_model):
    base = _snapshot(18)
    coa = CourseOfAction.objects.create(
        slug="fleet_size.ferox/t2", objective="Stage hulls", constraint=_constraint(base),
        readiness_delta=4, state="proposed",
    )
    user = django_user_model.objects.create(username="p3-u2")
    accept_coa(coa, user, baseline_snapshot=base)
    set_status(coa.linked_tasks().first(), user, Task.Status.CANCELLED)
    coa.refresh_from_db()
    assert coa.state == CourseOfAction.State.ACCEPTED  # re-actionable


@pytest.mark.django_db
def test_measure_outcome_writes_action_outcome():
    base, outcome = _snapshot(18), _snapshot(22)  # hulls 18 → 22 ⇒ binding 18 → 22
    coa = CourseOfAction.objects.create(
        slug="fleet_size.ferox/t3", objective="Stage hulls", constraint=_constraint(base),
        readiness_delta=4, baseline_snapshot=base, state="in_progress",
    )
    with patch("apps.command_intel.snapshot.build_snapshot", return_value=outcome):
        result = outcomes.measure_outcome(coa)
    assert result is not None
    assert float(result.measured_delta) == 4.0   # 22 − 18
    assert float(result.predicted_delta) == 4.0
    assert float(result.error) == 0.0            # prediction held exactly


@pytest.mark.django_db
def test_calibration_neutral_until_min_samples():
    cal = outcomes.calibration_for("fleet_size")
    assert cal == {"family": "fleet_size", "n": 0, "bias": 0.0, "spread": 0.0, "factor": 1.0}


@pytest.mark.django_db
def test_calibration_learns_bias_and_factor():
    base = _snapshot(18)
    coa = CourseOfAction.objects.create(
        slug="fleet_size.ferox/t4", objective="o", constraint=_constraint(base), state="completed",
    )
    for err in (-3, -1, -3, -1, -2):  # 5 samples, mean −2, some spread
        ActionOutcome.objects.create(
            coa=coa, metric_key="fleet_size.ferox", predicted_delta=4,
            measured_delta=4 + err, error=err,
        )
    cal = outcomes.calibration_for("fleet_size")
    assert cal["n"] == 5
    assert cal["bias"] == -2.0      # learns the systematic under-delivery
    assert cal["spread"] > 0
    assert 0.4 <= cal["factor"] < 1.0  # noisy history → lower-confidence estimates
