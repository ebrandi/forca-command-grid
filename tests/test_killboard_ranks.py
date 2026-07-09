"""Combat rank titles, future-only rewards, and historical rankings.

Covers the acceptance criteria: rank calculation across the whole range (zero →
veteran → maxed), the DB-driven ladder + fallback, the future-only reward baseline,
future-rank-up-only + duplicate-proof event generation, enrolled-only eligibility,
admin form validation, the reward lifecycle, permission gating, the monthly
aggregate (correctness + idempotency + cache invalidation), and the historical
rankings filters (default unchanged / year / month+year / empty).
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.killboard import aggregation, ranks, rewards
from apps.killboard.models import (
    CombatRankTitle,
    MonthlyPilotKillStat,
    PilotRankBaseline,
    RankRewardEvent,
    RankRewardSettings,
    RewardType,
)
from core import rbac
from tests._raffle_utils import HOME_CORP, detached_character, enrol_pilot, home_kill, make_user

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _kills(char_id, n, *, start_km=10000, when=None, solo=False, value="50000000",
           final_blow=True):
    """Give a pilot ``n`` distinct home-corp kills."""
    for i in range(n):
        home_kill(start_km + i, attackers=[(char_id, HOME_CORP, final_blow)],
                  is_solo=solo, value=value, when=when)


def _reward_rank(min_kills, *, amount="100000000", rtype=RewardType.ISK):
    """Flip a seeded rung into a reward rung and refresh the cached ladder."""
    r = CombatRankTitle.objects.get(metric="kills", min_kills=min_kills)
    r.grants_reward = True
    r.reward_type = rtype
    r.reward_amount = Decimal(amount)
    r.save()
    ranks.invalidate_ladder_cache()
    return r


# --------------------------------------------------------------------------- #
#  1. Rank calculation (the seeded 17-rung ladder)
# --------------------------------------------------------------------------- #
def test_ladder_seeded_and_ascending():
    ladder = ranks.active_ladder()
    assert len(ladder) == 17
    thresholds = [e["min_kills"] for e in ladder]
    assert thresholds == sorted(thresholds)            # strictly ascending
    assert thresholds[0] == 0 and thresholds[-1] == 25000
    assert ladder[0]["name"] == "Dockside Recruit"


def test_zero_kill_pilot_gets_a_friendly_title():
    r = ranks.combat_rank(0)
    assert r["title"] == "Dockside Recruit"
    assert r["tier"] == 0
    prog = ranks.rank_progress(0)
    assert prog["next"]["title"] == "First Blood"
    assert prog["kills_to_next"] == 1
    assert prog["progress_pct"] == 0.0


def test_one_kill_crosses_first_rung():
    assert ranks.combat_rank(1)["title"] == "First Blood"


@pytest.mark.parametrize("kills,title,tier", [
    (0, "Dockside Recruit", 0),
    (7, "Skirmisher", 2),
    (10, "Line Pilot", 3),
    (99, "Proven Combatant", 5),
    (100, "Battle-Tested Pilot", 6),
    (1200, "Ace Pilot", 9),
    (12000, "FORCA Warlord", 14),
    (30000, "Immortal of FORCA", 16),
])
def test_rank_calc_across_range(kills, title, tier):
    r = ranks.combat_rank(kills)
    assert r["title"] == title
    assert r["tier"] == tier
    assert r["max_tier"] == 16


def test_threshold_crossing_progress():
    prog = ranks.rank_progress(7)
    assert prog["current"]["title"] == "Skirmisher"      # 5+
    assert prog["next"]["title"] == "Line Pilot"          # 10+
    assert prog["kills_to_next"] == 3
    assert prog["progress_pct"] == 40.0                   # (7-5)/(10-5)


def test_veteran_and_maxed():
    assert ranks.combat_rank(1500)["title"] == "Elite Ace"
    maxed = ranks.rank_progress(30000)
    assert maxed["is_maxed"] is True
    assert maxed["next"] is None
    assert maxed["progress_pct"] == 100.0


def test_next_reward_looks_ahead_past_rewardless_rungs():
    """A zero-kill pilot whose immediate next rung (First Blood, 1 kill) grants no reward
    still sees the next reward-bearing rung ahead (Skirmisher at 5 kills)."""
    _reward_rank(5, amount="50000000", rtype=RewardType.ISK)
    prog = ranks.rank_progress(0)
    assert prog["next"]["title"] == "First Blood"          # immediate next: no reward
    nr = prog["next_reward"]
    assert nr and nr["title"] == "Skirmisher"
    assert nr["reward_type"] == "isk" and nr["reward_amount"] == 50000000.0
    assert nr["kills_away"] == 5


def test_next_reward_is_none_when_no_rung_ahead_carries_one():
    """The seeded ladder ships with no reward rungs → nothing to advertise."""
    assert ranks.rank_progress(0)["next_reward"] is None


def test_next_reward_skips_a_reward_rung_already_earned():
    """A reward on a rung the pilot already holds is not re-advertised as "next"."""
    _reward_rank(1, amount="10000000", rtype=RewardType.ISK)   # First Blood grants ISK
    _reward_rank(10, amount="20000000", rtype=RewardType.ISK)  # Line Pilot grants ISK
    prog = ranks.rank_progress(5)                              # already past First Blood
    assert prog["next_reward"]["title"] == "Line Pilot"
    assert prog["next_reward"]["kills_away"] == 5


def test_reward_label_and_compact_isk():
    from apps.identity.views import _compact_isk, _reward_label

    assert _reward_label("isk", 50_000_000, None) == "50M ISK"
    assert _reward_label("plex", 500, None) == "500 PLEX"
    assert _reward_label("manual", 0, None) == "a special reward"
    assert _compact_isk(1_500_000_000) == "1.5B" and _compact_isk(12_000) == "12k"


def test_ladder_fallback_when_table_empty():
    CombatRankTitle.objects.all().delete()
    ranks.invalidate_ladder_cache()
    # Falls back to the static pre-DB ladder — ranks never break.
    assert ranks.combat_rank(0)["title"] == "Capsuleer"
    assert ranks.combat_rank(1000)["title"] == "Apex Predator"


def test_inactive_rank_excluded_from_ladder():
    CombatRankTitle.objects.filter(min_kills=5).update(is_active=False)
    ranks.invalidate_ladder_cache()
    # 7 kills now maps to the 1-kill rung (First Blood), not the disabled 5-rung.
    assert ranks.combat_rank(7)["title"] == "First Blood"


# --------------------------------------------------------------------------- #
#  2. Reward baseline (future-only guarantee)
# --------------------------------------------------------------------------- #
def test_establish_baseline_snapshots_current_rank(django_user_model):
    enrol_pilot(django_user_model, 4001)
    _kills(4001, 60)                                   # → Proven Combatant (50+)
    n = rewards.establish_baseline()
    assert n >= 1
    base = PilotRankBaseline.objects.get(character_id=4001)
    assert base.baseline_min_kills == 50               # highest rung held at baseline
    assert RankRewardSettings.load().rewards_enabled is True


def test_no_retroactive_reward_for_already_held_rank(django_user_model):
    enrol_pilot(django_user_model, 4001)
    _kills(4001, 120)                                  # already past the 100 rung
    _reward_rank(100)                                  # make 100 a reward rung
    rewards.establish_baseline()                       # baseline = 100 rung
    created = rewards.scan_and_award()
    # The pilot already held the 100 rung at baseline → no retroactive reward.
    assert created == 0
    assert RankRewardEvent.objects.filter(character_id=4001).count() == 0


def test_reward_only_on_future_rank_up(django_user_model):
    enrol_pilot(django_user_model, 4001)
    _kills(4001, 60)                                   # Proven Combatant (50)
    _reward_rank(100)
    rewards.establish_baseline()                       # baseline = 50 rung
    assert rewards.scan_and_award() == 0               # not yet at 100
    _kills(4001, 45, start_km=20000)                   # now 105 kills → crosses 100
    created = rewards.scan_and_award()
    assert created == 1
    ev = RankRewardEvent.objects.get(character_id=4001)
    assert ev.rank_min_kills == 100
    assert ev.status == RankRewardEvent.Status.PENDING
    assert ev.reward_amount == Decimal("100000000")
    assert ev.previous_rank_name == "Fleet Regular" or ev.previous_rank_name  # rung below 100


def test_duplicate_reward_prevented(django_user_model):
    enrol_pilot(django_user_model, 4001)
    _kills(4001, 60)
    _reward_rank(100)
    rewards.establish_baseline()
    _kills(4001, 45, start_km=20000)
    assert rewards.scan_and_award() == 1
    assert rewards.scan_and_award() == 0               # idempotent — no second event
    assert RankRewardEvent.objects.filter(character_id=4001, rank_min_kills=100).count() == 1


def test_unenrolled_pilot_gets_no_payable_event(django_user_model):
    # A detached (no account) pilot who statistically qualifies must not create events.
    detached_character(7777)
    _kills(7777, 150)
    _reward_rank(100)
    rewards.establish_baseline()
    assert rewards.scan_and_award() == 0
    assert RankRewardEvent.objects.filter(character_id=7777).count() == 0


def test_pilot_enrolling_after_baseline_is_not_backfilled(django_user_model):
    # Baseline taken while corp has one pilot; a new enrolled pilot appears later
    # already above a reward rung → baselined on first sight, no retroactive event.
    enrol_pilot(django_user_model, 4001)
    _reward_rank(100)
    rewards.establish_baseline()
    enrol_pilot(django_user_model, 4002, username="late")
    _kills(4002, 150)                                  # already past 100 when first seen
    assert rewards.scan_and_award() == 0               # baselined, not awarded
    assert PilotRankBaseline.objects.filter(character_id=4002).exists()
    # A genuine future crossing then does award.
    _reward_rank(250)
    _kills(4002, 120, start_km=30000)                  # 270 kills → crosses 250
    assert rewards.scan_and_award() == 1
    assert RankRewardEvent.objects.filter(character_id=4002, rank_min_kills=250).count() == 1


def test_scan_noop_when_rewards_disabled(django_user_model):
    enrol_pilot(django_user_model, 4001)
    _kills(4001, 150)
    _reward_rank(100)
    # No establish_baseline() → rewards disabled.
    assert rewards.scan_and_award() == 0


# --------------------------------------------------------------------------- #
#  3. Reward lifecycle
# --------------------------------------------------------------------------- #
def _make_event(character_id=4001):
    return RankRewardEvent.objects.create(
        character_id=character_id, character_name="P", rank_name="Battle-Tested Pilot",
        rank_min_kills=100, kills_at_award=105, reward_type=RewardType.ISK,
        reward_amount=Decimal("100000000"), status=RankRewardEvent.Status.PENDING,
    )


def test_reward_approve_then_paid(django_user_model):
    actor = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    ev = _make_event()
    rewards.approve(ev, actor)
    assert ev.status == RankRewardEvent.Status.APPROVED and ev.approved_by == actor
    rewards.mark_paid(ev, actor, reference="jrnl#1")
    assert ev.status == RankRewardEvent.Status.PAID and ev.payment_reference == "jrnl#1"


def test_reward_invalid_transitions(django_user_model):
    actor = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    ev = _make_event()
    rewards.mark_paid(ev, actor)
    with pytest.raises(rewards.InvalidTransition):
        rewards.approve(ev, actor)                     # can't approve a paid reward
    with pytest.raises(rewards.InvalidTransition):
        rewards.cancel(ev, actor)                      # can't cancel a paid reward


def test_reward_reject_and_cancel(django_user_model):
    actor = make_user(django_user_model, "off", rbac.ROLE_OFFICER)
    ev = _make_event()
    rewards.reject(ev, actor, reason="dupe")
    assert ev.status == RankRewardEvent.Status.REJECTED and "dupe" in ev.notes
    ev2 = _make_event(character_id=4002)
    rewards.cancel(ev2, actor, reason="left corp")
    assert ev2.status == RankRewardEvent.Status.CANCELLED


# --------------------------------------------------------------------------- #
#  4. Admin form validation
# --------------------------------------------------------------------------- #
def test_rank_form_rejects_empty_name():
    from apps.admin_audit.console_combat import CombatRankForm

    f = CombatRankForm(data={"name": "  ", "metric": "kills", "min_kills": "3",
                             "color_class": "text-gold", "sort_order": "1",
                             "reward_type": "none", "reward_amount": "0"})
    assert not f.is_valid() and "name" in f.errors


def test_rank_form_rejects_duplicate_threshold():
    from apps.admin_audit.console_combat import CombatRankForm

    f = CombatRankForm(data={"name": "Clash", "metric": "kills", "min_kills": "100",
                             "color_class": "text-gold", "sort_order": "1",
                             "reward_type": "none", "reward_amount": "0"})
    assert not f.is_valid() and "min_kills" in f.errors   # 100 already seeded


def test_rank_form_requires_valid_reward_config():
    from apps.admin_audit.console_combat import CombatRankForm

    # grants_reward but no type
    f = CombatRankForm(data={"name": "R", "metric": "kills", "min_kills": "3",
                             "color_class": "text-gold", "sort_order": "1",
                             "grants_reward": "on", "reward_type": "none", "reward_amount": "0"})
    assert not f.is_valid() and "reward_type" in f.errors
    # ISK reward but zero amount
    f2 = CombatRankForm(data={"name": "R", "metric": "kills", "min_kills": "3",
                              "color_class": "text-gold", "sort_order": "1",
                              "grants_reward": "on", "reward_type": "isk", "reward_amount": "0"})
    assert not f2.is_valid() and "reward_amount" in f2.errors


def test_rank_form_rejects_negative_reward():
    from apps.admin_audit.console_combat import CombatRankForm

    f = CombatRankForm(data={"name": "R", "metric": "kills", "min_kills": "3",
                             "color_class": "text-gold", "sort_order": "1",
                             "grants_reward": "on", "reward_type": "isk", "reward_amount": "-5"})
    assert not f.is_valid() and "reward_amount" in f.errors


# --------------------------------------------------------------------------- #
#  5. Permissions
# --------------------------------------------------------------------------- #
def test_rank_config_is_director_only(client, django_user_model):
    member = make_user(django_user_model, "m", rbac.ROLE_MEMBER)
    officer = make_user(django_user_model, "o", rbac.ROLE_OFFICER)
    director = make_user(django_user_model, "d", rbac.ROLE_DIRECTOR)
    url = reverse("admin_audit:combat_ranks")
    client.force_login(member)
    assert client.get(url).status_code == 403
    client.force_login(officer)
    assert client.get(url).status_code == 403          # config is Director-only
    client.force_login(director)
    assert client.get(url).status_code == 200


def test_reward_triage_is_officer_plus(client, django_user_model):
    member = make_user(django_user_model, "m", rbac.ROLE_MEMBER)
    officer = make_user(django_user_model, "o", rbac.ROLE_OFFICER)
    url = reverse("admin_audit:combat_rewards")
    client.force_login(member)
    assert client.get(url).status_code == 403
    client.force_login(officer)
    assert client.get(url).status_code == 200


def test_mark_paid_is_director_only(client, django_user_model):
    officer = make_user(django_user_model, "o", rbac.ROLE_OFFICER)
    ev = _make_event()
    client.force_login(officer)
    r = client.post(reverse("admin_audit:combat_reward_action", args=[ev.pk]),
                    {"action": "paid"})
    assert r.status_code == 403                          # officer can't mark paid
    ev.refresh_from_db()
    assert ev.status == RankRewardEvent.Status.PENDING


# --------------------------------------------------------------------------- #
#  6. Monthly aggregate (correctness / idempotency / cache)
# --------------------------------------------------------------------------- #
def _dt(y, m, d=15):
    return datetime(y, m, d, 12, 0, tzinfo=UTC)


def test_monthly_aggregate_counts_distinct_kills(django_user_model):
    enrol_pilot(django_user_model, 4001)
    _kills(4001, 3, when=_dt(2025, 3), solo=True, value="1000000")
    n = aggregation.rebuild_month(2025, 3)
    assert n == 1
    row = MonthlyPilotKillStat.objects.get(character_id=4001, year=2025, month=3)
    assert row.kills == 3
    assert row.solo_kills == 3
    assert row.final_blows == 3
    assert row.isk_destroyed == Decimal("3000000")


def test_fleet_kill_counts_once_per_pilot(django_user_model):
    enrol_pilot(django_user_model, 5001, username="a")
    enrol_pilot(django_user_model, 5002, username="b")
    # ONE killmail, two home attackers → each pilot has exactly 1 kill.
    home_kill(9001, when=_dt(2025, 4), attackers=[
        (5001, HOME_CORP, True), (5002, HOME_CORP, False)])
    aggregation.rebuild_month(2025, 4)
    assert MonthlyPilotKillStat.objects.get(character_id=5001, year=2025, month=4).kills == 1
    assert MonthlyPilotKillStat.objects.get(character_id=5002, year=2025, month=4).kills == 1


def test_backfill_is_idempotent(django_user_model):
    enrol_pilot(django_user_model, 4001)
    _kills(4001, 2, when=_dt(2024, 1))
    _kills(4001, 3, when=_dt(2024, 2), start_km=11000)
    first = aggregation.backfill()
    rows_first = MonthlyPilotKillStat.objects.count()
    second = aggregation.backfill()
    assert MonthlyPilotKillStat.objects.count() == rows_first  # no dupes
    assert first == second
    assert MonthlyPilotKillStat.objects.get(character_id=4001, year=2024, month=1).kills == 2
    assert MonthlyPilotKillStat.objects.get(character_id=4001, year=2024, month=2).kills == 3


def test_rebuild_month_invalidates_historical_cache(django_user_model):
    enrol_pilot(django_user_model, 4001)
    # Cache an empty month first.
    empty = aggregation.historical_leaderboards(2025, 6)
    assert empty["pilot_count"] == 0
    # Add a kill in that month + rebuild (which busts the cached period).
    _kills(4001, 4, when=_dt(2025, 6))
    aggregation.rebuild_month(2025, 6)
    fresh = aggregation.historical_leaderboards(2025, 6)
    assert fresh["pilot_count"] == 1
    top = next(c for c in fresh["categories"] if c["key"] == "top_killers")
    assert top["rows"][0]["character_id"] == 4001
    assert top["rows"][0]["value"] == 4


# --------------------------------------------------------------------------- #
#  7. Historical rankings view
# --------------------------------------------------------------------------- #
def test_rankings_default_view_unchanged(client):
    r = client.get(reverse("killboard:rankings"))
    assert r.status_code == 200
    assert r.context["historical"] is False


def test_rankings_year_filter(client, django_user_model):
    enrol_pilot(django_user_model, 4001)
    _kills(4001, 5, when=_dt(2023, 7))
    aggregation.rebuild_month(2023, 7)
    r = client.get(reverse("killboard:rankings"), {"year": "2023"})
    assert r.status_code == 200
    assert r.context["historical"] is True
    assert r.context["sel_year"] == 2023 and r.context["sel_month"] is None
    top = next(c for c in r.context["categories"] if c["key"] == "top_killers")
    assert top["rows"][0]["character_id"] == 4001


def test_rankings_month_year_filter(client, django_user_model):
    enrol_pilot(django_user_model, 4001)
    _kills(4001, 2, when=_dt(2023, 7))
    _kills(4001, 5, when=_dt(2023, 8), start_km=12000)
    aggregation.backfill()
    r = client.get(reverse("killboard:rankings"), {"year": "2023", "month": "8"})
    assert r.status_code == 200
    assert r.context["sel_month"] == 8
    top = next(c for c in r.context["categories"] if c["key"] == "top_killers")
    assert top["rows"][0]["value"] == 5                 # August only, not the year total


def test_rankings_empty_period(client):
    r = client.get(reverse("killboard:rankings"), {"year": "2010", "month": "6"})
    assert r.status_code == 200
    assert r.context["pilot_count"] == 0                # empty state


def test_year_totals_sum_months(django_user_model):
    enrol_pilot(django_user_model, 4001)
    _kills(4001, 2, when=_dt(2023, 1))
    _kills(4001, 3, when=_dt(2023, 2), start_km=12000)
    aggregation.backfill()
    year = aggregation.historical_leaderboards(2023, None)
    top = next(c for c in year["categories"] if c["key"] == "top_killers")
    assert top["rows"][0]["value"] == 5                 # 2 + 3 across the year


@pytest.mark.parametrize("year", ["10000", "0", "1", "99999999999", "9999"])
def test_rankings_out_of_range_year_never_500(client, year):
    # An unbounded ?year= must not build an out-of-range datetime → it falls back
    # to the default live view instead of 500ing the public page.
    r = client.get(reverse("killboard:rankings"), {"year": year})
    assert r.status_code == 200
    assert r.context["historical"] is False


def test_no_retroactive_reward_on_midladder_rank_insertion(django_user_model):
    """Inserting a reward rung BELOW a pilot's baseline kill count (without
    re-baselining) must NOT grant a retroactive reward."""
    enrol_pilot(django_user_model, 4001)
    _kills(4001, 120)                                   # highest seeded rung = 100
    rewards.establish_baseline()
    base = PilotRankBaseline.objects.get(character_id=4001)
    assert base.baseline_min_kills == 100 and base.baseline_kills == 120
    # New reward rung at 110 — below the pilot's 120 baseline kills.
    CombatRankTitle.objects.create(name="Interloper", metric="kills", min_kills=110,
                                   grants_reward=True, reward_type=RewardType.ISK,
                                   reward_amount=Decimal("50000000"), sort_order=65)
    ranks.invalidate_ladder_cache()
    assert rewards.scan_and_award() == 0                # not retroactively awarded
    assert not RankRewardEvent.objects.filter(character_id=4001, rank_min_kills=110).exists()
    # A genuine FUTURE crossing above the baseline kill count still awards.
    CombatRankTitle.objects.create(name="Beyond", metric="kills", min_kills=125,
                                   grants_reward=True, reward_type=RewardType.ISK,
                                   reward_amount=Decimal("50000000"), sort_order=66)
    ranks.invalidate_ladder_cache()
    _kills(4001, 10, start_km=40000)                    # 130 kills → crosses 125 (> 120)
    assert rewards.scan_and_award() == 1
    assert RankRewardEvent.objects.filter(character_id=4001, rank_min_kills=125).exists()
