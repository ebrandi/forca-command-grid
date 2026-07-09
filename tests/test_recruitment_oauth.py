"""WS-6 candidate live-OAuth: the second EVE application.

The flow is the highest-trust-risk surface in the product, so these tests pin
its security invariants: state must match an active consent, PKCE binds to the
candidate's session, the authorised character MUST equal the candidate, the
token is never stored, and a consent is single-use + time-boxed.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from django.forms.models import model_to_dict
from django.test import override_settings
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.recruitment.models import Candidate, CandidateConsent, CandidateEvidence
from apps.recruitment.services import build_esi_evidence
from apps.sso.services import ensure_role
from core import rbac
from core.esi.oauth import TokenResponse

SSO = dict(
    RECRUITMENT_SSO_CLIENT_ID="recruit-client",
    RECRUITMENT_SSO_CLIENT_SECRET="recruit-secret",
    RECRUITMENT_SSO_CALLBACK_URL="https://grid.example.com/recruitment/oauth/callback/",
    RECRUITMENT_SSO_ENABLED=True,
)
SCOPES = ["publicData", "esi-skills.read_skills.v1", "esi-characters.read_corporation_roles.v1"]


def _candidate(character_id=99, name="Cand", status=Candidate.Status.PROSPECT):
    return Candidate.objects.create(character_id=character_id, name=name, status=status)


def _consent(candidate, state="st1", *, hours=24, scopes=None):
    return CandidateConsent.objects.create(
        candidate=candidate, scopes=scopes or SCOPES, state=state,
        expires_at=timezone.now() + timedelta(hours=hours),
    )


def _claims(character_id, scopes=None):
    return {"sub": f"CHARACTER:EVE:{character_id}", "scp": scopes or SCOPES[1:], "name": "Cand"}


# --------------------------------------------------------------------------- #
# build_esi_evidence (pure)                                                    #
# --------------------------------------------------------------------------- #

def test_evidence_director_role_is_flagged():
    rows = build_esi_evidence(
        {"total_sp": 45_200_000, "skills": [{"trained_skill_level": 5}, {"trained_skill_level": 3}]},
        {"roles": ["Director", "Accountant"]},
    )
    sp = next(r for r in rows if "skill points" in r["claim"])
    assert "45.2M" in sp["claim"] and sp["source"] == "esi" and sp["is_flag"] is False
    roles = next(r for r in rows if r["theme"] == "roles")
    assert roles["is_flag"] is True and "Director" in roles["claim"]


def test_evidence_line_member_not_flagged():
    rows = build_esi_evidence({"total_sp": 5_000_000, "skills": []}, {"roles": []})
    roles = next(r for r in rows if r["theme"] == "roles")
    assert roles["is_flag"] is False and "No special roles" in roles["claim"]


def test_evidence_omits_ungranted_scopes():
    # roles scope not granted → only skills-derived rows, no roles row.
    rows = build_esi_evidence({"total_sp": 1_000_000, "skills": [{"trained_skill_level": 5}]}, None)
    assert rows and all(r["theme"] != "roles" for r in rows)
    # nothing granted → no rows at all.
    assert build_esi_evidence(None, None) == []


# --------------------------------------------------------------------------- #
# begin interstitial                                                          #
# --------------------------------------------------------------------------- #

@override_settings(**SSO)
@pytest.mark.django_db
def test_begin_get_shows_consent_interstitial(client):
    c = _consent(_candidate())
    html = client.get(f"/recruitment/oauth/begin/{c.state}/").content.decode()
    assert "Authorise with EVE Online" in html and "Cand" in html


@override_settings(**SSO)
@pytest.mark.django_db
def test_begin_get_invalid_state_is_graceful(client):
    html = client.get("/recruitment/oauth/begin/nope/").content.decode()
    assert "invalid or already used" in html


@override_settings(**SSO)
@pytest.mark.django_db
def test_begin_post_redirects_to_eve_authorize_and_stashes_pkce(client):
    c = _consent(_candidate())
    resp = client.post(f"/recruitment/oauth/begin/{c.state}/")
    assert resp.status_code == 302
    loc = resp.url
    assert loc.startswith("https://login.eveonline.com/v2/oauth/authorize/")
    assert "client_id=recruit-client" in loc
    assert f"state={c.state}" in loc and "code_challenge=" in loc
    assert "esi-skills.read_skills.v1" in loc
    # PKCE verifier bound to this browser's session, keyed by state.
    assert client.session.get(f"recruit_pkce:{c.state}")


@pytest.mark.django_db
def test_begin_post_when_disabled_does_not_redirect(client):
    # default test settings have RECRUITMENT_SSO_ENABLED False.
    c = _consent(_candidate())
    resp = client.post(f"/recruitment/oauth/begin/{c.state}/")
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# callback                                                                    #
# --------------------------------------------------------------------------- #

def _prime_session(client, state, verifier="ver"):
    s = client.session
    s[f"recruit_pkce:{state}"] = verifier
    s.save()


@override_settings(**SSO)
@pytest.mark.django_db
@mock.patch("apps.recruitment.services.read_candidate_esi")
@mock.patch("apps.recruitment.oauth.validate_access_token")
@mock.patch("apps.recruitment.oauth.exchange_code")
def test_callback_happy_path_stores_evidence_marks_granted(exch, val, read, client):
    cand = _candidate(character_id=99)
    c = _consent(cand, state="ok1")
    _prime_session(client, "ok1")
    exch.return_value = TokenResponse("ACCESS-TOK", "REFRESH-TOK", 1200, "Bearer")
    val.return_value = _claims(99)
    read.return_value = ({"total_sp": 10_000_000, "skills": [{"trained_skill_level": 5}]},
                         {"roles": ["Accountant"]})

    resp = client.get("/recruitment/oauth/callback/?code=abc&state=ok1")
    assert resp.status_code == 200
    assert "All set" in resp.content.decode()

    cand.refresh_from_db()
    c.refresh_from_db()
    assert cand.status == Candidate.Status.LINKED
    assert c.granted_at is not None
    esi_rows = CandidateEvidence.objects.filter(candidate=cand, source="esi")
    assert esi_rows.exists()
    # ESI read was done with the real access token, once.
    read.assert_called_once()
    assert read.call_args.args[1] == "ACCESS-TOK"


@override_settings(**SSO)
@pytest.mark.django_db
@mock.patch("apps.recruitment.services.read_candidate_esi")
@mock.patch("apps.recruitment.oauth.validate_access_token")
@mock.patch("apps.recruitment.oauth.exchange_code")
def test_callback_never_stores_the_token(exch, val, read, client):
    cand = _candidate(character_id=99)
    c = _consent(cand, state="tok1")
    _prime_session(client, "tok1")
    exch.return_value = TokenResponse("ACCESS-TOK", "REFRESH-TOK", 1200, "Bearer")
    val.return_value = _claims(99)
    read.return_value = ({"total_sp": 1, "skills": []}, {"roles": []})

    client.get("/recruitment/oauth/callback/?code=abc&state=tok1")

    # The token must appear in NO recruitment record (consent, candidate, evidence).
    c.refresh_from_db()
    cand.refresh_from_db()
    blobs = [str(model_to_dict(c)), str(model_to_dict(cand))]
    blobs += [r.claim for r in CandidateEvidence.objects.filter(candidate=cand)]
    assert all("ACCESS-TOK" not in b and "REFRESH-TOK" not in b for b in blobs)


@override_settings(**SSO)
@pytest.mark.django_db
@mock.patch("apps.recruitment.services.read_candidate_esi")
@mock.patch("apps.recruitment.oauth.validate_access_token")
@mock.patch("apps.recruitment.oauth.exchange_code")
def test_callback_character_mismatch_rejected(exch, val, read, client):
    cand = _candidate(character_id=99)
    _consent(cand, state="mm1")
    _prime_session(client, "mm1")
    exch.return_value = TokenResponse("AT", "RT", 1200, "Bearer")
    val.return_value = _claims(12345)  # a DIFFERENT character authorised

    resp = client.get("/recruitment/oauth/callback/?code=abc&state=mm1")
    assert resp.status_code == 400
    read.assert_not_called()
    assert not CandidateEvidence.objects.filter(candidate=cand, source="esi").exists()
    assert CandidateConsent.objects.get(state="mm1").granted_at is None


@override_settings(**SSO)
@pytest.mark.django_db
@mock.patch("apps.recruitment.services.read_candidate_esi")
@mock.patch("apps.recruitment.oauth.validate_access_token")
@mock.patch("apps.recruitment.oauth.exchange_code")
def test_callback_esi_ratelimit_is_graceful_502_not_500(exch, val, read, client):
    from core.esi.client import ESIRateLimited

    cand = _candidate(character_id=99)
    _consent(cand, state="rl1")
    _prime_session(client, "rl1")
    exch.return_value = TokenResponse("AT", "RT", 1200, "Bearer")
    val.return_value = _claims(99)
    read.side_effect = ESIRateLimited("420 from ESI; error limit reached")

    resp = client.get("/recruitment/oauth/callback/?code=abc&state=rl1")
    assert resp.status_code == 502  # friendly retry page, not an unhandled 500
    assert "try again" in resp.content.decode().lower()
    # nothing committed: no evidence, consent stays active for a retry.
    assert not CandidateEvidence.objects.filter(candidate=cand, source="esi").exists()
    assert CandidateConsent.objects.get(state="rl1").granted_at is None


@pytest.mark.django_db
def test_callback_unknown_state_rejected(client):
    resp = client.get("/recruitment/oauth/callback/?code=abc&state=ghost")
    assert resp.status_code == 400


@override_settings(**SSO)
@pytest.mark.django_db
@mock.patch("apps.recruitment.oauth.exchange_code")
def test_callback_without_session_pkce_rejected(exch, client):
    cand = _candidate(character_id=99)
    _consent(cand, state="nopkce")
    # no _prime_session → verifier missing → must reject before any exchange.
    resp = client.get("/recruitment/oauth/callback/?code=abc&state=nopkce")
    assert resp.status_code == 400
    exch.assert_not_called()


@override_settings(**SSO)
@pytest.mark.django_db
@mock.patch("apps.recruitment.oauth.exchange_code")
def test_callback_consent_is_single_use(exch, client):
    cand = _candidate(character_id=99)
    c = _consent(cand, state="used1")
    c.granted_at = timezone.now()  # already granted
    c.save(update_fields=["granted_at"])
    _prime_session(client, "used1")
    resp = client.get("/recruitment/oauth/callback/?code=abc&state=used1")
    assert resp.status_code == 400
    exch.assert_not_called()


@override_settings(**SSO)
@pytest.mark.django_db
@mock.patch("apps.recruitment.oauth.exchange_code")
def test_callback_expired_consent_rejected(exch, client):
    cand = _candidate(character_id=99)
    CandidateConsent.objects.create(
        candidate=cand, scopes=SCOPES, state="exp1",
        expires_at=datetime(2020, 1, 1, tzinfo=UTC),  # long past
    )
    _prime_session(client, "exp1")
    resp = client.get("/recruitment/oauth/callback/?code=abc&state=exp1")
    assert resp.status_code == 400
    exch.assert_not_called()


# --------------------------------------------------------------------------- #
# officer surface                                                              #
# --------------------------------------------------------------------------- #

@override_settings(**SSO)
@pytest.mark.django_db
def test_request_consent_surfaces_shareable_link(client, django_user_model):
    officer = django_user_model.objects.create(username="fc")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    cand = _candidate()
    client.force_login(officer)
    client.post(f"/recruitment/{cand.pk}/consent/")
    consent = CandidateConsent.objects.filter(candidate=cand).latest("created_at")
    # the detail page renders the single-use begin link for the active consent.
    html = client.get(f"/recruitment/{cand.pk}/").content.decode()
    assert f"/recruitment/oauth/begin/{consent.state}/" in html


@pytest.mark.django_db
def test_many_roles_evidence_stores_without_truncation():
    """Regression: a candidate holding many corp roles (a Director) produces a long roles
    claim that overflowed the old CharField(300) and 500'd the callback. The claim column is
    now a TextField, so the full derived claim is stored intact."""
    from apps.recruitment.services import store_esi_evidence

    roles = {"roles": [
        "Director", "Personnel_Manager", "Accountant", "Auditor", "Config_Equipment",
        "Config_Starbase_Equipment", "Container_Take_1", "Container_Take_2", "Container_Take_3",
        "Diplomat", "Factory_Manager", "Fitting_Manager", "Hangar_Query_1", "Hangar_Query_2",
        "Hangar_Take_1", "Hangar_Take_2", "Hangar_Take_3", "Junior_Accountant", "Rent_Office",
        "Security_Officer", "Starbase_Defense_Operator", "Starbase_Fuel_Technician",
        "Station_Manager", "Trader", "Terrestrial_Combat_Officer",
    ]}
    rows = build_esi_evidence(None, roles)
    roles_claim = next(r["claim"] for r in rows if r["theme"] == "roles")
    assert len(roles_claim) > 300  # would have overflowed varchar(300)

    cand = Candidate.objects.create(character_id=90000123, name="Stromgren")
    assert store_esi_evidence(cand, rows) == len(rows)
    saved = CandidateEvidence.objects.get(candidate=cand, theme="roles")
    assert saved.claim == roles_claim  # full text preserved
    assert saved.is_flag is True       # Director flag surfaced
