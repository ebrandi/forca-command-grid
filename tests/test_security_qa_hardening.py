"""Regression tests for the availability-hardening fixes from the 2026-07-15 security QA.

F1 — the anonymous route/jump planner fanned out to one synchronous ESI ``/route`` call per
leg using ``essential=True`` (which bypasses the ESI error-budget floor): a flood could starve
workers and burn the budget until ESI froze app-wide. Fix: user-facing route lookups are now
non-essential (shed under budget pressure) and the per-request leg fan-out is bounded.

F2 — ``colonies_sync`` fanned out one Celery task per PLANETS-scoped pilot with no cooldown,
so a member could POST at the nginx rate and flood the queue. Fix: a per-user cooldown gate.
"""
from __future__ import annotations

import time

import pytest

from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter, EveScopeGrant
from apps.sso.services import ensure_role
from core import rbac


def _member(django_user_model, name):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    return u


# --- F1: route-planner ESI amplification -------------------------------------
@pytest.mark.django_db
def test_route_plan_multi_bounds_leg_fanout():
    """A single planner request can't fan out to unbounded synchronous ESI /route calls.
    The cap is checked before any ESI call, so no network mock is needed."""
    from apps.logistics.routing import _MAX_ROUTE_LEGS, RouteUnavailable, route_plan_multi

    seq = list(range(30_000_000, 30_000_000 + _MAX_ROUTE_LEGS + 2))  # _MAX_ROUTE_LEGS + 1 legs
    with pytest.raises(RouteUnavailable):
        route_plan_multi(seq)


@pytest.mark.django_db
def test_route_lookup_is_shed_under_esi_budget_pressure():
    """User-facing route lookups are non-essential, so a low ESI error budget SHEDS them
    (degrades to 'no route') instead of firing the call. This is the guard that stops an
    anonymous route flood from burning the budget and freezing ESI app-wide; ``essential=True``
    would bypass ``can_call`` and attempt the network call regardless."""
    from django.core.cache import cache

    from apps.logistics.routing import RouteUnavailable, route_plan

    # Drive the ESI error budget below the floor (ERROR_BUDGET_FLOOR=10) with a future reset.
    cache.set("esi:error_limit_remain", 1, timeout=120)
    cache.set("esi:error_limit_reset_at", time.time() + 60, timeout=120)
    with pytest.raises(RouteUnavailable):
        # Uncached systems → would hit ESI if not shed; non-essential means shed → RouteUnavailable.
        route_plan(30000142, 30002187, "safer")


# --- F2: colonies_sync Celery fan-out cooldown -------------------------------
@pytest.mark.django_db
def test_colonies_sync_has_cooldown(client, django_user_model, monkeypatch):
    """A second colony-sync within the cooldown window must not re-enqueue tasks."""
    from apps.planetary import tasks as pi_tasks

    u = _member(django_user_model, "eve:9100")
    char = EveCharacter.objects.create(
        character_id=9100, user=u, name="Farmer", is_main=True, is_corp_member=True
    )
    EveScopeGrant.objects.create(character=char, scope="esi-planets.manage_planets.v1", active=True)

    calls: list[int] = []
    monkeypatch.setattr(pi_tasks.sync_character_colonies, "delay", lambda cid: calls.append(cid))

    client.force_login(u)
    r1 = client.post("/industry/pi/colonies/sync/")
    assert r1.status_code == 302 and calls == [9100]  # first fan-out enqueued
    r2 = client.post("/industry/pi/colonies/sync/")
    assert r2.status_code == 302 and calls == [9100]  # second throttled — no new task enqueued


# --- AVAIL-05: candidate_refresh cooldown ------------------------------------
@pytest.mark.django_db
def test_candidate_refresh_has_cooldown(client, django_user_model, monkeypatch):
    """A second evidence refresh for the same candidate within the window doesn't re-enqueue."""
    from apps.recruitment import tasks as rec_tasks
    from apps.recruitment.models import Candidate

    officer = django_user_model.objects.create(username="rec_officer")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    cand = Candidate.objects.create(character_id=555, name="Prospect", added_by=officer)

    calls: list[int] = []
    monkeypatch.setattr(rec_tasks.refresh_candidate_evidence, "delay", lambda pk: calls.append(pk))

    client.force_login(officer)
    assert client.post(f"/recruitment/{cand.pk}/refresh/").status_code == 302
    assert calls == [cand.pk]
    assert client.post(f"/recruitment/{cand.pk}/refresh/").status_code == 302
    assert calls == [cand.pk]  # throttled — no second enqueue


# --- AVAIL-06: parse_eft line-count bound ------------------------------------
@pytest.mark.django_db
def test_parse_eft_bounds_per_line_db_lookups(monkeypatch):
    """A pathological paste can't fan out into an unbounded number of SdeType lookups —
    parse_eft truncates to _MAX_LINES, so _resolve is called a bounded number of times."""
    from apps.doctrines import fitparser

    calls = {"n": 0}

    def _fake_resolve(name):
        calls["n"] += 1
        return None

    monkeypatch.setattr(fitparser, "_resolve", _fake_resolve)
    text = "[Rifter, spam]\n" + "\n".join(f"Mod{i}" for i in range(fitparser._MAX_LINES + 300))
    fitparser.parse_eft(text)
    # Header + at most (_MAX_LINES - 1) module lines are parsed, plus one _resolve for the ship
    # name — so total lookups are bounded regardless of how long the paste is.
    assert calls["n"] <= fitparser._MAX_LINES + 1
