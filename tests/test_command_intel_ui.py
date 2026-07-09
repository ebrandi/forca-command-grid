"""Command Intelligence leadership UI — gating + classification-filter coverage.

The web surface is officer-gated (`role_required`), and the Reports surfaces add a
per-row classification gate on top: a viewer below a report's clearance can neither
enumerate it (`visible_reports`) nor open it (`report_detail` → 403). These tests pin
both gates through the real URLs.
"""
from __future__ import annotations

import pytest

from apps.command_intel.access import visible_reports
from apps.command_intel.models import (
    Classification,
    CourseOfAction,
    IntelligenceReport,
)
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, role: str, suffix: str = ""):
    user, _ = django_user_model.objects.get_or_create(username=f"ci-{role}{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


@pytest.mark.django_db
def test_officer_reaches_the_command_area(client, django_user_model, sde):
    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER))
    for url in ("/command/", "/command/constraints/", "/command/reports/"):
        assert client.get(url).status_code == 200, url


@pytest.mark.django_db
def test_plain_member_is_blocked_from_the_command_area(client, django_user_model, sde):
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER))
    # role_required raises PermissionDenied (403); a redirect to login is also acceptable.
    assert client.get("/command/").status_code in (403, 302)


@pytest.mark.django_db
def test_member_cannot_see_a_director_eyes_only_report(client, django_user_model, sde):
    member = _user(django_user_model, rbac.ROLE_MEMBER)
    report = IntelligenceReport.objects.create(
        classification=Classification.DIRECTOR_EYES_ONLY,
        status=IntelligenceReport.Status.READY,
        title="EYES ONLY ALPHA",
    )
    # List gate: the queryset excludes it entirely (cannot even enumerate it).
    assert report not in list(visible_reports(member))
    # Detail gate: the member is bounced (403 from the role gate or the clearance gate).
    client.force_login(member)
    assert client.get(f"/command/reports/{report.pk}/").status_code in (403, 302)


@pytest.mark.django_db
def test_classification_gate_blocks_a_cleared_officer_too(client, django_user_model, sde):
    # An officer passes the *area* gate but not the director-only *clearance* floor,
    # so the classification gate is what does the work here (a 403, not a redirect).
    officer = _user(django_user_model, rbac.ROLE_OFFICER, suffix="-clr")
    report = IntelligenceReport.objects.create(
        classification=Classification.DIRECTOR_EYES_ONLY,
        status=IntelligenceReport.Status.READY,
        title="EYES ONLY BRAVO",
    )
    assert report not in list(visible_reports(officer))
    client.force_login(officer)
    assert client.get(f"/command/reports/{report.pk}/").status_code == 403
    # The list surface must not leak the title of a report above clearance.
    assert b"EYES ONLY BRAVO" not in client.get("/command/reports/").content


@pytest.mark.django_db
def test_ready_briefing_renders_with_its_coa_cards(client, django_user_model, sde):
    # Exercises _briefing / _coa_card / _classification_banner end-to-end.
    officer = _user(django_user_model, rbac.ROLE_OFFICER, suffix="-brief")
    report = IntelligenceReport.objects.create(
        classification=Classification.HIGH_COMMAND,
        status=IntelligenceReport.Status.READY,
        title="Posture briefing",
        summary="We can field the doctrine but logistics is tightening.",
        body={
            "executive_summary": "Posture 72 (WATCH). Two binding constraints.",
            "operational_picture": {
                "posture_statement": "Deterministic constraint picture.",
                "overall_readiness": 72,
                "highlights": ["Max Ferox fleet: 18 pilots"],
                "not_assessed": ["Build capacity — corp_industry scope not granted"],
            },
            "operational_constraints": [
                {"constraint_key": "fleet_size.ferox", "interpretation": "Capped at 18.", "priority_rank": 1},
            ],
            "strategic_risks": [{"risk": "Cash runway < 60d", "severity": "high", "linked_constraint": ""}],
            "forecast": "Posture projected 72 -> 66 in 14d.",
            "annexes": [{"title": "Constraint evidence", "ref": "constraints"}],
        },
        # Real adapter token shape (input/output/…). Regression: a POPULATED token_usage
        # must render — the briefing footer previously looked up non-existent keys
        # (prompt_tokens/input_tokens) and 500'd, but only when token_usage was non-empty,
        # so an empty-{} test report hid the bug.
        token_usage={"input": 31128, "output": 3504, "total": 34632, "reasoning": 741, "cached": 0},
    )
    CourseOfAction.objects.create(
        report=report, slug="fleet_size.ferox/stage-hulls",
        objective="Stage 12 Ferox hulls to the home keepstar this week.",
        reasoning="22 qualify, only 18 staged.",
        severity_if_ignored="high", priority=70, confidence=0.78,
        state=CourseOfAction.State.PROPOSED,
    )
    client.force_login(officer)
    resp = client.get(f"/command/reports/{report.pk}/")
    assert resp.status_code == 200
    body = resp.content
    assert b"HIGH COMMAND" in body              # classification banner (top + bottom)
    assert b"Stage 12 Ferox hulls" in body      # COA card objective
    assert b"Accept" in body                     # the Accept decision affordance
    assert b"tokens 31128/3504" in body          # regression: token_usage.input/output render (not prompt_tokens)


@pytest.mark.django_db
def test_in_flight_report_renders_the_polling_page(client, django_user_model, sde):
    # Exercises _generating: a non-terminal report shows the htmx self-poll shell.
    officer = _user(django_user_model, rbac.ROLE_OFFICER, suffix="-gen")
    report = IntelligenceReport.objects.create(
        classification=Classification.HIGH_COMMAND,
        status=IntelligenceReport.Status.CALLING_LLM,
        title="In flight",
    )
    client.force_login(officer)
    resp = client.get(f"/command/reports/{report.pk}/")
    assert resp.status_code == 200
    assert b'id="report-shell"' in resp.content
    assert b"hx-get" in resp.content              # htmx poll wired, CSP-clean (no JS)
    # The htmx status fragment returns the stepper while non-terminal.
    frag = client.get(f"/command/reports/{report.pk}/status/", HTTP_HX_REQUEST="true")
    assert frag.status_code == 200
    assert b'id="report-shell"' in frag.content
