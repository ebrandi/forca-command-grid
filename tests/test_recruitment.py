"""Recruitment intelligence: public evidence (pure), access, consent, privacy."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.identity.models import RoleAssignment
from apps.recruitment.models import Candidate, CandidateConsent, CandidateEvidence
from apps.recruitment.services import build_public_evidence, request_consent
from apps.sso.services import ensure_role
from core import rbac

NOW = datetime(2026, 6, 22, tzinfo=UTC)


def test_build_public_evidence_age_and_corp_hop_flag():
    char = {"birthday": "2022-06-22T00:00:00Z"}
    # 6 distinct corps in the last year → flagged as worth asking about.
    history = [
        {"corporation_id": i, "start_date": "2026-01-01T00:00:00Z"} for i in range(6)
    ]
    rows = build_public_evidence(char, history, NOW)
    age = next(r for r in rows if "age" in r["claim"].lower())
    assert "4.0 years" in age["claim"] and age["confidence"] == "high"
    churn = next(r for r in rows if "corporation" in r["claim"].lower())
    assert churn["is_flag"] is True and churn["theme"] == "risk"


def test_build_public_evidence_stable_pilot_not_flagged():
    char = {"birthday": "2015-01-01T00:00:00Z"}
    history = [{"corporation_id": 1, "start_date": "2018-01-01T00:00:00Z"}]
    rows = build_public_evidence(char, history, NOW)
    assert all(not r["is_flag"] for r in rows)


def test_security_status_surfaced():
    char = {"birthday": "2015-01-01T00:00:00Z", "security_status": -3.4}
    rows = build_public_evidence(char, [], NOW)
    sec = next(r for r in rows if "security status" in r["claim"].lower())
    assert "-3.4" in sec["claim"] and sec["is_flag"] is False


def test_red_standing_corp_in_history_is_flagged():
    char = {"birthday": "2015-01-01T00:00:00Z"}
    history = [
        {"corporation_id": 1, "start_date": "2024-01-01T00:00:00Z"},
        {"corporation_id": 666, "start_date": "2023-01-01T00:00:00Z"},  # a corp we hold red
    ]
    rows = build_public_evidence(char, history, NOW, red_entities={666})
    hostile = next(r for r in rows if "red" in r["claim"].lower())
    assert hostile["is_flag"] is True and hostile["confidence"] == "high"
    # No red set → no hostile flag.
    assert not any("red" in r["claim"].lower() for r in build_public_evidence(char, history, NOW))


@pytest.mark.django_db
def test_desk_is_officer_only(client, django_user_model):
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.get("/recruitment/").status_code == 403


@pytest.mark.django_db
def test_consent_is_scoped_and_time_boxed(django_user_model):
    officer = django_user_model.objects.create(username="fc")
    candidate = Candidate.objects.create(character_id=123, name="Cand")
    consent = request_consent(candidate, officer, ["esi-skills.read_skills.v1"])
    assert consent.is_active is True
    assert consent.scopes == ["esi-skills.read_skills.v1"]
    assert consent.expires_at > consent.created_at
    assert consent.state  # unique CSRF-style state stored


@pytest.mark.django_db
def test_rejecting_candidate_purges_esi_data(client, django_user_model):
    officer = django_user_model.objects.create(username="fc")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    candidate = Candidate.objects.create(character_id=42, name="Cand")
    CandidateEvidence.objects.create(candidate=candidate, theme="roles", claim="can fly logi", source="esi")
    CandidateConsent.objects.create(
        candidate=candidate, scopes=[], state="s1",
        expires_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    client.force_login(officer)
    client.post(f"/recruitment/{candidate.pk}/update/", {"status": "rejected", "notes": "no"})
    assert not CandidateEvidence.objects.filter(candidate=candidate, source="esi").exists()
    assert CandidateConsent.objects.get(candidate=candidate).revoked_at is not None
