"""Conversational answer pipeline (P7, doc 17 §3): grounding (bogus citations dropped),
graceful degradation when the LLM is off, self-only IDOR on the turn poll, and gating."""
from __future__ import annotations

import pytest
from django.core.cache import cache

from apps.command_intel import services
from apps.command_intel.ask import answer_question
from apps.command_intel.models import (
    Classification,
    ConversationTurn,
    IntelligenceReport,
)
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _user(django_user_model, role, suffix=""):
    user, _ = django_user_model.objects.get_or_create(username=f"ci-ask-{role}{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


class _FakeResult:
    def __init__(self, obj):
        self.obj = obj
        self.text = obj.get("answer", "")
        self.usage = {"input": 20, "output": 8}
        self.model = "MiniMax-M2.7"
        self.latency_ms = 1200
        self.finish_reason = "stop"


class _FakeClient:
    def __init__(self, obj):
        self._obj = obj

    def generate(self, req):
        return _FakeResult(self._obj)


def _report(summary="logistics depth is tightening"):
    return IntelligenceReport.objects.create(
        classification=Classification.HIGH_COMMAND, status=IntelligenceReport.Status.READY,
        title="Posture", summary=summary,
    )


@pytest.mark.django_db
def test_answer_grounds_and_drops_bogus_citations(django_user_model, settings, monkeypatch):
    settings.COMMAND_INTEL_ENABLED = True
    officer = _user(django_user_model, rbac.ROLE_OFFICER)
    report = _report()
    turn = ConversationTurn.objects.create(user=officer, question="logistics posture?")
    obj = {"answer": "Logistics is tightening.",
           "citations": [f"report:{report.pk}", "bogus:999"], "answerable": True}
    monkeypatch.setattr("apps.command_intel.llm.client.LLMClient", lambda *a, **k: _FakeClient(obj))

    answer_question(turn)
    turn.refresh_from_db()
    assert turn.status == ConversationTurn.Status.READY
    assert turn.grounded is True
    ids = [c["id"] for c in turn.citations]
    assert f"report:{report.pk}" in ids
    assert "bogus:999" not in ids          # a citation to a non-retrieved passage is dropped
    assert turn.clearance == "high_command"  # audit ceiling recorded


@pytest.mark.django_db
def test_ungrounded_answer_is_flagged(django_user_model, settings, monkeypatch):
    settings.COMMAND_INTEL_ENABLED = True
    officer = _user(django_user_model, rbac.ROLE_OFFICER, "-ung")
    _report()
    turn = ConversationTurn.objects.create(user=officer, question="logistics?")
    obj = {"answer": "Something unsourced.", "citations": [], "answerable": True}
    monkeypatch.setattr("apps.command_intel.llm.client.LLMClient", lambda *a, **k: _FakeClient(obj))

    answer_question(turn)
    turn.refresh_from_db()
    assert turn.status == ConversationTurn.Status.READY
    assert turn.grounded is False   # no valid citation ⇒ flagged for the officer to verify


@pytest.mark.django_db
def test_degraded_when_llm_disabled(django_user_model, settings):
    settings.COMMAND_INTEL_ENABLED = False
    officer = _user(django_user_model, rbac.ROLE_OFFICER, "-deg")
    _report()
    turn = ConversationTurn.objects.create(user=officer, question="logistics?")
    answer_question(turn)
    turn.refresh_from_db()
    assert turn.status == ConversationTurn.Status.READY_DEGRADED
    assert turn.grounded is False
    assert turn.answer  # a retrieval-only listing, never empty


@pytest.mark.django_db
def test_request_answer_creates_a_pending_turn(django_user_model):
    officer = _user(django_user_model, rbac.ROLE_OFFICER, "-req")
    turn = services.request_answer(user=officer, question="what is our posture?")
    assert turn.status == ConversationTurn.Status.PENDING
    assert turn.user == officer


@pytest.mark.django_db
def test_ask_rate_limit_trips_to_a_failed_turn(django_user_model):
    from apps.command_intel import config

    officer = _user(django_user_model, rbac.ROLE_OFFICER, "-rate")
    config.set("provider", {"rate_limit_per_hour": 2})
    t1 = services.request_answer(user=officer, question="q1")
    t2 = services.request_answer(user=officer, question="q2")
    t3 = services.request_answer(user=officer, question="q3")  # exceeds the 2/hour cap
    assert t1.status == ConversationTurn.Status.PENDING
    assert t2.status == ConversationTurn.Status.PENDING
    assert t3.status == ConversationTurn.Status.FAILED
    assert "Rate limit" in t3.error


@pytest.mark.django_db
def test_ask_status_is_self_only(client, django_user_model, sde):
    owner = _user(django_user_model, rbac.ROLE_OFFICER, "-own")
    intruder = _user(django_user_model, rbac.ROLE_OFFICER, "-int")
    turn = ConversationTurn.objects.create(
        user=owner, question="q", answer="a", status=ConversationTurn.Status.READY,
    )
    client.force_login(intruder)
    assert client.get(f"/command/ask/{turn.pk}/status/", HTTP_HX_REQUEST="true").status_code == 404
    client.force_login(owner)
    assert client.get(f"/command/ask/{turn.pk}/status/", HTTP_HX_REQUEST="true").status_code == 200


@pytest.mark.django_db
def test_ask_page_officer_only(client, django_user_model, sde):
    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-pg"))
    assert client.get("/command/ask/").status_code == 200
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, "-pg"))
    assert client.get("/command/ask/").status_code in (403, 302)


@pytest.mark.django_db
def test_ask_page_renders_a_ready_turn_with_citations(client, django_user_model, sde):
    officer = _user(django_user_model, rbac.ROLE_OFFICER, "-render")
    ConversationTurn.objects.create(
        user=officer, question="logistics posture?", answer="Logistics is tightening.",
        status=ConversationTurn.Status.READY, grounded=True,
        citations=[{"id": "report:1", "kind": "report", "title": "Posture", "ref_url": "/command/reports/1/"}],
    )
    client.force_login(officer)
    body = client.get("/command/ask/").content
    assert b"Logistics is tightening." in body
    assert b"report: Posture" in body       # citation chip rendered
    assert b"grounded" in body


@pytest.mark.django_db
def test_pending_turn_fragment_self_polls(client, django_user_model, sde):
    officer = _user(django_user_model, rbac.ROLE_OFFICER, "-poll")
    turn = ConversationTurn.objects.create(user=officer, question="q", status=ConversationTurn.Status.PENDING)
    client.force_login(officer)
    frag = client.get(f"/command/ask/{turn.pk}/status/", HTTP_HX_REQUEST="true").content
    assert b"hx-get" in frag                 # the poll is wired (CSP-clean, no JS)
    assert b"Consulting the intelligence archive" in frag
