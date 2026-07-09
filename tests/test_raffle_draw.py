"""Winner draw: commit-reveal fairness, prize assignment, one-prize-per-pilot,
draw-time eligibility exclusion, empty pools, multi-win, and reproducibility.
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.raffle import draw as draw_engine
from apps.raffle import services
from apps.raffle.draw import verify_draw
from apps.raffle.models import RaffleContest, RaffleDraw
from core import rbac
from tests._raffle_utils import add_prizes, enrol_pilot, make_contest, make_user


def _grant(contest, actor, character_id, amount):
    services.grant_manual_tickets(contest, actor, character_id=character_id,
                                  amount=amount, reason="seed tickets")


def _winners(draw):
    return list(
        draw.results.order_by("draw_order").values_list("winner_user_id", flat=True)
    )


@pytest.mark.django_db
def test_run_draw_completes_and_assigns_distinct_winners(django_user_model):
    contest = make_contest(one_prize_per_pilot=True)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    ua, _ = enrol_pilot(django_user_model, 6001)
    ub, _ = enrol_pilot(django_user_model, 6002)
    _grant(contest, director, 6001, 100)
    _grant(contest, director, 6002, 50)
    add_prizes(contest, n=2)

    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    draw = services.run_draw(contest, director)

    assert draw is not None
    assert draw.status == RaffleDraw.Status.COMPLETED
    assert draw.results.count() == 2
    winners = _winners(draw)
    assert set(winners) == {ua.id, ub.id}          # both distinct pilots won
    assert len(winners) == len(set(winners))       # one prize per pilot

    report = verify_draw(draw)
    assert report["commitment_ok"] is True
    assert report["values_ok"] is True
    assert report["winners_match"] is True


@pytest.mark.django_db
def test_pilot_ineligible_at_draw_is_excluded_from_pool(django_user_model):
    contest = make_contest(one_prize_per_pilot=True)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    ua, _ = enrol_pilot(django_user_model, 6011)
    ub, char_b = enrol_pilot(django_user_model, 6012)
    _grant(contest, director, 6011, 100)
    _grant(contest, director, 6012, 40)
    add_prizes(contest, n=2)

    # B revokes their token AFTER earning — excluded from the pool at draw time.
    token = char_b.tokens.first()
    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])

    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    draw = services.run_draw(contest, director)

    assert draw.total_excluded_tickets == 40
    assert draw.total_eligible_tickets == 100
    winners = _winners(draw)
    assert ub.id not in winners
    assert winners == [ua.id]  # only A can win

    snap_b = draw.eligibility_snapshots.get(user_id=ub.id)
    assert snap_b.eligible is False
    assert snap_b.tickets_excluded == 40


@pytest.mark.django_db
def test_empty_pool_draws_zero_winners(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    add_prizes(contest, n=2)  # prizes but nobody has tickets

    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    draw = services.run_draw(contest, director)

    assert draw.status == RaffleDraw.Status.COMPLETED
    assert draw.results.count() == 0
    assert draw.total_eligible_tickets == 0
    report = verify_draw(draw)
    assert report["commitment_ok"] is True
    assert report["winners_match"] is True


@pytest.mark.django_db
def test_multi_win_config_can_repeat_a_winner(django_user_model):
    contest = make_contest(one_prize_per_pilot=False)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    ua, _ = enrol_pilot(django_user_model, 6021)
    _grant(contest, director, 6021, 100)
    add_prizes(contest, n=2)

    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    draw = services.run_draw(contest, director)

    winners = _winners(draw)
    assert winners == [ua.id, ua.id]  # same pilot took both prizes


@pytest.mark.django_db
def test_same_seed_is_reproducible(django_user_model):
    """Two draws over the same census from the SAME stored seed pick identically."""
    contest = make_contest(one_prize_per_pilot=True)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 6031)
    enrol_pilot(django_user_model, 6032)
    enrol_pilot(django_user_model, 6033)
    _grant(contest, director, 6031, 70)
    _grant(contest, director, 6032, 20)
    _grant(contest, director, 6033, 10)
    add_prizes(contest, n=2)

    first = draw_engine.execute_draw(draw_engine.prepare_draw(contest))
    first_winners = [(r.prize_id, r.winner_user_id)
                     for r in first.results.order_by("draw_order")]

    # A brand-new draw row seeded with the very same secret must reproduce it.
    replay = RaffleDraw.objects.create(
        contest=contest, status=RaffleDraw.Status.COMMITTED,
        algorithm_version=contest.algorithm_version,
        seed=first.seed, seed_commitment=first.seed_commitment,
    )
    replay = draw_engine.execute_draw(replay)
    replay_winners = [(r.prize_id, r.winner_user_id)
                      for r in replay.results.order_by("draw_order")]

    assert first_winners == replay_winners
    assert len(first_winners) == 2
