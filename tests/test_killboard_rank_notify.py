"""KB-1 — one-time combat rank-up celebrations via Pingboard.

Acceptance: a rank-up fires exactly one notification per rung; a first-seen pilot is
baselined silently (future-only, no retroactive flood); no repeat for the same rung; a
"reward pending" note only when rewards are armed; leadership can switch the event off.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.killboard import ranks
from apps.killboard.models import (
    CombatRankTitle,
    PilotRankNotification,
    RankRewardSettings,
    RewardType,
)
from apps.killboard.rank_notify import notify_rank_ups
from apps.pingboard import config
from apps.pingboard.models import Alert
from tests._raffle_utils import HOME_CORP, detached_character, enrol_pilot, home_kill

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_notifications_config():
    config.reset("notifications")
    yield
    config.reset("notifications")


def _kills(char_id, n, *, start_km=10000):
    for i in range(n):
        home_kill(start_km + i, attackers=[(char_id, HOME_CORP, True)], value="50000000")


def _reward_rank(min_kills, *, amount="100000000"):
    r = CombatRankTitle.objects.get(metric="kills", min_kills=min_kills)
    r.grants_reward = True
    r.reward_type = RewardType.ISK
    r.reward_amount = Decimal(amount)
    r.save()
    ranks.invalidate_ladder_cache()
    return r


def _rankup_alerts(user_id):
    return Alert.objects.filter(source_service="killboard", audience={"kind": "user", "id": user_id})


# --- future-only baseline ----------------------------------------------------
def test_first_run_baselines_silently(django_user_model):
    user, _ = enrol_pilot(django_user_model, 4001)
    _kills(4001, 60)  # Proven Combatant (50)
    sent = notify_rank_ups()
    assert sent == 0
    tracker = PilotRankNotification.objects.get(character_id=4001)
    assert tracker.last_notified_min_kills == 50
    assert tracker.last_notified_at is None  # silent baseline
    assert not _rankup_alerts(user.id).exists()


# --- one notification per rung ----------------------------------------------
def test_rank_up_notifies_once(django_user_model):
    user, _ = enrol_pilot(django_user_model, 4001)
    _kills(4001, 60)  # baseline at 50
    notify_rank_ups()
    _kills(4001, 45, start_km=20000)  # now 105 → crosses the 100 rung
    sent = notify_rank_ups()
    assert sent == 1
    alerts = _rankup_alerts(user.id)
    assert alerts.count() == 1
    a = alerts.first()
    assert "rank" in a.title.lower()
    tracker = PilotRankNotification.objects.get(character_id=4001)
    assert tracker.last_notified_min_kills == 100
    assert tracker.last_notified_at is not None


def test_no_repeat_for_same_rung(django_user_model):
    user, _ = enrol_pilot(django_user_model, 4001)
    _kills(4001, 60)
    notify_rank_ups()
    _kills(4001, 45, start_km=20000)
    assert notify_rank_ups() == 1
    assert notify_rank_ups() == 0  # deduped — same rung
    assert _rankup_alerts(user.id).count() == 1


# --- reward note only when armed --------------------------------------------
def test_reward_note_present_only_when_rewards_armed(django_user_model):
    user, _ = enrol_pilot(django_user_model, 4001)
    _kills(4001, 60)
    _reward_rank(100)
    notify_rank_ups()  # baseline @ 50
    _kills(4001, 45, start_km=20000)  # cross 100 (a reward rung)
    # rewards not yet armed → no reward note
    rewards_settings = RankRewardSettings.load()
    rewards_settings.rewards_enabled = False
    rewards_settings.save()
    notify_rank_ups()
    body = _rankup_alerts(user.id).first().body
    assert "reward" not in body.lower()


def test_reward_note_appears_when_armed(django_user_model):
    user, _ = enrol_pilot(django_user_model, 4001)
    _kills(4001, 60)
    _reward_rank(100)
    from apps.killboard import rewards as reward_engine

    reward_engine.establish_baseline()  # arms rewards + baselines @ 50
    notify_rank_ups()  # rank_notify baseline @ 50
    _kills(4001, 45, start_km=20000)
    notify_rank_ups()
    body = _rankup_alerts(user.id).first().body
    assert "reward is pending" in body.lower()
    assert "paid" not in body.lower()  # never states a completed payment


# --- leadership off switch ---------------------------------------------------
def test_disabled_event_is_a_noop(django_user_model):
    user, _ = enrol_pilot(django_user_model, 4001)
    _kills(4001, 60)
    config.set("notifications", {"events": {"killboard.rank_up": {"enabled": False}}})
    assert notify_rank_ups() == 0
    assert not PilotRankNotification.objects.filter(character_id=4001).exists()
    assert not _rankup_alerts(user.id).exists()


# --- detached pilots are ignored --------------------------------------------
def test_detached_pilot_not_tracked(django_user_model):
    detached_character(7777)
    _kills(7777, 150)
    assert notify_rank_ups() == 0
    assert not PilotRankNotification.objects.filter(character_id=7777).exists()
