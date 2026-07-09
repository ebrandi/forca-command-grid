"""Contest lifecycle: the guarded state machine, ledger freeze on close, and the
grant-after-close block.
"""
from __future__ import annotations

import pytest

from apps.raffle import services
from apps.raffle.models import RaffleContest
from core import rbac
from tests._raffle_utils import enrol_pilot, make_contest, make_user


@pytest.mark.django_db
def test_legal_transition_succeeds(django_user_model):
    contest = make_contest(status=RaffleContest.Status.DRAFT)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    assert services.set_status(contest, RaffleContest.Status.ACTIVE, director) is True
    contest.refresh_from_db()
    assert contest.status == RaffleContest.Status.ACTIVE


@pytest.mark.django_db
def test_illegal_transition_returns_false(django_user_model):
    """A draft cannot jump straight to completed."""
    contest = make_contest(status=RaffleContest.Status.DRAFT)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    assert services.can_transition(contest, RaffleContest.Status.COMPLETED) is False
    assert services.set_status(contest, RaffleContest.Status.COMPLETED, director) is False
    contest.refresh_from_db()
    assert contest.status == RaffleContest.Status.DRAFT


@pytest.mark.django_db
def test_close_freezes_the_ledger(django_user_model):
    contest = make_contest(status=RaffleContest.Status.ACTIVE)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    assert contest.is_frozen is False
    assert services.set_status(contest, RaffleContest.Status.CLOSED, director) is True
    contest.refresh_from_db()
    assert contest.is_frozen is True


@pytest.mark.django_db
def test_grant_after_close_is_blocked(django_user_model):
    contest = make_contest(status=RaffleContest.Status.ACTIVE)
    user, _ = enrol_pilot(django_user_model, 4001)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)

    # A grant is fine while active…
    services.grant_manual_tickets(contest, director, character_id=4001, amount=2, reason="ok")
    # …but refused once the ledger freezes.
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    contest.refresh_from_db()
    with pytest.raises(services.GrantBlocked):
        services.grant_manual_tickets(contest, director, character_id=4001, amount=2, reason="late")


@pytest.mark.django_db
def test_cancelled_is_terminal(django_user_model):
    contest = make_contest(status=RaffleContest.Status.ACTIVE)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    assert services.set_status(contest, RaffleContest.Status.CANCELLED, director) is True
    contest.refresh_from_db()
    # Nothing is reachable from cancelled.
    assert services.can_transition(contest, RaffleContest.Status.ACTIVE) is False
    assert services.set_status(contest, RaffleContest.Status.ACTIVE, director) is False
