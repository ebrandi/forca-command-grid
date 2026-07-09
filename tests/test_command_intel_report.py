"""Command Intelligence — the generation pipeline (doc 06).

Three lanes: the deterministic ``ready_degraded`` path (no key in the test settings),
the LLM path through a MOCKED provider returning a canned grounded body, and the
grounding gate that DROPS any course of action citing an unknown constraint (the G5
"zero ungrounded ids" guarantee). The grounding unit (schema.ground / drop_ungrounded)
is exercised directly too.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.command_intel import report as report_mod
from apps.command_intel.llm import schema
from apps.command_intel.llm.client import LLMClient, LLMResult
from apps.command_intel.models import IntelligenceReport, OperationalConstraint


def _grounded_body(constraint_key="fuel_runway"):
    """A structurally-valid, fully-grounded report body (cites only real keys)."""
    return {
        "executive_summary": "Fuel and SRP need attention this cycle.",
        "operational_picture": {
            "posture_statement": "Defensive posture; logistics under strain.",
            "highlights": ["Structure fuel is running low."],
            "not_assessed": [],
            "overall_readiness": None,
        },
        "operational_constraints": [
            {"constraint_key": constraint_key, "interpretation": "Fuel is the soonest risk.",
             "priority_rank": 1},
        ],
        "courses_of_action": [
            {
                "constraint_key": constraint_key,
                "objective": "Refuel the low-fuel structures",
                "reasoning": "Fuel runway sits below the watch margin.",
                "risk_if_ignored": "A structure could go offline.",
                "severity_if_ignored": "high",
                "effort": "medium",
                "priority": 90,
                "depends_on": [],
                "entity_refs": [],
            },
        ],
        "strategic_risks": [
            {"risk": "Structure fuel exhaustion", "severity": "high",
             "linked_constraint": constraint_key},
        ],
        "forecast": "Stable barring further fuel draw.",
        "annexes": [],
    }


def _ungrounded_body():
    """Structurally valid, but its single COA cites a constraint that does not exist."""
    body = _grounded_body()
    body["courses_of_action"][0]["constraint_key"] = "fleet_size.__not_a_real_doctrine__"
    return body


def _fake_init(self, provider=None):
    # Bypass the real LLMClient.__init__ (no key needed for the mocked generate()).
    self.adapter = None


@pytest.mark.django_db
def test_degraded_report_when_llm_disabled(django_user_model):
    # The test settings carry no LLM_API_KEY, so COMMAND_INTEL_ENABLED is False and
    # the pipeline produces a deterministic ready_degraded report.
    report = IntelligenceReport.objects.create(status=IntelligenceReport.Status.QUEUED)
    report_mod.run_generation(report)
    report.refresh_from_db()

    assert report.status == IntelligenceReport.Status.READY_DEGRADED
    assert report.snapshot_id is not None
    assert OperationalConstraint.objects.filter(snapshot=report.snapshot).exists()
    assert report.body.get("_degraded") is True
    for section in ("executive_summary", "operational_picture", "operational_constraints",
                    "courses_of_action", "strategic_risks", "forecast", "annexes"):
        assert section in report.body


@pytest.mark.django_db
@override_settings(COMMAND_INTEL_ENABLED=True, LLM_PROVIDER="minimax")
def test_llm_path_persists_grounded_report(django_user_model):
    report = IntelligenceReport.objects.create(status=IntelligenceReport.Status.QUEUED)
    canned = LLMResult(
        obj=_grounded_body(), text="...", usage={"input": 10, "output": 20},
        model="MiniMax-M2.7", latency_ms=100, finish_reason="stop",
    )
    with patch.object(LLMClient, "__init__", _fake_init), \
         patch.object(LLMClient, "generate", return_value=canned):
        report_mod.run_generation(report)
    report.refresh_from_db()

    assert report.status == IntelligenceReport.Status.READY
    assert report.grounding_violations_dropped == 0
    assert report.model_name == "MiniMax-M2.7"
    assert report.courses_of_action.count() >= 1                 # COAs persisted
    assert report.body["executive_summary"] == _grounded_body()["executive_summary"]


@pytest.mark.django_db
@override_settings(COMMAND_INTEL_ENABLED=True, LLM_PROVIDER="minimax")
def test_ungrounded_coa_is_dropped_by_grounding(django_user_model):
    report = IntelligenceReport.objects.create(status=IntelligenceReport.Status.QUEUED)
    canned = LLMResult(
        obj=_ungrounded_body(), text="...", usage={"input": 10, "output": 20},
        model="MiniMax-M2.7", latency_ms=100, finish_reason="stop",
    )
    with patch.object(LLMClient, "__init__", _fake_init), \
         patch.object(LLMClient, "generate", return_value=canned):
        report_mod.run_generation(report)
    report.refresh_from_db()

    assert report.status == IntelligenceReport.Status.READY
    assert report.grounding_violations_dropped >= 1              # the gate fired
    assert report.body["courses_of_action"] == []               # the ungrounded COA is gone
    assert report.courses_of_action.count() == 0                 # so nothing persisted from it


def test_grounding_flags_and_drops_an_ungrounded_citation():
    snapshot = {"sources": {"doctrine": {"doctrines": [{"name": "Ferox Fleet", "slug": "ferox"}]}}}
    constraints = [{"key": "fleet_size.ferox"}]
    index = schema.build_index(snapshot, constraints)

    obj = {
        "operational_constraints": [],
        "courses_of_action": [
            {"constraint_key": "fleet_size.__ghost__", "objective": "x", "entity_refs": []},
        ],
        "strategic_risks": [],
    }
    violations = schema.ground(obj, index)
    assert any("fleet_size.__ghost__" in v for v in violations)

    dropped = schema.drop_ungrounded(obj, index)
    assert dropped == 1
    assert obj["courses_of_action"] == []


def test_grounded_citation_passes_validation():
    snapshot = {"sources": {"doctrine": {"doctrines": [{"name": "Ferox Fleet", "slug": "ferox"}]}}}
    constraints = [{"key": "fleet_size.ferox"}]
    index = schema.build_index(snapshot, constraints)

    obj = {
        "operational_constraints": [{"constraint_key": "fleet_size.ferox"}],
        "courses_of_action": [],
        "strategic_risks": [],
    }
    assert schema.ground(obj, index) == []
    assert schema.drop_ungrounded(obj, index) == 0
