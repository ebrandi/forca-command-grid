"""4.3 — Multi-metric rank tracks for support roles.

Acceptance: alongside the primary KILLS rank, a pilot has parallel rank tracks on
solo kills / final blows / active days — so a zero-kill logi/support/regular pilot
still has a rung to climb. Each track is DB-driven per metric with a sensible static
fallback; the reward engine (kills-only) is untouched.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache

from apps.killboard import ranks
from apps.killboard.models import CombatMetric, CombatRankTitle, MonthlyPilotKillStat, RankMetric

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_ladder_cache():
    cache.clear()
    yield
    cache.clear()


def test_fallback_ladder_per_metric():
    # The new support metrics have no seeded DB rows, so they use their static fallback.
    solo = ranks.active_ladder(RankMetric.SOLO_KILLS)
    assert [r["name"] for r in solo][:3] == ["Wingman", "Lone Wolf", "Duelist"]
    days = ranks.active_ladder(RankMetric.ACTIVE_DAYS)
    assert days[0]["name"] == "Visitor" and days[-1]["name"] == "Pillar"
    # Distinct ladder from the (seeded) kills track.
    assert solo[0]["name"] not in {r["name"] for r in ranks.active_ladder(RankMetric.KILLS)}


def test_default_metric_is_kills_backcompat():
    assert ranks.active_ladder() == ranks.active_ladder(RankMetric.KILLS)


def test_db_rows_override_fallback_for_a_metric():
    CombatRankTitle.objects.create(name="Custom Solo", metric=RankMetric.SOLO_KILLS,
                                   min_kills=3, is_active=True, is_visible=True, sort_order=0)
    ladder = ranks.active_ladder(RankMetric.SOLO_KILLS, use_cache=False)
    assert [r["name"] for r in ladder] == ["Custom Solo"]
    # The kills ladder is unaffected — the solo row never leaks into it.
    assert "Custom Solo" not in {r["name"] for r in ranks.active_ladder(RankMetric.KILLS, use_cache=False)}


def test_pilot_track_standings_titles_by_count():
    tracks = {t["metric"]: t for t in ranks.pilot_track_standings(
        {"solo_kills": 12, "final_blows": 30, "active_days": 23})}
    assert tracks["solo_kills"]["current"]["title"] == "Duelist"      # 10 <= 12 < 30
    assert tracks["final_blows"]["current"]["title"] == "Executioner"  # 25 <= 30 < 75
    assert tracks["active_days"]["current"]["title"] == "Committed"    # 20 <= 23 < 60
    # progress + next are populated for a non-maxed track
    assert tracks["solo_kills"]["next"]["title"] == "Solo Hunter"
    assert 0 <= tracks["solo_kills"]["progress_pct"] <= 100


def test_zero_count_still_offers_a_rung():
    tracks = {t["metric"]: t for t in ranks.pilot_track_standings({})}
    solo = tracks["solo_kills"]
    assert solo["count"] == 0 and solo["current"]["title"] == "Wingman"
    assert solo["next"]["title"] == "Lone Wolf" and not solo["is_maxed"]


def test_pilot_metric_counts_reads_stats():
    cid = 8001
    CombatMetric.objects.create(entity_type=CombatMetric.EntityType.CHARACTER, entity_id=cid,
                                window="all", kills=60, solo_kills=12, final_blows=30)
    MonthlyPilotKillStat.objects.create(character_id=cid, year=2026, month=6, active_days=8)
    MonthlyPilotKillStat.objects.create(character_id=cid, year=2026, month=5, active_days=15)
    counts = ranks.pilot_metric_counts(cid)
    assert counts == {"kills": 60, "solo_kills": 12, "final_blows": 30, "active_days": 23}


def test_track_ladders_carry_no_rewards():
    # The support tracks are recognition-only; nothing on them grants a reward.
    for metric in (RankMetric.SOLO_KILLS, RankMetric.FINAL_BLOWS, RankMetric.ACTIVE_DAYS):
        assert all(not r["grants_reward"] for r in ranks.active_ladder(metric))


def test_console_form_rejects_reward_on_support_track():
    from apps.admin_audit.console_combat import CombatRankForm
    base = {
        "color_class": "text-gold", "sort_order": 0, "is_active": "on", "is_visible": "on",
        "grants_reward": "on", "reward_type": "isk", "reward_amount": "1000000",
    }
    # A reward on a support-role track is rejected (engine only pays kills).
    bad = CombatRankForm(data={"name": "Solo Reward", "metric": RankMetric.SOLO_KILLS,
                               "min_kills": 10, **base})
    assert not bad.is_valid() and "grants_reward" in bad.errors
    # The same reward config on the kills track is accepted.
    ok = CombatRankForm(data={"name": "Kill Reward", "metric": RankMetric.KILLS,
                              "min_kills": 99999, **base})
    assert ok.is_valid(), ok.errors
