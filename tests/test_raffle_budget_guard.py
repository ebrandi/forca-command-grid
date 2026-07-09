"""RAF-5 (roadmap 3.14) — cross-contest monthly prize-spend budget guard.

A leadership-set monthly ISK ceiling that warns near the threshold and holds new contests
from going live once the month is committed past it. Counts a contest's prizes in the month
it draws; draft/cancelled contests don't count.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.raffle import services
from apps.raffle.models import RaffleContest, RafflePrize
from tests._raffle_utils import make_contest

pytestmark = pytest.mark.django_db


def _contest(value, *, status=RaffleContest.Status.ACTIVE):
    c = make_contest(status=status, seed_sources=False)
    c.draw_at = timezone.now()  # pin to this month so the test is boundary-independent
    c.save(update_fields=["draw_at"])
    RafflePrize.objects.create(contest=c, rank=1, name="Prize", estimated_value=Decimal(value))
    return c


def _set_ceiling(amount, warn_pct=80):
    cfg = services.active_config()
    cfg.monthly_prize_budget = Decimal(amount)
    cfg.budget_warn_pct = warn_pct
    cfg.save(update_fields=["monthly_prize_budget", "budget_warn_pct", "updated_at"])
    return cfg


def test_monthly_spend_sums_committed_only():
    _contest(100)
    _contest(200)
    _contest(500, status=RaffleContest.Status.DRAFT)      # draft — not committed
    _contest(700, status=RaffleContest.Status.CANCELLED)  # cancelled — not committed
    assert services.monthly_prize_spend() == Decimal("300")


def test_budget_status_states():
    _set_ceiling(1000)
    _contest(500)
    assert services.budget_status()["state"] == "ok"    # 50%
    _contest(350)
    assert services.budget_status()["state"] == "warn"  # 85% >= 80
    _contest(300)
    s = services.budget_status()
    assert s["state"] == "over" and s["spent"] == Decimal("1150")


def test_ceiling_zero_is_off():
    _contest(9999)
    s = services.budget_status()
    assert s["enabled"] is False and s["state"] == "off"


def test_block_reason_when_activation_would_breach():
    _set_ceiling(1000)
    _contest(900)  # committed active
    breach = _contest(200, status=RaffleContest.Status.SCHEDULED)
    assert services.budget_block_reason(breach)  # 900 + 200 > 1000


def test_no_block_when_within_budget():
    _set_ceiling(1000)
    _contest(500)  # committed active
    within = _contest(200, status=RaffleContest.Status.SCHEDULED)
    assert services.budget_block_reason(within) == ""  # 500 + 200 <= 1000


def test_draft_activation_held_when_over_budget():
    _set_ceiling(1000)
    _contest(900)  # committed active
    contest = _contest(300, status=RaffleContest.Status.DRAFT)
    assert services.set_status(contest, RaffleContest.Status.ACTIVE) is False  # 900+300 > 1000
    contest.refresh_from_db()
    assert contest.status == RaffleContest.Status.DRAFT  # held


def test_draft_scheduling_held_when_over_budget():
    # The draft-exit is guarded, so a schedule-then-activate can't sneak past the ceiling.
    _set_ceiling(1000)
    _contest(900)
    contest = _contest(300, status=RaffleContest.Status.DRAFT)
    assert services.set_status(contest, RaffleContest.Status.SCHEDULED) is False
    contest.refresh_from_db()
    assert contest.status == RaffleContest.Status.DRAFT


def test_draft_activation_allowed_within_budget():
    _set_ceiling(1000)
    _contest(500)  # committed active
    contest = _contest(200, status=RaffleContest.Status.DRAFT)
    assert services.set_status(contest, RaffleContest.Status.ACTIVE) is True  # 500+200 <= 1000
    contest.refresh_from_db()
    assert contest.status == RaffleContest.Status.ACTIVE


def test_reactivation_of_committed_contest_not_blocked_over_budget():
    # A closed contest whose prizes are ALREADY committed re-opens without re-litigating its
    # own value, even when the month is over budget (its 600 was already counted).
    _set_ceiling(1000)
    _contest(600)  # committed active
    closed = _contest(600, status=RaffleContest.Status.CLOSED)  # month total 1200, over
    assert services.budget_status()["state"] == "over"
    assert services.set_status(closed, RaffleContest.Status.ACTIVE) is True  # re-open allowed
    closed.refresh_from_db()
    assert closed.status == RaffleContest.Status.ACTIVE
