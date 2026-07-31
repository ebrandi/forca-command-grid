"""P5 — the public rankings boards must not cost a scan per visitor.

Three defects are pinned here, each with the claim it is supposed to establish:

* **coverage** — the warmer used to fill 3 of the 12 reachable board variants (6 windows
  × the ``?by=main`` toggle), so nine of them, including the full-history ``all``, took a
  cold build on an anonymous click. It now fills all 12 in every enabled locale, and does
  so with a query count that does NOT grow with the number of locales: that independence,
  not a pinned number, is the actual claim, because it is what makes the wider coverage
  affordable on a 4-core HDD box.
* **stampede** — a cold or stale variant is rebuilt by exactly one request; the others are
  served the previous payload (or wait a bounded moment for the winner) instead of each
  running the same aggregation.
* **cost** — ``_active_days`` no longer materialises one Python object per (pilot, day)
  pair for the whole history of the corp. A verbatim copy of the pre-fix implementation is
  kept below as the correctness oracle, because "cheaper" is only acceptable if the answer
  is identical, including the day a pilot both killed and died on.

Every performance assertion is paired with an equality assertion against a known-good
value, so a future refactor cannot buy speed with a wrong board.
"""
from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db import connection
from django.db.models.functions import TruncDate
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone, translation

from apps.killboard.leaderboards import (
    CACHE_VERSION,
    WINDOW_KEYS,
    _active_days,
    _cache_keys,
    _home,
    _time_filter,
    leaderboards,
    warm_windows,
    window_for,
)
from apps.killboard.models import Killmail, KillmailParticipant

HOME = 98000001
ENEMY = 55555
A = 1001  # pilot A
B = 1002  # pilot B


def _kill(km_id, *, value, points, days_ago, attackers=(), victim_char=None,
          home_role=Killmail.HomeRole.ATTACKER, is_npc=False, is_solo=False):
    km = Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}",
        killmail_time=timezone.now() - timedelta(days=days_ago),
        solar_system_id=30000142, victim_ship_type_id=587,
        total_value=Decimal(value), points=points, is_solo=is_solo, is_npc=is_npc,
        involves_home_corp=True, home_corp_role=home_role,
        victim_character_id=victim_char,
        victim_corporation_id=HOME if home_role == Killmail.HomeRole.VICTIM else ENEMY,
    )
    for i, (char, corp, fb) in enumerate(attackers, start=1):
        KillmailParticipant.objects.create(
            killmail=km, role="attacker", seq=i, character_id=char,
            corporation_id=corp, ship_type_id=587, final_blow=fb, damage_done=100,
        )
    return km


@pytest.fixture
def isolated_cache():
    """A private, in-process cache for these tests.

    Everything here asserts on cache CONTENT — which key exists, whether a lock is held,
    whether a read cost a query — and the dev cache is a shared Redis that a parallel test
    run (or a warmer on the same box) also writes to. Pointing this module at its own
    LocMem backend is the difference between a real regression test and a coin flip.
    """
    private = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "p5-rankings-tests",
        }
    }
    with override_settings(CACHES=private):
        cache.clear()
        yield
        cache.clear()


@pytest.fixture
def rankings_data(db, settings, isolated_cache):
    """A board with no ties anywhere: every ISK value, kill count and points total is
    distinct, so a rebuild cannot legitimately order two rows differently from a warm
    and an equality assertion against a rebuild is meaningful.

    The kill on day 1 and the loss on day 1 are deliberate: that pilot was active on ONE
    day, not two, which is the case ``_active_days`` has to de-duplicate across its two
    sources. Kills are spread over several windows so ``30d``/``90d``/``all`` differ.
    """
    settings.FORCA_HOME_CORP_ID = HOME
    _kill(1, value="500000000", points=10, days_ago=1, is_solo=True,
          attackers=[(A, HOME, True)])
    _kill(2, value="400000000", points=8, days_ago=2,
          attackers=[(A, HOME, False), (B, HOME, True)])
    _kill(3, value="300000000", points=6, days_ago=40,       # outside 30d, inside 90d
          attackers=[(A, HOME, True)])
    _kill(4, value="900000000", points=20, days_ago=200,     # only "all" sees this
          attackers=[(B, HOME, True)])
    # A's PvP loss, on a day A already has a kill on (must count as ONE active day).
    _kill(10, value="250000000", points=1, days_ago=1, victim_char=A,
          home_role=Killmail.HomeRole.VICTIM, attackers=[(9000, ENEMY, True)])
    # B's PvP loss on its own day, plus a ratting death that no metric may count.
    _kill(11, value="150000000", points=1, days_ago=5, victim_char=B,
          home_role=Killmail.HomeRole.VICTIM, attackers=[(9000, ENEMY, True)])
    _kill(12, value="50000000", points=1, days_ago=3, victim_char=B, is_npc=True,
          home_role=Killmail.HomeRole.VICTIM, attackers=[(0, None, True)])
    return None


def _oracle_active_days(window) -> dict[int, int]:
    """Verbatim copy of the pre-fix ``_active_days`` — the correctness oracle.

    Kept here rather than imported so it cannot drift with the implementation it exists
    to check: if the union/Counter rewrite ever disagrees with the dict-of-sets it
    replaced, this test says so.
    """
    days: dict[int, set] = {}
    kill_days = (
        KillmailParticipant.objects.filter(
            _time_filter("killmail__killmail_time", window),
            role=KillmailParticipant.Role.ATTACKER,
            corporation_id=_home(),
            character_id__isnull=False,
            killmail__home_corp_role=Killmail.HomeRole.ATTACKER,
            killmail__is_npc=False,
        )
        .annotate(day=TruncDate("killmail__killmail_time"))
        .values_list("character_id", "day")
        .distinct()
    )
    loss_days = (
        Killmail.objects.filter(
            _time_filter("killmail_time", window),
            involves_home_corp=True,
            home_corp_role=Killmail.HomeRole.VICTIM,
            victim_character_id__isnull=False,
            is_npc=False,
        )
        .annotate(day=TruncDate("killmail_time"))
        .values_list("victim_character_id", "day")
        .distinct()
    )
    for cid, day in list(kill_days) + list(loss_days):
        days.setdefault(cid, set()).add(day)
    return {cid: len(ds) for cid, ds in days.items()}


# --- (c) the all-time aggregation ------------------------------------------------
@pytest.mark.django_db
def test_active_days_matches_the_previous_implementation(rankings_data):
    for window_key in WINDOW_KEYS:
        window = window_for(window_key)
        assert _active_days(window) == _oracle_active_days(window), window_key


@pytest.mark.django_db
def test_active_days_counts_a_kill_and_a_loss_on_one_day_once(rankings_data):
    """The known-good value behind the oracle: A killed and died on day 1 and killed
    again on day 2 — two active days, not three."""
    days = _active_days(window_for("7d"))
    assert days[A] == 2
    assert days[B] == 2  # a kill on day 2 and a loss on day 5


@pytest.mark.django_db
def test_active_days_is_one_round_trip(rankings_data, django_assert_num_queries):
    """The kill side and the loss side are de-duplicated against each other in the
    database now, so the unbounded ``all`` window costs ONE query and returns one row per
    (pilot, day) — not two queries whose rows Python then had to fold together."""
    with django_assert_num_queries(1):
        assert _active_days(window_for("all")) == {A: 3, B: 3}


# --- (a) warm coverage ------------------------------------------------------------
@pytest.mark.django_db
def test_warm_caches_fills_every_reachable_variant(rankings_data):
    """All six windows AND both alt-rollup modes, in every enabled locale. Nine of these
    twelve variants were never warmed before, ``all`` (a full-history scan) among them."""
    from apps.killboard.analytics import warm_caches, warm_languages

    warm_caches()
    for window_key in WINDOW_KEYS:
        for suffix in ("", ":main"):
            for lang in warm_languages():
                key = f"kb:lb:{CACHE_VERSION}:{HOME}:{window_key}{suffix}:{lang}"
                assert cache.get(key) is not None, key


@pytest.mark.django_db
def test_warmed_variant_costs_no_queries_to_read(rankings_data, django_assert_num_queries):
    """The point of warming: the anonymous request that follows it touches no database."""
    from apps.killboard.analytics import warm_caches

    warm_caches()
    with django_assert_num_queries(0):
        for window_key in WINDOW_KEYS:
            for by_main in (False, True):
                assert leaderboards(window_key, by_main=by_main)["window"]["key"] == window_key


@pytest.mark.django_db
def test_warm_cost_does_not_scale_with_locales(rankings_data):
    """The bound that makes the wider coverage affordable: rendering a board in another
    language is prose, not data, so a locale must add ZERO queries. Four locales warm four
    times as many entries as one for the same database cost."""
    with CaptureQueriesContext(connection) as one_locale:
        written_one = warm_windows(WINDOW_KEYS, ["en"])
    with CaptureQueriesContext(connection) as four_locales:
        written_four = warm_windows(WINDOW_KEYS, ["en", "de", "fr", "ja"])

    assert len(four_locales) == len(one_locale)
    assert written_four == 4 * written_one
    assert written_one == 2 * len(WINDOW_KEYS)  # both alt-rollup modes, every window


@pytest.mark.django_db
def test_warming_both_rollup_modes_is_cheaper_than_building_them(rankings_data):
    """``?by=main`` is a regrouping of the same rows, so warming both modes of the
    heaviest window must cost LESS than the two builds it replaces — one pass over the
    killmails plus the main-map lookup, not two passes."""
    with CaptureQueriesContext(connection) as separately:
        leaderboards("all", use_cache=False)
        leaderboards("all", use_cache=False, by_main=True)
    with CaptureQueriesContext(connection) as together:
        assert warm_windows(["all"], ["en"]) == 2

    assert len(together) < len(separately)


@pytest.mark.django_db
def test_warmed_payload_is_identical_to_a_direct_build(rankings_data):
    """Cheaper must also mean *same*. Every warmed variant has to equal what the
    unmemoized code path produces for that window and mode, row for row."""
    with translation.override("en"):
        warm_windows(WINDOW_KEYS, ["en"])
        for window_key in WINDOW_KEYS:
            for by_main in (False, True):
                key, _fresh, _lock = _cache_keys(window_key, by_main)
                expected = leaderboards(window_key, use_cache=False, by_main=by_main)
                assert cache.get(key) == expected, (window_key, by_main)


@pytest.mark.django_db
def test_boards_are_still_right(rankings_data):
    """A known-good expectation for the warmed payload itself, so an equality test
    against a rebuild can never pass by both sides being wrong."""
    with translation.override("en"):
        warm_windows(["all"], ["en"])
        key, _fresh, _lock = _cache_keys("all", False)
    payload = cache.get(key)
    boards = {c["key"]: c["rows"] for c in payload["categories"]}
    assert [(r["character_id"], r["value"]) for r in boards["top_killers"]] == [(A, 3), (B, 2)]
    assert boards["solo_kills"] == [
        {"place": 1, "character_id": A, "value": 1, "secondary": None}
    ]
    # The ratting death is excluded and only A's 250M PvP loss ranks.
    assert [(r["character_id"], r["value"]) for r in boards["isk_lost"]] == [
        (A, Decimal("250000000")), (B, Decimal("150000000")),
    ]
    # Most-valuable kills are the corp's own kills, biggest first.
    assert [k["killmail_id"] for k in payload["most_valuable"]] == [4, 1, 2, 3]


# --- (b) stampede protection ------------------------------------------------------
def _sentinel(window_key: str) -> dict:
    """A payload nothing could compute — proof that it was served, not rebuilt."""
    return {
        "window": {"key": window_key, "label": "sentinel"},
        "categories": [], "most_valuable": [], "pilot_count": -1,
        "efficiency_min_fights": 5,
    }


@pytest.mark.django_db
def test_stale_variant_is_served_while_another_request_rebuilds(
    rankings_data, django_assert_num_queries
):
    """The expensive case: ``?window=all`` is past its freshness and someone is already
    rebuilding it. Every other reader gets the previous payload for zero queries instead
    of starting its own full-history aggregation."""
    key, _fresh_key, lock_key = _cache_keys("all", False)
    cache.set(key, _sentinel("all"), 600)          # a payload, deliberately NOT marked fresh
    cache.add(lock_key, "another-worker", 600)     # someone else holds the rebuild

    with django_assert_num_queries(0):
        assert leaderboards("all") == _sentinel("all")


@pytest.mark.django_db
def test_cold_and_contended_variant_waits_for_the_winner(rankings_data, django_assert_num_queries):
    """Nothing cached at all AND the lock taken: the loser waits a bounded moment for the
    winner's payload rather than duplicating the build."""
    key, _fresh_key, lock_key = _cache_keys("all", False)
    cache.add(lock_key, "another-worker", 600)
    winner = threading.Timer(0.15, lambda: cache.set(key, _sentinel("all"), 600))
    winner.start()
    try:
        with django_assert_num_queries(0):
            assert leaderboards("all") == _sentinel("all")
    finally:
        winner.join()


@pytest.mark.django_db
def test_stale_variant_is_still_rebuilt_by_the_one_request_that_wins(rankings_data):
    """Serving stale must not mean never refreshing: with no rebuild in flight, the reader
    that finds a stale payload is the one that rebuilds and re-marks it fresh."""
    key, fresh_key, _lock_key = _cache_keys("all", False)
    cache.set(key, _sentinel("all"), 600)

    payload = leaderboards("all")
    assert payload["pilot_count"] == 2          # rebuilt from the real data, not the sentinel
    assert cache.get(fresh_key) is True
    assert cache.get(key) == payload
    assert cache.get(_cache_keys("all", False)[2]) is None  # the lock was released


@pytest.mark.django_db
def test_second_reader_of_a_freshly_built_variant_hits_the_cache(
    rankings_data, django_assert_num_queries
):
    first = leaderboards("90d")
    with django_assert_num_queries(0):
        assert leaderboards("90d") == first


@pytest.mark.django_db
def test_warm_tick_skips_when_the_previous_one_is_still_running(
    db, isolated_cache, django_assert_num_queries
):
    """The 5-minute beat must not stack on itself: a tick that is still running owns the
    lock, and the next firing is a no-op instead of a second full warm."""
    from apps.killboard.analytics import _WARM_LOCK_KEY, warm_caches

    cache.add(_WARM_LOCK_KEY, "the-running-tick", 600)
    with django_assert_num_queries(0):
        assert warm_caches() == 0
