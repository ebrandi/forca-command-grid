"""KB-37 (WS-D3) — gamification: trophies, kill-of-the-week, seasons, pod-SRP tiers, coaching.

Covers: the criteria DSL per metric type (award fires exactly at threshold, once, idempotent);
the award pipeline (future-only silent baseline, then ping + WS-B3 subscription fan-out + a reward
event through the EXISTING governance flow); the cursor-consumer scan (advance + resume); the
Kill-of-the-Week pick maths (at-kill value, tie→points, idempotent recompute, officer override that
survives recompute and is audited); seasonal composition from the monthly aggregate; the member-
gated CV page; pod-SRP implant tiers (flag OFF = unchanged payouts regression, ON = documented
bands, empty pod not covered); newbro no-implant coaching (only in the window, once); and that the
seed migration produced the default catalogue.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from apps.admin_audit.models import AuditLog
from apps.killboard import cv, kotw, seasons, trophies
from apps.killboard.models import (
    CombatMetric,
    KillboardStreamEvent,
    KillboardSubscription,
    Killmail,
    KillmailItem,
    KillmailParticipant,
    KillOfTheWeek,
    MonthlyPilotKillStat,
    PilotTrophy,
    PilotTrophyBaseline,
    RankRewardEvent,
    RewardSource,
    RewardType,
    SubscriptionChannel,
    SubscriptionEventType,
    TrophyDefinition,
    TrophyScanState,
)
from apps.pingboard import config as pingboard_config
from apps.pingboard.models import Alert
from apps.srp import services as srp
from apps.srp.models import POD_TYPE_IDS, SrpProgram
from core import rbac
from tests._raffle_utils import HOME_CORP, enrol_pilot, make_user

pytestmark = pytest.mark.django_db

ATTACKER = Killmail.HomeRole.ATTACKER
VICTIM = Killmail.HomeRole.VICTIM
POD = POD_TYPE_IDS[0]

# SDE ship type ids used by the class/role metrics.
RIFTER, BATTLESHIP, DREAD, GUARDIAN = 587, 24692, 19720, 11987


@pytest.fixture(autouse=True)
def _reset():
    pingboard_config.reset("notifications")
    cache.clear()
    yield
    cache.clear()
    pingboard_config.reset("notifications")


# --------------------------------------------------------------------------- #
#  Builders
# --------------------------------------------------------------------------- #
def _def(slug, criteria, *, tier="bronze", category="kills", enabled=True, **reward):
    return TrophyDefinition.objects.create(
        slug=slug, name=slug.replace("-", " ").title(), description="Test trophy",
        category=category, tier=tier, criteria=criteria, enabled=enabled, **reward,
    )


def _combat_metric(cid, *, kills=0, losses=0, solo=0, fb=0):
    CombatMetric.objects.update_or_create(
        entity_type=CombatMetric.EntityType.CHARACTER, entity_id=cid, window="all",
        defaults={"kills": kills, "losses": losses, "solo_kills": solo, "final_blows": fb},
    )
    cache.clear()  # pilot_combat_card is memoised


def _kill(km_id, cid, *, victim_ship=RIFTER, attacker_ship=RIFTER, value="50000000",
          sec_band="lowsec", solo=False, points=1, when=None):
    km = Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}", killmail_time=when or timezone.now(),
        solar_system_id=30000142, victim_ship_type_id=victim_ship,
        total_value=Decimal(value), value_at_kill=Decimal(value), points=points,
        is_solo=solo, is_npc=False, involves_home_corp=True, home_corp_role=ATTACKER,
        sec_band=sec_band, victim_character_id=666, victim_corporation_id=999,
    )
    KillmailParticipant.objects.create(
        killmail=km, role="attacker", seq=0, character_id=cid, corporation_id=HOME_CORP,
        ship_type_id=attacker_ship, final_blow=True, damage_done=100,
    )
    return km


def _loss(km_id, cid, *, ship=POD, when=None):
    return Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}", killmail_time=when or timezone.now(),
        solar_system_id=30000142, victim_ship_type_id=ship, total_value=Decimal("1000000"),
        involves_home_corp=True, home_corp_role=VICTIM,
        victim_character_id=cid, victim_corporation_id=HOME_CORP, sec_band="lowsec",
    )


def _ev(km, *, ship_class="Frigate"):
    return KillboardStreamEvent.objects.create(
        killmail=km, killmail_hash=km.killmail_hash, kill_time=km.killmail_time,
        home_role=km.home_corp_role, sec_band=km.sec_band, system_id=km.solar_system_id,
        ship_class=ship_class, victim_ship_type_id=km.victim_ship_type_id,
        victim_character_id=km.victim_character_id, victim_corporation_id=km.victim_corporation_id,
        total_value=km.total_value,
    )


def _sde():
    from apps.sde.models import SdeCategory, SdeGroup, SdeType

    SdeCategory.objects.get_or_create(category_id=6, defaults={"name": "Ship"})
    groups = {25: "Frigate", 27: "Battleship", 883: "Dreadnought", 832: "Logistics"}
    for gid, name in groups.items():
        SdeGroup.objects.get_or_create(group_id=gid, defaults={"category_id": 6, "name": name})
    for tid, gid in {RIFTER: 25, BATTLESHIP: 27, DREAD: 883, GUARDIAN: 832}.items():
        SdeType.objects.get_or_create(type_id=tid, defaults={"group_id": gid, "name": f"Type {tid}"})


# --------------------------------------------------------------------------- #
#  Criteria DSL
# --------------------------------------------------------------------------- #
def test_dsl_count_metric_fires_exactly_at_threshold():
    crit = {"metric": "kills", "threshold": 100}
    assert trophies.progress_for(1, crit, {"kills": 99})[1] is False
    assert trophies.progress_for(1, crit, {"kills": 100})[1] is True
    assert trophies.progress_for(1, crit, {"kills": 101})[1] is True


def test_dsl_solo_and_final_blows():
    assert trophies.progress_for(1, {"metric": "solo_kills", "threshold": 10}, {"solo_kills": 10})[1]
    assert trophies.progress_for(1, {"metric": "final_blows", "threshold": 5}, {"final_blows": 4})[1] is False


def test_dsl_kill_value_at_least():
    cid = 5001
    _kill(1, cid, value="500000000")
    _kill(2, cid, value="2000000000")  # 2B — the biggest single kill
    crit = {"metric": "kill_value_at_least", "isk": 1_000_000_000}
    value, met, _prog = trophies.progress_for(cid, crit, {})
    assert met and value == 2_000_000_000
    assert trophies.progress_for(cid, {"metric": "kill_value_at_least", "isk": 5_000_000_000}, {})[1] is False


def test_dsl_ship_class_kills():
    _sde()
    cid = 5002
    _kill(1, cid, victim_ship=BATTLESHIP)
    _kill(2, cid, victim_ship=BATTLESHIP)
    _kill(3, cid, victim_ship=RIFTER)  # not a battleship
    crit = {"metric": "ship_class_kills", "class": "Battleship", "threshold": 2}
    value, met, _p = trophies.progress_for(cid, crit, {})
    assert met and value == 2
    # A capital class kill (dreadnought) counts for Capital only.
    _kill(4, cid, victim_ship=DREAD)
    assert trophies.progress_for(cid, {"metric": "ship_class_kills", "class": "Capital", "threshold": 1}, {})[1]


def test_dsl_sec_band_kills():
    cid = 5003
    _kill(1, cid, sec_band="nullsec")
    _kill(2, cid, sec_band="nullsec")
    _kill(3, cid, sec_band="highsec")
    crit = {"metric": "sec_band_kills", "band": "nullsec", "threshold": 2}
    assert trophies.progress_for(cid, crit, {})[1]
    assert trophies.progress_for(cid, {"metric": "sec_band_kills", "band": "nullsec", "threshold": 3}, {})[1] is False


def test_dsl_role_on_kill_logi_inferable_others_not():
    _sde()
    cid = 5004
    _kill(1, cid, attacker_ship=GUARDIAN)  # dedicated logi hull
    _kill(2, cid, attacker_ship=GUARDIAN)
    _kill(3, cid, attacker_ship=RIFTER)    # dps
    assert trophies.progress_for(cid, {"metric": "role_on_kill", "role": "logi", "threshold": 2}, {})[1]
    # tackle/ewar are not inferable from a hull alone → always 0.
    assert trophies.progress_for(cid, {"metric": "role_on_kill", "role": "tackle", "threshold": 1}, {})[1] is False


def test_dsl_unknown_metric_never_matches():
    value, met, _p = trophies.progress_for(1, {"metric": "bogus", "threshold": 1}, {"kills": 999})
    assert value == 0 and met is False


# --------------------------------------------------------------------------- #
#  Award pipeline (future-only + idempotent + side-effects)
# --------------------------------------------------------------------------- #
def test_first_sight_baselines_silently(django_user_model):
    user, _ = enrol_pilot(django_user_model, 6001)
    _combat_metric(6001, kills=100)
    d = _def("centurion", {"metric": "kills", "threshold": 100})
    n = trophies.evaluate_pilot(6001, "P", user.id, [d], trigger_km_id=None)
    assert n == 0  # silent baseline — no celebration
    pt = PilotTrophy.objects.get(character_id=6001, definition=d)
    assert pt.notified is False and pt.killmail_id is None
    assert PilotTrophyBaseline.objects.filter(character_id=6001).exists()
    assert not _trophy_alerts(user.id).exists()


def test_award_after_baseline_is_celebrated_once(django_user_model):
    user, _ = enrol_pilot(django_user_model, 6002)
    d = _def("centurion", {"metric": "kills", "threshold": 100})
    _combat_metric(6002, kills=50)
    assert trophies.evaluate_pilot(6002, "P", user.id, [d], trigger_km_id=None) == 0  # baseline @ 50
    _combat_metric(6002, kills=100)  # cross the threshold
    assert trophies.evaluate_pilot(6002, "P", user.id, [d], trigger_km_id=777) == 1
    pt = PilotTrophy.objects.get(character_id=6002, definition=d)
    assert pt.notified is True and pt.killmail_id == 777
    # Idempotent — a re-run awards nothing new.
    assert trophies.evaluate_pilot(6002, "P", user.id, [d], trigger_km_id=888) == 0
    assert PilotTrophy.objects.filter(character_id=6002).count() == 1
    assert _trophy_alerts(user.id).count() == 1


def test_award_fires_subscription_fan_out(django_user_model):
    user, _ = enrol_pilot(django_user_model, 6003)
    sub = KillboardSubscription.objects.create(
        user=user, event_type=SubscriptionEventType.TROPHY_AWARDED,
        channel=SubscriptionChannel.RSS, rss_token="tok-6003",
    )
    d = _def("centurion", {"metric": "kills", "threshold": 100})
    _combat_metric(6003, kills=50)
    trophies.evaluate_pilot(6003, "P", user.id, [d], trigger_km_id=None)  # baseline
    _combat_metric(6003, kills=100)
    trophies.evaluate_pilot(6003, "P", user.id, [d], trigger_km_id=1)
    assert sub.feed_events.count() == 1
    assert sub.feed_events.get().event_type == SubscriptionEventType.TROPHY_AWARDED


def test_award_creates_reward_event_through_governance(django_user_model):
    from apps.killboard import rewards

    user, _ = enrol_pilot(django_user_model, 6004)
    officer = make_user(django_user_model, "kb-officer", rbac.ROLE_OFFICER)
    settings_row = rewards.RankRewardSettings.load()
    settings_row.rewards_enabled = True
    settings_row.save()
    d = _def("centurion", {"metric": "kills", "threshold": 100}, grants_reward=True,
             reward_type=RewardType.ISK, reward_amount=Decimal("100000000"))
    _combat_metric(6004, kills=50)
    trophies.evaluate_pilot(6004, "P", user.id, [d], trigger_km_id=None)  # baseline
    _combat_metric(6004, kills=100)
    trophies.evaluate_pilot(6004, "P", user.id, [d], trigger_km_id=1)

    ev = RankRewardEvent.objects.get(character_id=6004, source=RewardSource.TROPHY)
    assert ev.trophy_id == d.id and ev.source_key == f"trophy:{d.id}"
    assert ev.reward_amount == Decimal("100000000")
    # Flows through the EXISTING lifecycle unchanged.
    rewards.approve(ev, officer)
    assert ev.status == RankRewardEvent.Status.APPROVED


def test_reward_event_not_created_when_rewards_off(django_user_model):
    user, _ = enrol_pilot(django_user_model, 6005)
    d = _def("centurion", {"metric": "kills", "threshold": 100}, grants_reward=True,
             reward_type=RewardType.ISK, reward_amount=Decimal("100000000"))
    _combat_metric(6005, kills=50)
    trophies.evaluate_pilot(6005, "P", user.id, [d], trigger_km_id=None)
    _combat_metric(6005, kills=100)
    trophies.evaluate_pilot(6005, "P", user.id, [d], trigger_km_id=1)
    assert not RankRewardEvent.objects.filter(character_id=6005).exists()


def _trophy_alerts(user_id):
    return Alert.objects.filter(
        source_service="killboard", audience={"kind": "user", "id": user_id},
        title__icontains="trophy",
    )


# --------------------------------------------------------------------------- #
#  Cursor-consumer scan
# --------------------------------------------------------------------------- #
def test_scan_awards_touched_pilots_and_advances_cursor(django_user_model):
    user, _ = enrol_pilot(django_user_model, 6100)
    # Control the definition set: disable the seeded catalogue so only this trophy is in play.
    TrophyDefinition.objects.update(enabled=False)
    _def("centurion", {"metric": "kills", "threshold": 100})
    _combat_metric(6100, kills=50)
    _ev(_kill(1, 6100))  # a fresh kill touches the pilot
    assert trophies.scan_trophies()["awarded"] == 0  # first sight → silent baseline
    state = TrophyScanState.load()
    assert state.last_seq > 0

    _combat_metric(6100, kills=100)
    _ev(_kill(2, 6100))  # a newer kill event
    res = trophies.scan_trophies()
    assert res["awarded"] == 1
    # Resume: nothing new → no-op.
    assert trophies.scan_trophies()["awarded"] == 0


# --------------------------------------------------------------------------- #
#  Kill of the Week
# --------------------------------------------------------------------------- #
def _week_when(iso_year=2026, iso_week=28):
    start, _end = kotw._week_range(iso_year, iso_week)
    return start + dt.timedelta(days=1)


def test_kotw_picks_top_by_value():
    when = _week_when()
    _kill(1, 7001, value="1000000000", when=when)
    _kill(2, 7002, value="9000000000", when=when)  # the whale
    res = kotw.pick_kill_of_the_week(2026, 28)
    assert res["killmail_id"] == 2
    row = KillOfTheWeek.objects.get(iso_year=2026, iso_week=28)
    assert row.value == Decimal("9000000000") and row.character_id == 7002


def test_kotw_tie_broken_by_points():
    when = _week_when()
    _kill(1, 7001, value="5000000000", points=3, when=when)
    _kill(2, 7002, value="5000000000", points=9, when=when)  # same value, more points
    kotw.pick_kill_of_the_week(2026, 28)
    assert KillOfTheWeek.objects.get(iso_year=2026, iso_week=28).killmail_id == 2


def test_kotw_idempotent_recompute():
    when = _week_when()
    _kill(1, 7001, value="9000000000", when=when)
    kotw.pick_kill_of_the_week(2026, 28)
    kotw.pick_kill_of_the_week(2026, 28)  # recompute
    assert KillOfTheWeek.objects.filter(iso_year=2026, iso_week=28).count() == 1


def test_kotw_override_survives_recompute(django_user_model):
    when = _week_when()
    top = _kill(1, 7001, value="9000000000", when=when)  # noqa: F841 — the auto-pick would choose this
    pinned = _kill(2, 7002, value="1000000000", when=when)
    officer = make_user(django_user_model, "ko", rbac.ROLE_OFFICER)
    kotw.set_override(2026, 28, pinned, officer)
    kotw.pick_kill_of_the_week(2026, 28)  # must NOT clobber the override
    row = KillOfTheWeek.objects.get(iso_year=2026, iso_week=28)
    assert row.killmail_id == 2 and row.is_override is True


def test_kotw_override_view_is_audited(django_user_model):
    user, _ = enrol_pilot(django_user_model, 7100, roles=(rbac.ROLE_OFFICER, rbac.ROLE_MEMBER))
    km = _kill(1, 7100, value="1000000000", when=_week_when())
    c = Client()
    c.force_login(user)
    r = c.post("/killboard/kotw/override/",
               {"iso_year": "2026", "iso_week": "28", "killmail_id": str(km.killmail_id)})
    assert r.status_code == 302
    assert KillOfTheWeek.objects.get(iso_year=2026, iso_week=28).is_override is True
    assert AuditLog.objects.filter(action="killboard.kotw_override").exists()


# --------------------------------------------------------------------------- #
#  Seasons
# --------------------------------------------------------------------------- #
def test_season_composition_sums_the_quarter():
    # Q3 2026 = months 7, 8, 9. Two pilots across two of those months.
    MonthlyPilotKillStat.objects.create(character_id=8001, year=2026, month=7, kills=10, isk_destroyed=Decimal("100"))
    MonthlyPilotKillStat.objects.create(character_id=8001, year=2026, month=8, kills=5, isk_destroyed=Decimal("50"))
    MonthlyPilotKillStat.objects.create(character_id=8002, year=2026, month=9, kills=4)
    boards = seasons.compute_boards(2026, 3)
    top = boards["top_killers"]
    assert top[0] == {"place": 1, "character_id": 8001, "value": 15}  # 10 + 5, exact
    assert top[1]["character_id"] == 8002 and top[1]["value"] == 4


def test_season_snapshot_and_payload():
    MonthlyPilotKillStat.objects.create(character_id=8003, year=2025, month=1, kills=7)
    snap = seasons.snapshot_season(2025, 1)
    assert snap.boards["top_killers"][0]["character_id"] == 8003
    payload = seasons.season_payload(2025, 1)  # a past season → served from the snapshot
    assert payload["boards"]["top_killers"][0]["character_id"] == 8003


# --------------------------------------------------------------------------- #
#  PVP CV page
# --------------------------------------------------------------------------- #
def test_cv_member_gated(django_user_model):
    outsider = make_user(django_user_model, "outsider")  # no member role
    c = Client()
    c.force_login(outsider)
    assert c.get("/killboard/pilot/9001/cv/").status_code == 403


def test_cv_renders_sections(django_user_model):
    user, _ = enrol_pilot(django_user_model, 9002)
    _combat_metric(9002, kills=100, losses=10)
    d = _def("centurion", {"metric": "kills", "threshold": 100})
    PilotTrophy.objects.create(character_id=9002, definition=d, notified=True)
    c = Client()
    c.force_login(user)
    r = c.get("/killboard/pilot/9002/cv/")
    assert r.status_code == 200
    body = r.content.decode()
    assert "PVP CV" in body and "Trophies" in body


def test_pilot_cv_payload_assembles_sections(django_user_model):
    enrol_pilot(django_user_model, 9003)
    _combat_metric(9003, kills=100)
    _kill(1, 9003, value="3000000000")
    payload = cv.pilot_cv(9003)
    assert payload["best_kill"]["value"] == 3_000_000_000
    assert payload["favourite_hull"]["ship_type_id"] == RIFTER
    assert "rank_progress" in payload and "trophies" in payload


def test_hall_pages_render_for_member(django_user_model):
    user, _ = enrol_pilot(django_user_model, 9100)
    when = _week_when()
    _kill(1, 9100, value="9000000000", when=when)
    kotw.pick_kill_of_the_week(2026, 28)
    MonthlyPilotKillStat.objects.create(character_id=9100, year=2026, month=7, kills=5)
    c = Client()
    c.force_login(user)
    for path in ("/killboard/trophies/", "/killboard/seasons/", "/killboard/kotw/"):
        assert c.get(path).status_code == 200, path


def test_hall_pages_member_gated(django_user_model):
    outsider = make_user(django_user_model, "outsider2")
    c = Client()
    c.force_login(outsider)
    for path in ("/killboard/trophies/", "/killboard/seasons/", "/killboard/kotw/"):
        assert c.get(path).status_code == 403, path


# --------------------------------------------------------------------------- #
#  Pod-SRP implant tiers
# --------------------------------------------------------------------------- #
def _pod_program():
    SrpProgram.objects.all().delete()
    return SrpProgram.objects.create(
        name="Pods", is_active=True, enabled=True, cover_pod=True,
        require_doctrine=False, valuation=SrpProgram.Valuation.ACTUAL_LOSS,
        payout_mode=SrpProgram.PayoutMode.ISK_FULL,
    )


def _pod_loss_with_implants(km_id, cid, n_implants, unit_value="200000000"):
    km = _loss(km_id, cid, ship=POD)
    km.destroyed_value = Decimal(unit_value) * n_implants
    km.save(update_fields=["destroyed_value"])
    for i in range(n_implants):
        KillmailItem.objects.create(
            killmail=km, idx=i, item_type_id=20000 + i, flag=89,  # implant slot
            quantity_destroyed=1, unit_value=Decimal(unit_value),
        )
    return km


@override_settings(SRP_POD_TIERS_ENABLED=False)
def test_pod_tiers_flag_off_leaves_payout_unchanged():
    program = _pod_program()
    km = _pod_loss_with_implants(1, 4242, 6)  # a rich pod
    info = srp.eligibility(km, program)
    # Flag OFF: pod covered on actual-loss basis, uncapped (the regression guarantee).
    assert info["eligible"] is True
    assert info.get("pod_tier") is None
    assert info["payout"] == Decimal("1200000000")  # 6 × 200M, not capped


@override_settings(SRP_POD_TIERS_ENABLED=True)
def test_pod_tiers_flag_on_caps_payout():
    program = _pod_program()
    km = _pod_loss_with_implants(1, 4242, 6)  # 6 implants worth 1.2B → 'high-grade' tier, cap 5B
    info = srp.eligibility(km, program)
    assert info["eligible"] is True and info["pod_tier"] == "high-grade"
    # A modest pod: 2 cheap implants → 'basic' tier (cap 50M) bounds the payout.
    km2 = _pod_loss_with_implants(2, 4243, 2, unit_value="40000000")  # 80M actual
    info2 = srp.eligibility(km2, program)
    assert info2["pod_tier"] == "basic" and info2["payout"] == Decimal("50000000")


@override_settings(SRP_POD_TIERS_ENABLED=True)
def test_pod_with_no_implants_not_covered():
    program = _pod_program()
    km = _loss(1, 4244, ship=POD)  # empty pod, no implant items
    info = srp.eligibility(km, program)
    assert info["eligible"] is False


# --------------------------------------------------------------------------- #
#  Newbro no-implant coaching
# --------------------------------------------------------------------------- #
def test_newbro_no_implant_coaching_fires_once(django_user_model):
    user, _ = enrol_pilot(django_user_model, 4300)
    # A newbro (few engagements) loses an empty pod.
    km = _loss(1, 4300, ship=POD)
    _ev(km, ship_class="Capsule")
    res = trophies.scan_trophies()
    assert res["coached"] == 1
    assert _coaching_alerts(4300).count() == 1
    # A second fresh pod loss must NOT re-nudge (once per pilot).
    km2 = _loss(2, 4300, ship=POD)
    _ev(km2, ship_class="Capsule")
    res2 = trophies.scan_trophies()
    assert res2["coached"] == 0
    assert _coaching_alerts(4300).count() == 1


def test_coaching_skipped_when_pod_had_implants(django_user_model):
    enrol_pilot(django_user_model, 4301)
    km = _pod_loss_with_implants(1, 4301, 3)  # implants present
    _ev(km, ship_class="Capsule")
    assert trophies.scan_trophies()["coached"] == 0


def test_coaching_skipped_for_veteran(django_user_model):
    enrol_pilot(django_user_model, 4302)
    _combat_metric(4302, kills=100, losses=5)  # well past the newbro window
    km = _loss(1, 4302, ship=POD)
    _ev(km, ship_class="Capsule")
    assert trophies.scan_trophies()["coached"] == 0


def _coaching_alerts(cid):
    return Alert.objects.filter(source_service="killboard", source_object_id=f"noimplant:{cid}")


# --------------------------------------------------------------------------- #
#  Seed migration
# --------------------------------------------------------------------------- #
def test_seed_migration_produced_default_catalogue():
    # The data migration 0024 seeds a spread across categories; a few known slugs must exist.
    slugs = set(TrophyDefinition.objects.values_list("slug", flat=True))
    assert {"first-blood", "warlord", "whale-slayer", "guardian-angel"} <= slugs
    assert TrophyDefinition.objects.filter(enabled=True).count() >= 10
