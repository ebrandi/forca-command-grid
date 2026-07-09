"""Activity safeguards (minimum-activity draw gate) + prize-value / ticket boosters."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.raffle import boosters, engine, metrics, services
from apps.raffle.forms import RaffleContestForm
from apps.raffle.models import RaffleContest, RafflePrize
from tests._raffle_utils import add_prizes, enrol_pilot, home_kill, make_contest

pytestmark = pytest.mark.django_db

HOME = 98000001


def _contest_with_kills(django_user_model, n_kills, **kw):
    contest = make_contest(status=RaffleContest.Status.ACTIVE, **kw)
    user, _ = enrol_pilot(django_user_model, 4001)
    for i in range(n_kills):
        home_kill(6000 + i, attackers=[(4001, HOME, True)], is_solo=True)
    engine.process_source(contest, "pvp")
    return contest, user


# --- metrics --------------------------------------------------------------- #
def test_pvp_kills_metric_counts_distinct_killmails(django_user_model):
    contest, _ = _contest_with_kills(django_user_model, 3)
    vals = metrics.current_values(contest, ["pvp_kills", "participants"])
    assert vals["pvp_kills"] == Decimal("3")
    assert vals["participants"] == Decimal("1")


def test_pvp_kills_counts_unique_killmails_not_pilot_participations(django_user_model):
    """A single kill with N corp attackers must count as ONE kill — not N — so the
    min-activity / booster conditions can't be mis-triggered by a crowded killmail."""
    contest = make_contest(status=RaffleContest.Status.ACTIVE,
                           min_activity_metric="pvp_kills", min_activity_threshold=Decimal("2"),
                           prize_booster_metric="pvp_kills", prize_booster_goal=Decimal("2"),
                           prize_booster_percent=Decimal("10"))
    # 5 enrolled corp pilots all on ONE shared kill (first lands the final blow).
    for i in range(5):
        enrol_pilot(django_user_model, 5001 + i, username=f"crowd{i}")
    home_kill(7001, is_solo=False, value="1000000000", attackers=[
        (5001, HOME, True), (5002, HOME, False), (5003, HOME, False),
        (5004, HOME, False), (5005, HOME, False),
    ])
    engine.process_source(contest, "pvp")

    vals = metrics.current_values(contest, ["pvp_kills", "pvp_isk_destroyed", "participants"])
    assert vals["pvp_kills"] == Decimal("1")                 # ONE kill, not five
    assert vals["pvp_isk_destroyed"] == Decimal("1000000000")  # kill value counted ONCE, not ×5
    assert vals["participants"] == Decimal("5")              # (pilots is a per-pilot count — correct)

    # The 5 participations must NOT satisfy a 2-kill minimum or booster goal.
    assert boosters.min_activity_status(contest)["met"] is False
    assert boosters.prize_booster_status(contest)["achieved"] is False

    # A genuine SECOND kill makes it two unique kills → conditions now trigger.
    home_kill(7002, is_solo=True, attackers=[(5001, HOME, True)])
    engine.process_source(contest, "pvp")
    assert metrics.value_of(contest, "pvp_kills") == Decimal("2")
    assert boosters.min_activity_status(contest)["met"] is True
    assert boosters.prize_booster_status(contest)["achieved"] is True


# --- minimum-activity draw gate ------------------------------------------- #
def test_draw_held_below_minimum_and_force_overrides(django_user_model):
    contest, user = _contest_with_kills(
        django_user_model, 2, min_activity_metric="pvp_kills", min_activity_threshold=Decimal("5"))
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED)

    assert boosters.min_activity_status(contest)["met"] is False
    with pytest.raises(services.ActivityNotMet):
        services.run_draw(contest)                      # held
    draw = services.run_draw(contest, user, force=True)  # override
    assert draw.status == "completed"
    assert draw.results.count() == 1
    assert draw.forced_below_minimum is True
    assert draw.min_activity_met is False


def test_auto_draw_holds_below_minimum(django_user_model):
    contest, _ = _contest_with_kills(
        django_user_model, 2, min_activity_metric="pvp_kills", min_activity_threshold=Decimal("5"))
    add_prizes(contest, n=1)
    contest.draw_at = timezone.now() - timedelta(minutes=1)   # draw time already passed
    contest.save(update_fields=["draw_at"])
    services.set_status(contest, RaffleContest.Status.CLOSED)
    from apps.raffle.tasks import draw_due
    assert draw_due() == 0                              # nothing drawn — held
    assert not contest.draws.filter(status="completed").exists()


def test_draw_proceeds_when_minimum_met(django_user_model):
    contest, _ = _contest_with_kills(
        django_user_model, 5, min_activity_metric="pvp_kills", min_activity_threshold=Decimal("5"))
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED)
    draw = services.run_draw(contest)                   # met → draws without force
    assert draw is not None and draw.results.count() == 1
    assert draw.min_activity_met is True
    assert draw.forced_below_minimum is False


# --- prize-value booster --------------------------------------------------- #
def test_prize_value_booster_applies_to_isk_plex_only(django_user_model):
    contest, user = _contest_with_kills(
        django_user_model, 2, prize_booster_metric="pvp_kills",
        prize_booster_goal=Decimal("1"), prize_booster_percent=Decimal("10"))
    isk = RafflePrize.objects.create(contest=contest, rank=1, name="ISK", prize_type="isk",
                                     estimated_value=Decimal("1000000000"))
    plex = RafflePrize.objects.create(contest=contest, rank=2, name="PLEX", prize_type="plex",
                                      estimated_value=Decimal("500000000"))
    ship = RafflePrize.objects.create(contest=contest, rank=3, name="Ship", prize_type="doctrine_ship",
                                      estimated_value=Decimal("200000000"))
    st = boosters.prize_booster_status(contest)
    assert st["achieved"] is True
    assert boosters.effective_prize_value(isk, contest) == Decimal("1100000000")   # +10%
    assert boosters.effective_prize_value(plex, contest) == Decimal("550000000")   # +10%
    assert boosters.effective_prize_value(ship, contest) == Decimal("200000000")   # unchanged

    services.set_status(contest, RaffleContest.Status.CLOSED)
    draw = services.run_draw(contest)
    assert draw.prize_booster_applied is True
    assert draw.prize_booster_percent == Decimal("10.00")
    r_isk = draw.results.get(prize=isk)
    assert r_isk.awarded_value == Decimal("1100000000")  # frozen boosted value


def test_prize_booster_not_applied_below_goal(django_user_model):
    contest, _ = _contest_with_kills(
        django_user_model, 2, prize_booster_metric="pvp_kills",
        prize_booster_goal=Decimal("50"), prize_booster_percent=Decimal("10"))
    isk = RafflePrize.objects.create(contest=contest, rank=1, name="ISK", prize_type="isk",
                                     estimated_value=Decimal("1000000000"))
    assert boosters.prize_booster_status(contest)["achieved"] is False
    assert boosters.effective_prize_value(isk, contest) == Decimal("1000000000")   # base only


# --- form validation ------------------------------------------------------- #
def test_form_requires_positive_threshold_and_goal():
    from datetime import timedelta

    from django.utils import timezone
    now = timezone.now()

    def dt(d):
        return (now + timedelta(days=d)).strftime("%Y-%m-%dT%H:%M")

    base = {"name": "T", "start_at": dt(1), "end_at": dt(7), "draw_at": dt(8),
            "leaderboard_size": 25, "booster_multiplier": "1"}
    # metric set but zero threshold → error
    f = RaffleContestForm(data={**base, "min_activity_metric": "pvp_kills", "min_activity_threshold": "0"})
    assert not f.is_valid() and "min_activity_threshold" in f.errors
    # booster metric set but zero goal/percent → error
    f2 = RaffleContestForm(data={**base, "prize_booster_metric": "pvp_kills",
                                 "prize_booster_goal": "0", "prize_booster_percent": "0"})
    assert not f2.is_valid() and "prize_booster_goal" in f2.errors
    # valid config
    f3 = RaffleContestForm(data={**base, "min_activity_metric": "pvp_kills", "min_activity_threshold": "50",
                                 "prize_booster_metric": "pvp_kills", "prize_booster_goal": "100",
                                 "prize_booster_percent": "10"})
    assert f3.is_valid(), f3.errors
