"""Raffle admin console permissions: officer-gated day-to-day surfaces and the
Director-only guard on executing a draw.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.raffle import services
from apps.raffle.models import RaffleContest, RaffleDraw
from core import rbac
from tests._raffle_utils import add_prizes, enrol_pilot, make_contest, make_user


@pytest.mark.django_db
def test_member_denied_hub_and_grant(client, django_user_model, sde):
    contest = make_contest()
    client.force_login(make_user(django_user_model, "member", rbac.ROLE_MEMBER))
    assert client.get(reverse("admin_audit:raffle_hub")).status_code == 403
    assert client.get(reverse("admin_audit:raffle_grant", args=[contest.pk])).status_code == 403


@pytest.mark.django_db
def test_officer_can_open_hub_and_grant(client, django_user_model, sde):
    contest = make_contest()
    client.force_login(make_user(django_user_model, "officer", rbac.ROLE_OFFICER))
    assert client.get(reverse("admin_audit:raffle_hub")).status_code == 200
    assert client.get(reverse("admin_audit:raffle_grant", args=[contest.pk])).status_code == 200


@pytest.mark.django_db
def test_execute_draw_refused_for_non_director(client, django_user_model, sde):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7001)
    services.grant_manual_tickets(contest, director, character_id=7001, amount=10, reason="seed")
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    services.prepare_draw(contest, director)

    url = reverse("admin_audit:raffle_draw_action", args=[contest.pk])

    # An officer reaches the view (200-tier POST → redirect) but the draw is refused.
    officer = make_user(django_user_model, "officer", rbac.ROLE_OFFICER)
    client.force_login(officer)
    resp = client.post(url, {"action": "execute"})
    assert resp.status_code == 302
    assert not contest.draws.filter(status=RaffleDraw.Status.COMPLETED).exists()
    contest.refresh_from_db()
    assert contest.status == RaffleContest.Status.CLOSED

    # A Director executes it for real.
    client.force_login(director)
    resp = client.post(url, {"action": "execute"})
    assert resp.status_code == 302
    assert contest.draws.filter(status=RaffleDraw.Status.COMPLETED).exists()
    contest.refresh_from_db()
    assert contest.status == RaffleContest.Status.COMPLETED


@pytest.mark.django_db
def test_member_cannot_post_draw_action(client, django_user_model, sde):
    """A member is 403'd before any draw logic runs."""
    contest = make_contest()
    client.force_login(make_user(django_user_model, "member", rbac.ROLE_MEMBER))
    resp = client.post(reverse("admin_audit:raffle_draw_action", args=[contest.pk]),
                       {"action": "execute"})
    assert resp.status_code == 403
