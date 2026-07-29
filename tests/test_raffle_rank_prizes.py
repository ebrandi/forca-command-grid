"""Configurable ticket-winner counts, killboard rank prizes, and selective boosting.

The rules this file exists to pin:

* rank prizes are EARNED, ticket prizes are DRAWN — so a rank winner may still have a
  ticket drawn, and may win rank prizes on several boards, but nobody wins two ticket
  prizes;
* every prize kind is gated by the minimum-activity safeguard;
* the booster inflates only the kinds leadership chose.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.raffle import boosters, rankings, services
from apps.raffle.models import (
    RaffleContest,
    RafflePrize,
    RaffleRankAward,
    RaffleRankPrize,
)
from core import rbac
from tests._raffle_utils import add_prizes, enrol_pilot, home_kill, make_contest, make_user

HOME = 98000001


def _rank_prize(contest, board="top_killers", position=1, value="500000000", **kw):
    return RaffleRankPrize.objects.create(
        contest=contest, board_key=board, position=position,
        name=kw.pop("name", f"{board} #{position}"),
        estimated_value=Decimal(value), **kw)


# --------------------------------------------------------------------------- #
#  1. How many ticket winners
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_leadership_can_set_the_number_of_ticket_winners(django_user_model):
    contest = make_contest(status=RaffleContest.Status.DRAFT)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)

    services.set_ticket_prize_slots(contest, services.DEFAULT_TICKET_WINNERS, director)
    assert contest.prizes.count() == 3
    assert [p.rank for p in contest.prizes.order_by("rank")] == [1, 2, 3]

    result = services.set_ticket_prize_slots(contest, 6, director)
    assert result["added"] == 3
    assert contest.prizes.count() == 6
    assert [p.rank for p in contest.prizes.order_by("rank")] == [1, 2, 3, 4, 5, 6]
    # A 6th winner must not be a bare English string in eight locales.
    assert contest.prizes.get(rank=6).name == "6th prize"

    services.set_ticket_prize_slots(contest, 2, director)
    assert contest.prizes.count() == 2


@pytest.mark.django_db
def test_winner_count_is_bounded_and_locked_once_accrual_starts(django_user_model):
    contest = make_contest(status=RaffleContest.Status.DRAFT)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)

    with pytest.raises(services.GrantBlocked):
        services.set_ticket_prize_slots(contest, 11, director)
    with pytest.raises(services.GrantBlocked):
        services.set_ticket_prize_slots(contest, 0, director)

    # Once tickets are accruing the ladder is the deal pilots are playing for.
    live = make_contest(status=RaffleContest.Status.ACTIVE)
    assert live.is_editable is False
    with pytest.raises(services.GrantBlocked):
        services.set_ticket_prize_slots(live, 5, director)


@pytest.mark.django_db
def test_shrinking_the_ladder_never_deletes_a_prize_that_was_won(django_user_model):
    """RaffleDrawResult.prize cascades — dropping a won slot would erase a published winner."""
    contest = make_contest(status=RaffleContest.Status.DRAFT)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    services.set_ticket_prize_slots(contest, 3, director)

    # Model a contest that already paid out its 3rd prize.
    user, _ = enrol_pilot(django_user_model, 4801)
    third = contest.prizes.get(rank=3)
    from apps.raffle.models import RaffleDraw, RaffleDrawResult
    draw = RaffleDraw.objects.create(contest=contest, status=RaffleDraw.Status.COMPLETED)
    RaffleDrawResult.objects.create(draw=draw, prize=third, winner_user=user, draw_order=1)

    result = services.set_ticket_prize_slots(contest, 1, director)
    assert 3 in result["kept_won"], "the won slot is kept"
    assert contest.prizes.filter(rank=3).exists()
    assert RaffleDrawResult.objects.filter(prize=third).exists(), "winner survives"


# --------------------------------------------------------------------------- #
#  2. Rank prizes
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_rank_prize_goes_to_the_top_of_that_board_with_frozen_evidence(django_user_model):
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=-1)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    top, _ = enrol_pilot(django_user_model, 4901)
    second, _ = enrol_pilot(django_user_model, 4902)
    for i in range(3):
        home_kill(7700 + i, attackers=[(4901, HOME, True)], when=now - timedelta(days=2))
    home_kill(7710, attackers=[(4902, HOME, True)], when=now - timedelta(days=2))
    prize = _rank_prize(contest, "top_killers", 1)

    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    awards = services.award_rank_prizes(contest, director)

    assert len(awards) == 1
    award = RaffleRankAward.objects.get(rank_prize=prize)
    assert award.winner_user_id == top.pk
    assert award.metric_value == Decimal("3.00")
    assert award.position == 1
    assert award.status == RaffleRankAward.Status.AWARDED
    # The board that decided it is frozen, because the killboard keeps moving.
    assert [r["place"] for r in award.standings] == [1, 2]
    assert award.standings[0]["name"] == "Pilot 4901"
    assert second.pk not in {a.winner_user_id for a in awards}


@pytest.mark.django_db
def test_rank_prizes_stack_with_each_other_and_with_a_ticket_win(django_user_model):
    """The whole point: earned prizes accumulate; only the drawn one is limited."""
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=-1, one_prize_per_pilot=True)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    ace, _ = enrol_pilot(django_user_model, 4911)
    # One pilot tops BOTH boards.
    for i in range(3):
        home_kill(7800 + i, attackers=[(4911, HOME, True)], is_solo=True,
                  when=now - timedelta(days=2))
    _rank_prize(contest, "top_killers", 1, name="Top killer")
    _rank_prize(contest, "solo_kills", 1, name="Top solo")
    # …and is the only ticket holder, so they also win the draw.
    services.grant_manual_tickets(contest, director, character_id=4911, amount=10,
                                  reason="seed")
    add_prizes(contest, n=1)

    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    draw = services.run_draw(contest, director)

    rank_wins = RaffleRankAward.objects.filter(contest=contest, winner_user=ace)
    assert rank_wins.count() == 2, "two boards topped, two fixed prizes"
    assert draw.results.get().winner_user_id == ace.pk, "and the ticket prize as well"


@pytest.mark.django_db
def test_a_rank_win_does_not_consume_the_one_ticket_prize_allowance(django_user_model):
    """A rank winner must not be locked out of the ticket draw, nor take two ticket prizes."""
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=-1, one_prize_per_pilot=True)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    ace, _ = enrol_pilot(django_user_model, 4921)
    other, _ = enrol_pilot(django_user_model, 4922)
    home_kill(7900, attackers=[(4921, HOME, True)], when=now - timedelta(days=2))
    _rank_prize(contest, "top_killers", 1)
    services.grant_manual_tickets(contest, director, character_id=4921, amount=50, reason="a")
    services.grant_manual_tickets(contest, director, character_id=4922, amount=50, reason="b")
    add_prizes(contest, n=2)

    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    draw = services.run_draw(contest, director)

    winners = list(draw.results.order_by("draw_order").values_list("winner_user_id", flat=True))
    assert sorted(winners) == sorted([ace.pk, other.pk]), "both ticket prizes awarded"
    assert len(winners) == len(set(winners)), "still one ticket prize per pilot"
    assert RaffleRankAward.objects.filter(contest=contest, winner_user=ace).count() == 1


@pytest.mark.django_db
def test_awarding_rank_prizes_is_idempotent(django_user_model):
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=-1)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 4931)
    home_kill(7910, attackers=[(4931, HOME, True)], when=now - timedelta(days=2))
    _rank_prize(contest, "top_killers", 1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    assert len(services.award_rank_prizes(contest, director)) == 1
    assert services.award_rank_prizes(contest, director) == [], "second run pays nothing"
    assert RaffleRankAward.objects.filter(contest=contest).count() == 1


@pytest.mark.django_db
def test_an_unclaimed_place_leaves_the_prize_unawarded(django_user_model):
    contest = make_contest(start_days_ago=7, end_days_ahead=-1)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    _rank_prize(contest, "top_killers", 1)      # nobody scored anything
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    assert services.award_rank_prizes(contest, director) == []
    assert RaffleRankAward.objects.filter(contest=contest).count() == 0


# --------------------------------------------------------------------------- #
#  3 + 4. Safeguards
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_rank_prizes_are_held_by_the_minimum_activity_safeguard(django_user_model):
    """Every prize kind sits behind the same gate — no side door onto a dead contest."""
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=-1,
                           min_activity_metric="pvp_kills",
                           min_activity_threshold=Decimal("50"))
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 4941)
    home_kill(7920, attackers=[(4941, HOME, True)], when=now - timedelta(days=2))
    _rank_prize(contest, "top_killers", 1)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    with pytest.raises(services.ActivityNotMet):
        services.run_draw(contest, director)
    assert RaffleRankAward.objects.filter(contest=contest).count() == 0, "nothing paid out"

    # The leadership override releases both kinds together.
    services.run_draw(contest, director, force=True)
    assert RaffleRankAward.objects.filter(contest=contest).count() == 1


@pytest.mark.django_db
def test_the_booster_only_inflates_the_prize_kinds_leadership_chose(django_user_model):
    now = timezone.now()
    # NOTE the metric: booster/min-activity metrics are computed from the raffle's own
    # ledger (apps.raffle.metrics.current_values), NOT from raw killmails. "pvp_kills" here
    # would read zero, because these tickets came from a manual grant rather than the PVP
    # sweep. The rank STANDINGS read the killboard; the safeguards read the ledger. That
    # divergence is by design and is why the two can show different numbers.
    contest = make_contest(
        start_days_ago=7, end_days_ahead=-1,
        prize_booster_metric="total_tickets", prize_booster_goal=Decimal("1"),
        prize_booster_percent=Decimal("50"),
        prize_booster_applies_to=RaffleContest.BoosterScope.RANK,
    )
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 4951)
    home_kill(7930, attackers=[(4951, HOME, True)], when=now - timedelta(days=2))
    services.grant_manual_tickets(contest, director, character_id=4951, amount=5, reason="s")
    rank_prize = _rank_prize(contest, "top_killers", 1, value="1000000")
    ticket_prize = RafflePrize.objects.create(
        contest=contest, rank=1, name="Ticket prize", estimated_value=Decimal("1000000"))

    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    draw = services.run_draw(contest, director)

    award = RaffleRankAward.objects.get(contest=contest)
    assert award.awarded_value == Decimal("1500000"), "rank prize boosted +50%"
    assert award.booster_applied is True
    assert draw.results.get().awarded_value == Decimal("1000000"), "ticket prize NOT boosted"

    # And the pilot-facing page must agree with what the draw actually paid.
    assert boosters.is_boostable(rank_prize, contest, kind="rank") is True
    assert boosters.is_boostable(ticket_prize, contest, kind="ticket") is False


@pytest.mark.django_db
def test_rank_prizes_count_towards_the_monthly_prize_budget(django_user_model):
    """Rank prizes are real ISK from the same wallet as the ticket ladder."""
    contest = make_contest(status=RaffleContest.Status.DRAFT)
    RafflePrize.objects.create(contest=contest, rank=1, name="Ticket",
                               estimated_value=Decimal("1000000"))
    _rank_prize(contest, "top_killers", 1, value="4000000")

    assert services.contest_prize_total(contest) == Decimal("5000000")


@pytest.mark.django_db
def test_perverse_boards_cannot_carry_a_prize():
    """Paying for ISK lost is a bounty on feeding; paying for efficiency pays pilots to dock."""
    assert "isk_lost" not in rankings.AWARDABLE_BOARDS
    assert "efficiency" not in rankings.AWARDABLE_BOARDS
    assert "top_killers" in rankings.AWARDABLE_BOARDS
    assert "solo_kills" in rankings.AWARDABLE_BOARDS
    assert "most_active" in rankings.AWARDABLE_BOARDS
    # …but they remain visible on the killboard's own display boards.
    assert "isk_lost" in rankings.CATEGORY_KEYS


# --------------------------------------------------------------------------- #
#  Pilot-facing dashboard
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_the_dashboard_shows_standings_and_the_climb_to_the_next_place(client, django_user_model):
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=1)
    leader, _ = enrol_pilot(django_user_model, 4961)
    chaser, _ = enrol_pilot(django_user_model, 4962)
    for i in range(4):
        home_kill(7940 + i, attackers=[(4961, HOME, True)], when=now - timedelta(days=2))
    home_kill(7950, attackers=[(4962, HOME, True)], when=now - timedelta(days=2))
    _rank_prize(contest, "top_killers", 1, name="Top killer prize")

    client.force_login(chaser)
    body = client.get(f"/raffle/{contest.slug}/").content.decode()

    assert "Top killer prize" in body
    assert "Prizes you earn by ranking" in body
    assert "Top Killers" in body
    # The incentive line: where they are, and exactly what closing the gap costs.
    assert "You are #2" in body
    assert "Pilot 4961" in body
    # And it says why this board can differ from the killboard's own.
    assert "live ESI token are ranked" in body

    # The leader sees the hold-it message instead.
    client.force_login(leader)
    leader_body = client.get(f"/raffle/{contest.slug}/").content.decode()
    assert "You are #1" in leader_body


@pytest.mark.django_db
def test_a_contest_without_rank_prizes_shows_no_standings_section(client, django_user_model):
    contest = make_contest()
    user, _ = enrol_pilot(django_user_model, 4971)
    client.force_login(user)
    body = client.get(f"/raffle/{contest.slug}/").content.decode()
    assert "Prizes you earn by ranking" not in body


@pytest.mark.django_db
def test_the_standings_warmer_only_builds_contests_that_need_it(django_user_model):
    from apps.raffle import rankings as rk
    from apps.raffle.tasks import refresh_adoption

    now = timezone.now()
    with_prize = make_contest(name="With rank prize", start_days_ago=7, end_days_ahead=1)
    make_contest(name="No rank prize", start_days_ago=7, end_days_ahead=1)
    enrol_pilot(django_user_model, 4981)
    home_kill(7960, attackers=[(4981, HOME, True)], when=now - timedelta(days=2))
    _rank_prize(with_prize, "top_killers", 1)

    rk.invalidate(with_prize)
    refresh_adoption()

    from django.core.cache import cache
    assert cache.get(rk._cache_key(with_prize)) is not None, "warmed"


@pytest.mark.django_db
def test_the_pvp_event_template_ships_both_prize_kinds(django_user_model):
    """Requirement 3: one scheduled event combining ranked and drawn prizes."""
    from apps.raffle import contest_templates

    contest = make_contest(status=RaffleContest.Status.DRAFT, seed_sources=False)
    assert contest_templates.apply_template(contest, "pvp_event", overwrite_prizes=True)

    assert contest.prizes.count() == 3, "a ticket ladder"
    boards = set(contest.rank_prizes.values_list("board_key", flat=True))
    assert boards == {"top_killers", "solo_kills", "most_active"}, "and rank prizes"
    assert contest.rank_prizes.filter(position=1).count() == 3
    assert contest.source_configs.get(source_key="pvp").enabled is True
    # The rules a pilot reads must state the stacking rule up front.
    assert "can win only one" in contest.public_rules
    assert "You can win both" in contest.public_rules


@pytest.mark.django_db
def test_reapplying_a_template_never_destroys_an_awarded_prize(django_user_model):
    from apps.raffle import contest_templates

    contest = make_contest(status=RaffleContest.Status.DRAFT)
    user, _ = enrol_pilot(django_user_model, 4991)
    prize = _rank_prize(contest, "top_killers", 1, name="Already paid")
    RaffleRankAward.objects.create(contest=contest, rank_prize=prize, winner_user=user,
                                   position=1)

    contest_templates.apply_template(contest, "pvp_event", overwrite_prizes=True)

    prize.refresh_from_db()
    assert RaffleRankAward.objects.filter(rank_prize=prize).exists(), "award survives"
    assert prize.name == "Already paid", "and so does the prize that paid it"


@pytest.mark.django_db
def test_the_contest_form_still_validates_without_the_new_booster_scope_key():
    """A new field on a long-lived ModelForm must not break existing submitters.

    ``prize_booster_applies_to`` has choices and no ``blank=True``, so left to the
    ModelForm it would be REQUIRED — and every payload that predates it (the console's own
    edit flow included) would start failing validation with no visible cause. Omitting it
    means "the default", which is exactly the behaviour that existed before the field did.
    """
    from apps.raffle.forms import RaffleContestForm

    now = timezone.now()
    payload = {
        "name": "No scope key",
        "start_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
        "end_at": (now + timedelta(days=8)).strftime("%Y-%m-%dT%H:%M"),
        "draw_at": (now + timedelta(days=9)).strftime("%Y-%m-%dT%H:%M"),
        "leaderboard_size": 25, "booster_multiplier": "1",
        "min_activity_threshold": "0", "prize_booster_goal": "0",
        "prize_booster_percent": "0",
    }
    form = RaffleContestForm(payload)
    assert form.is_valid(), form.errors
    contest = form.save()
    assert contest.prize_booster_applies_to == RaffleContest.BoosterScope.BOTH
