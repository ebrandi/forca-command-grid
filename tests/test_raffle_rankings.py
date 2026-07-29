"""Contest-window killboard standings: scope, eligibility, determinism and the climb gap."""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.raffle import rankings
from apps.sso.models import EveCharacter
from tests._raffle_utils import enrol_pilot, home_kill, make_contest

HOME = 98000001


@pytest.mark.django_db
def test_standings_scope_to_the_contest_window_and_resolve_accounts(django_user_model):
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=1)
    ua, _ = enrol_pilot(django_user_model, 4201)
    ub, _ = enrol_pilot(django_user_model, 4202)
    home_kill(7201, attackers=[(4201, HOME, True)], is_solo=True, when=now - timedelta(days=3))
    home_kill(7202, attackers=[(4201, HOME, True)], when=now - timedelta(days=2))
    home_kill(7203, attackers=[(4202, HOME, True)], when=now - timedelta(days=2))
    # Outside the accrual window entirely — must not count towards the contest.
    home_kill(7204, attackers=[(4202, HOME, True)], when=now - timedelta(days=40))

    rows = rankings.standings(contest, "top_killers", use_cache=False)
    assert [(r["place"], r["user_id"], r["value"]) for r in rows] == [
        (1, ua.pk, 2.0), (2, ub.pk, 1.0)
    ]
    assert rows[0]["name"] == "Pilot 4201"

    me = rankings.pilot_standing(contest, "top_killers", ub)
    assert me["place"] == 2
    assert me["gap"] == 1.0
    assert me["ahead_name"] == "Pilot 4201"
    assert me["is_leader"] is False

    assert rankings.winner_for(contest, "top_killers", 1)["user_id"] == ua.pk


@pytest.mark.django_db
def test_pilots_who_cannot_win_are_not_ranked_at_all(django_user_model):
    """The raffle only knows pilots who connected a token, so the board is the real race.

    A lapsed-token pilot leading the killboard simply is not in the raffle standings, and
    the pilot behind them is genuinely #1 — not "#2 who happens to get paid".
    """
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=1)
    lapsed, lapsed_char = enrol_pilot(django_user_model, 4211)
    ub, _ = enrol_pilot(django_user_model, 4212)
    # An unlinked corp character with the most kills of all — never enrolled.
    EveCharacter.objects.create(character_id=4213, user=None, name="Never Enrolled",
                                is_main=False, is_corp_member=True)

    for i in range(3):
        home_kill(7220 + i, attackers=[(4213, HOME, True)], when=now - timedelta(days=2))
    for i in range(2):
        home_kill(7230 + i, attackers=[(4211, HOME, True)], when=now - timedelta(days=2))
    home_kill(7240, attackers=[(4212, HOME, True)], when=now - timedelta(days=2))

    token = lapsed_char.tokens.first()
    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])

    rows = rankings.standings(contest, "top_killers", use_cache=False)
    assert {r["user_id"] for r in rows} == {ub.pk}, "only a pilot who can win is ranked"
    assert 4213 not in {r["character_id"] for r in rows}, "unenrolled pilot is not on the board"
    assert rows[0]["place"] == 1 and rows[0]["user_id"] == ub.pk

    assert rankings.winner_for(contest, "top_killers", 1)["user_id"] == ub.pk
    assert rankings.pilot_standing(contest, "top_killers", lapsed) is None


@pytest.mark.django_db
def test_alts_roll_up_so_one_person_holds_one_place(django_user_model):
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=1)
    user, _main = enrol_pilot(django_user_model, 4221)
    EveCharacter.objects.create(character_id=4222, user=user, name="Alt 4222",
                                is_main=False, is_corp_member=True)
    home_kill(7250, attackers=[(4221, HOME, True)], when=now - timedelta(days=2))
    home_kill(7251, attackers=[(4222, HOME, True)], when=now - timedelta(days=2))

    rows = rankings.standings(contest, "top_killers", use_cache=False)
    assert len(rows) == 1, f"one person, one place — got {rows}"
    assert rows[0]["value"] == 2.0, "both characters' kills count for the one person"


@pytest.mark.django_db
def test_tied_pilots_get_a_stable_deterministic_order(django_user_model):
    """A tie must not decide a prize by row ordering — it must be reproducible."""
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=1)
    for cid in (4303, 4301, 4302):          # created out of order on purpose
        enrol_pilot(django_user_model, cid)
    for i, cid in enumerate((4303, 4301, 4302)):
        home_kill(7300 + i, attackers=[(cid, HOME, True)], when=now - timedelta(days=2))

    seen = []
    rows = []
    for _ in range(4):
        rows = rankings.standings(contest, "top_killers", use_cache=False)
        assert [r["value"] for r in rows] == [1.0, 1.0, 1.0], "all three are tied"
        seen.append([r["character_id"] for r in rows])
    assert all(order == seen[0] for order in seen), f"tie order flapped: {seen}"
    assert seen[0] == [4301, 4302, 4303], "ties break on the lower character id"
    assert [r["place"] for r in rows] == [1, 2, 3]


@pytest.mark.django_db
def test_all_boards_are_built_in_one_pass(django_user_model):
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=1)
    enrol_pilot(django_user_model, 4231)
    home_kill(7260, attackers=[(4231, HOME, True)], is_solo=True, when=now - timedelta(days=2))

    boards = rankings.all_standings(contest, use_cache=False)
    assert set(boards) == set(rankings.CATEGORY_KEYS)
    assert boards["top_killers"]["rows"][0]["user_id"]
    assert boards["solo_kills"]["rows"][0]["value"] == 1.0
    # efficiency needs EFFICIENCY_MIN_FIGHTS (5) before it ranks anyone
    assert boards["efficiency"]["rows"] == []


@pytest.mark.django_db
def test_a_pilot_outside_the_displayed_board_still_sees_their_place(django_user_model):
    """Being 12th and seeing what 11th costs is the whole point of the board."""
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=1)
    # 12 pilots, descending kill counts, so the last two fall outside BOARD_LIMIT=10.
    users = {}
    km = 7400
    for i in range(12):
        cid = 4400 + i
        users[cid], _ = enrol_pilot(django_user_model, cid)
        for _ in range(12 - i):
            home_kill(km, attackers=[(cid, HOME, True)], when=now - timedelta(days=2))
            km += 1

    data = rankings.board(contest, "top_killers", use_cache=False)
    assert len(data["rows"]) == rankings.BOARD_LIMIT, "the page shows ten"
    assert len(data["ranked"]) == 12, "but everyone is placed"
    assert data["as_of"], "a board that can be stale must say when it was built"

    twelfth = rankings.pilot_standing(contest, "top_killers", users[4411])
    assert twelfth["place"] == 12
    assert twelfth["on_display_board"] is False
    assert twelfth["ahead_place"] == 11
    assert twelfth["gap"] == 1.0, "one more kill closes the gap to 11th"
    assert twelfth["ranked_pilots"] == 12


@pytest.mark.django_db
def test_a_tie_is_reported_as_level_not_as_a_zero_gap(django_user_model):
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=1)
    ua, _ = enrol_pilot(django_user_model, 4501)
    ub, _ = enrol_pilot(django_user_model, 4502)
    home_kill(7500, attackers=[(4501, HOME, True)], when=now - timedelta(days=2))
    home_kill(7501, attackers=[(4502, HOME, True)], when=now - timedelta(days=2))

    second = rankings.pilot_standing(contest, "top_killers", ub)
    assert second["place"] == 2
    assert second["gap"] == 0.0
    assert second["tied_ahead"] is True, "level on kills, behind only on the tie-break"


@pytest.mark.django_db
def test_a_pilot_with_nothing_in_a_category_has_no_standing(django_user_model):
    contest = make_contest(start_days_ago=7, end_days_ahead=1)
    user, _ = enrol_pilot(django_user_model, 4601)
    assert rankings.pilot_standing(contest, "solo_kills", user) is None


@pytest.mark.django_db
def test_an_account_with_no_main_flagged_still_holds_only_one_place(django_user_model):
    """The rollup keys on the ACCOUNT, not on ``mains_for``.

    ``core.pilots.mains_for`` maps a linked character whose account has no ``is_main``
    flagged to *itself*, so rolling up by main leaves such an account as two rows. On a
    display board that is a cosmetic duplicate; here it would let one person hold two
    places on one board and collect the same fixed prize twice.
    """
    now = timezone.now()
    contest = make_contest(start_days_ago=7, end_days_ahead=1)
    user, main = enrol_pilot(django_user_model, 4701)
    EveCharacter.objects.create(character_id=4702, user=user, name="Second 4702",
                                is_main=False, is_corp_member=True)
    # Nobody is flagged as the main on this account.
    main.is_main = False
    main.save(update_fields=["is_main"])

    home_kill(7600, attackers=[(4701, HOME, True)], when=now - timedelta(days=2))
    home_kill(7601, attackers=[(4702, HOME, True)], when=now - timedelta(days=2))
    home_kill(7602, attackers=[(4702, HOME, True)], when=now - timedelta(days=2))

    rows = rankings.standings(contest, "top_killers", use_cache=False)
    assert len(rows) == 1, f"one account must hold exactly one place — got {rows}"
    assert rows[0]["user_id"] == user.pk
    assert rows[0]["value"] == 3.0, "all three kills count once, for the one account"
    # And the prize cannot be taken twice off the same board.
    assert rankings.winner_for(contest, "top_killers", 2) is None
