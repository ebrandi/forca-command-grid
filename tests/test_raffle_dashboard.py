"""0.7 / RAF-1: raffle standing + pending win surfaced on the Command Center."""
from __future__ import annotations

import pytest

from apps.raffle.models import (
    RaffleContest,
    RaffleDraw,
    RaffleDrawResult,
    RaffleParticipantSummary,
)
from apps.raffle.services import dashboard_summary
from tests._raffle_utils import add_prizes, enrol_pilot, make_contest


@pytest.mark.django_db
def test_dashboard_summary_none_when_nothing(django_user_model):
    user, _ = enrol_pilot(django_user_model, 900001)
    assert dashboard_summary(user) is None


@pytest.mark.django_db
def test_dashboard_summary_active_contest_odds(django_user_model):
    user, _ = enrol_pilot(django_user_model, 900002)
    other, _ = enrol_pilot(django_user_model, 900003)
    contest = make_contest()
    RaffleParticipantSummary.objects.create(contest=contest, user=user, total_tickets=30, rank=1)
    RaffleParticipantSummary.objects.create(contest=contest, user=other, total_tickets=70, rank=2)

    summary = dashboard_summary(user)
    assert summary["pending_win"] is None
    active = summary["active"]
    assert active["name"] == contest.name
    assert active["my_tickets"] == 30
    assert active["total_tickets"] == 100
    assert active["odds_pct"] == 30.0  # 30 / 100
    assert active["my_rank"] == 1


@pytest.mark.django_db
def test_dashboard_summary_surfaces_pending_win(django_user_model):
    user, _ = enrol_pilot(django_user_model, 900004)
    contest = make_contest(status=RaffleContest.Status.COMPLETED)
    prize = add_prizes(contest, 1)[0]
    draw = RaffleDraw.objects.create(contest=contest)
    RaffleDrawResult.objects.create(
        draw=draw, prize=prize, winner_user=user,
        status=RaffleDrawResult.Status.WON,
        fulfil_status=RaffleDrawResult.FulfilStatus.PENDING,
    )
    summary = dashboard_summary(user)
    assert summary["pending_win"]["prize_name"] == prize.name
    assert summary["pending_win"]["contest_name"] == contest.name
    # The winning contest is COMPLETED, so there is no active-contest section.
    assert summary["active"] is None
