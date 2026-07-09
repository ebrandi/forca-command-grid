"""Adding a recruitment candidate by pilot NAME (resolved to an id via ESI)."""
from __future__ import annotations

import pytest
import responses

from apps.identity.models import RoleAssignment
from apps.recruitment.models import Candidate
from apps.sso.services import ensure_role
from core import rbac
from core.esi.names import resolve_character_id

IDS_URL = "https://esi.evetech.net/universe/ids/"


@responses.activate
def test_resolve_character_id_prefers_exact_match():
    responses.add(
        responses.POST, IDS_URL,
        json={"characters": [
            {"id": 2, "name": "Stromgren Alt"},
            {"id": 1, "name": "Stromgren"},
        ]},
        status=200,
    )
    assert resolve_character_id("stromgren") == (1, "Stromgren")  # case-insensitive exact


@responses.activate
def test_resolve_character_id_none_when_no_match():
    responses.add(responses.POST, IDS_URL, json={}, status=200)
    assert resolve_character_id("Definitely Not A Pilot") is None


def _officer(django_user_model):
    user = django_user_model.objects.create(username="fc")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


@responses.activate
@pytest.mark.django_db
def test_add_by_name_resolves_id(client, django_user_model, monkeypatch):
    # Don't let the eager evidence task reach out to ESI in the test.
    monkeypatch.setattr(
        "apps.recruitment.tasks.refresh_candidate_evidence.delay", lambda *a, **k: None
    )
    responses.add(
        responses.POST, IDS_URL,
        json={"characters": [{"id": 344805695, "name": "Akusaa III"}]}, status=200,
    )
    client.force_login(_officer(django_user_model))

    resp = client.post("/recruitment/add/", {"name": "akusaa iii"})
    assert resp.status_code == 302
    candidate = Candidate.objects.get(character_id=344805695)
    assert candidate.name == "Akusaa III"  # canonical name from ESI, not the id


@responses.activate
@pytest.mark.django_db
def test_add_unknown_name_creates_nothing(client, django_user_model):
    responses.add(responses.POST, IDS_URL, json={}, status=200)
    client.force_login(_officer(django_user_model))
    resp = client.post("/recruitment/add/", {"name": "Nobody Here"})
    assert resp.status_code == 302
    assert Candidate.objects.count() == 0


@pytest.mark.django_db
def test_add_still_accepts_a_raw_id(client, django_user_model, monkeypatch):
    monkeypatch.setattr(
        "apps.recruitment.tasks.refresh_candidate_evidence.delay", lambda *a, **k: None
    )
    client.force_login(_officer(django_user_model))
    client.post("/recruitment/add/", {"name": "90000001"})
    assert Candidate.objects.filter(character_id=90000001).exists()
