"""Performance regressions for the campaign board, battle report and watchlist intel.

Three audit findings are pinned here, each with a COST claim and a RESULT claim, because
a cost fix that quietly changes a number is worse than the slow version it replaced:

* **P4 (remainder)** — ``combat_campaigns._srp_spend`` re-resolved the active SRP programme
  once per estimated loss, and ``_compute_stats`` built the officer-only SRP/compliance
  overlay for everyone, including the anonymous slug permalink that can never render it.
* **P7** — the battle report's derived blocks (kill timeline, per-side role composition)
  were rebuilt from raw Killmail/Participant/Item rows on every request, on a page that
  ``battle_report_public`` serves to anonymous callers with no login and no rate limit.
* **P9** — ``intel.entry_activity`` materialised every killmail id a watched entity had
  ever touched and fed the list straight back as an ``IN (…)`` bind list.

The cost assertions deliberately assert a BOUND that does not scale with the loop variable
(or that a specific table is not touched at all) rather than an exact query number, so they
keep their meaning as unrelated queries come and go.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.killboard import battle_sides, combat_campaigns, roles
from apps.killboard.intel import entry_activity, watchlist_overview
from apps.killboard.models import (
    BattleReport,
    CombatCampaign,
    Killmail,
    KillmailParticipant,
    Watchlist,
    WatchlistEntry,
)
from apps.sso.services import ensure_role
from core import rbac

HOME_CORP, ENEMY_CORP = 98000001, 98000002  # FORCA_HOME_CORP_ID in test settings
OUR_A, OUR_B = 95000001, 95000002
ENEMY_A, ENEMY_B = 97000001, 97000002
GUARDIAN, STABBER, RIFTER = 11987, 622, 587
JITA, TAMA = 30000142, 30002813
THE_FORGE = 10000002

_VICTIM = Killmail.HomeRole.VICTIM
_ATTACKER = Killmail.HomeRole.ATTACKER

# Table names used to prove a code path was (or was not) walked. Asserting on the table
# rather than on a total count keeps the test honest when unrelated queries move around.
_SRP_PROGRAM_TABLE = "srp_srpprogram"
_SRP_RULE_TABLE = "srp_srprule"
_ITEM_TABLE = "killboard_killmailitem"


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _user(django_user_model, role, suffix=""):
    user, _ = django_user_model.objects.get_or_create(username=f"perf-cb-{role}{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


def _permissive_program():
    """An SRP programme that pays every loss, so the estimate path is always exercised."""
    from apps.srp.models import SrpProgram

    return SrpProgram.objects.create(
        name="Perf", is_active=True, enabled=True,
        payout_mode=SrpProgram.PayoutMode.ISK_FULL,
        valuation=SrpProgram.Valuation.ACTUAL_LOSS,
        require_doctrine=False, require_fleet_op=False,
    )


def _km(kid, t, *, victim_ship, victim_corp, victim_char, value, role,
        system=JITA, region=THE_FORGE, destroyed=0):
    return Killmail.objects.create(
        killmail_id=kid, killmail_hash=f"h{kid}", killmail_time=t, solar_system_id=system,
        region_id=region, sec_band="highsec", victim_ship_type_id=victim_ship,
        victim_corporation_id=victim_corp, victim_character_id=victim_char,
        total_value=Decimal(value), destroyed_value=Decimal(destroyed),
        involves_home_corp=True, home_corp_role=role,
    )


def _att(km, seq, char, corp, ship):
    KillmailParticipant.objects.create(
        killmail=km, role="attacker", seq=seq, character_id=char, corporation_id=corp,
        ship_type_id=ship,
    )


def _vic(km, char, corp, ship):
    KillmailParticipant.objects.create(
        killmail=km, role="victim", seq=0, character_id=char, corporation_id=corp,
        ship_type_id=ship,
    )


def _losses(count, *, first_id=100):
    """``count`` home-corp losses in Jita, none of them claimed (so all are estimated)."""
    base = timezone.now() - dt.timedelta(minutes=90)
    made = []
    for i in range(count):
        km = _km(first_id + i, base + dt.timedelta(minutes=i), victim_ship=GUARDIAN,
                 victim_corp=HOME_CORP, victim_char=OUR_A, value=250_000_000,
                 role=_VICTIM, destroyed=200_000_000)
        _vic(km, OUR_A, HOME_CORP, GUARDIAN)
        _att(km, 1, ENEMY_A, ENEMY_CORP, STABBER)
        made.append(km)
    return made


def _campaign(**kwargs):
    defaults = dict(
        name="Perf Campaign",
        start_time=timezone.now() - dt.timedelta(hours=2),
        end_time=timezone.now() + dt.timedelta(hours=1),
        scope={},
    )
    defaults.update(kwargs)
    return CombatCampaign.objects.create(**defaults)


def _table_queries(captured, table: str) -> list[str]:
    return [q["sql"] for q in captured.captured_queries if table in q["sql"]]


def _build_battle(title="Battle in Tama"):
    """The two-sided synthetic fight the KB-31 suite uses, rebuilt here standalone."""
    t0 = timezone.now() - dt.timedelta(minutes=30)
    t1 = timezone.now() - dt.timedelta(minutes=20)
    t2 = timezone.now() - dt.timedelta(minutes=10)

    km1 = _km(1, t0, victim_ship=STABBER, victim_corp=ENEMY_CORP, victim_char=ENEMY_A,
              value=100_000_000, role=_ATTACKER, system=TAMA, region=None)
    _vic(km1, ENEMY_A, ENEMY_CORP, STABBER)
    _att(km1, 1, OUR_A, HOME_CORP, RIFTER)
    _att(km1, 2, OUR_B, HOME_CORP, RIFTER)

    km2 = _km(2, t1, victim_ship=GUARDIAN, victim_corp=HOME_CORP, victim_char=OUR_A,
              value=250_000_000, role=_VICTIM, system=TAMA, region=None,
              destroyed=200_000_000)
    _vic(km2, OUR_A, HOME_CORP, GUARDIAN)
    _att(km2, 1, ENEMY_A, ENEMY_CORP, STABBER)
    _att(km2, 2, ENEMY_B, ENEMY_CORP, STABBER)

    km3 = _km(3, t2, victim_ship=RIFTER, victim_corp=ENEMY_CORP, victim_char=ENEMY_B,
              value=50_000_000, role=_ATTACKER, system=TAMA, region=None)
    _vic(km3, ENEMY_B, ENEMY_CORP, RIFTER)
    _att(km3, 1, OUR_B, HOME_CORP, RIFTER)

    report = BattleReport.objects.create(
        title=title, system_ids=[TAMA], start_time=t0, end_time=t2,
        sides={"corporations": []}, ship_breakdown={},
    )
    report.killmails.set([1, 2, 3])
    battle_sides.recompute_sides(report)
    return report


# --------------------------------------------------------------------------- #
#  P4 (remainder) — the SRP programme is resolved once per campaign, not per loss
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_srp_spend_resolves_the_programme_once_regardless_of_loss_count():
    """``active_program()`` is a per-campaign constant; N losses must not mean N lookups.

    ``eligibility`` still costs what it costs per mail (valuation, rule lookup) — that is
    another lane's problem. The claim pinned here is narrow and exact: the number of
    queries against the SRP programme table stays 1 whether the campaign covers one loss
    or six, which is only true if the programme is hoisted out of the loop.
    """
    _permissive_program()
    _losses(6)
    camp = _campaign(scope={"system_ids": [JITA]})

    with CaptureQueriesContext(connection) as captured:
        stats = combat_campaigns.campaign_stats(camp, use_cache=False)

    assert stats["srp"]["estimated"] == 6  # every loss took the estimate path
    program_queries = _table_queries(captured, _SRP_PROGRAM_TABLE)
    assert len(program_queries) == 1, (
        f"expected one SRP programme lookup for 6 losses, got {len(program_queries)}"
    )


@pytest.mark.django_db
def test_srp_spend_resolves_the_srp_rule_once_regardless_of_loss_count():
    """The rule lookup is per-doctrine, not per-loss, once the loop shares one batch.

    ``eligibility`` re-derived the active ``SrpRule`` for every loss it scored — one or
    two queries each, on a loop that runs over every in-scope loss of a campaign. The
    losses here all share one doctrine (in fact none of them match one), so a batch that
    memoises correctly turns N lookups into 1. Pinning it at "does not grow with the loss
    count" rather than an exact number keeps the test honest if the resolution order
    changes.
    """
    _permissive_program()
    _losses(6)
    camp = _campaign(scope={"system_ids": [JITA]})

    with CaptureQueriesContext(connection) as captured:
        stats = combat_campaigns.campaign_stats(camp, use_cache=False)

    assert stats["srp"]["estimated"] == 6
    rule_queries = _table_queries(captured, _SRP_RULE_TABLE)
    assert len(rule_queries) <= 2, (
        f"expected the SRP rule to be resolved once for 6 losses, got {len(rule_queries)} "
        "queries — the batch is not being shared across the loop"
    )


@pytest.mark.django_db
def test_srp_spend_total_is_unchanged_by_the_hoist():
    """The payout maths must be byte-identical to the per-loss lookup it replaced."""
    _permissive_program()
    _losses(3)
    camp = _campaign(scope={"system_ids": [JITA]})

    stats = combat_campaigns.campaign_stats(camp, use_cache=False)
    # ACTUAL_LOSS pays the destroyed value: 3 × 200M, all estimated, none claimed.
    assert stats["srp"]["spend"] == Decimal("600000000")
    assert stats["srp"]["losses"] == 3
    assert stats["srp"]["actual_claims"] == 0
    assert stats["srp"]["basis"] == "estimate"


@pytest.mark.django_db
def test_srp_spend_does_not_seed_a_programme_when_every_loss_is_claimed(django_user_model):
    """The hoist must stay LAZY: ``active_program()`` creates a default programme when
    none exists, so resolving it eagerly would seed an SRP programme row merely by
    rendering a campaign whose losses are all claimed — which the per-loss version,
    reached only from the estimate branch, never did."""
    from apps.srp.models import SrpClaim, SrpProgram

    (km,) = _losses(1)
    claimant = _user(django_user_model, rbac.ROLE_MEMBER, "-claim")
    SrpClaim.objects.create(
        killmail=km, claimant=claimant, status=SrpClaim.Status.APPROVED,
        computed_payout=Decimal("150000000"),
    )
    camp = _campaign(scope={"system_ids": [JITA]})

    stats = combat_campaigns.campaign_stats(camp, use_cache=False)
    assert stats["srp"]["basis"] == "actual"
    assert stats["srp"]["spend"] == Decimal("150000000")
    assert not SrpProgram.objects.exists()


# --------------------------------------------------------------------------- #
#  P4 (remainder) — the officer overlay is computed only when it will be rendered
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_overlays_false_skips_the_srp_walk_entirely():
    _permissive_program()
    _losses(4)
    camp = _campaign(scope={"system_ids": [JITA]})

    with CaptureQueriesContext(connection) as captured:
        core = combat_campaigns.campaign_stats(camp, use_cache=False, overlays=False)

    assert _table_queries(captured, _SRP_PROGRAM_TABLE) == []
    assert core["srp"] is None and core["compliance"] is None
    # …and the core scoreboard is exactly what the full payload reports.
    full = combat_campaigns.campaign_stats(camp, use_cache=False, overlays=True)
    for field in ("kills", "losses", "isk_destroyed", "isk_lost", "efficiency",
                  "participants", "killmail_count", "top_pilots", "top_pilots_by_main",
                  "top_ships", "doctrine_target_pct"):
        assert core[field] == full[field], field
    assert full["srp"] is not None and full["srp"]["spend"] == Decimal("800000000")


@pytest.mark.django_db
def test_campaign_list_cost_does_not_grow_with_the_srp_walk():
    """The member campaign LIST renders no overlay, so it must not pay for one — the
    finding was that it multiplied the per-loss SRP walk by the campaign count."""
    _permissive_program()
    _losses(4)
    for i in range(3):
        _campaign(name=f"Perf {i}", scope={"system_ids": [JITA]})

    with CaptureQueriesContext(connection) as captured:
        for camp in CombatCampaign.objects.all():
            combat_campaigns.campaign_stats(camp, use_cache=False, overlays=False)

    assert _table_queries(captured, _SRP_PROGRAM_TABLE) == []


@pytest.mark.django_db
def test_core_cache_warmed_without_overlays_still_yields_the_real_overlay():
    """The two fragments live under SEPARATE cache keys.

    The trap this guards: a non-officer view warms the campaign cache first, and a later
    officer view reads that entry back and renders ``srp = None`` — silently hiding a
    real, over-budget SRP figure from the person who needs it. The core entry is shared
    (that is the point); the overlay must still be computed on demand.
    """
    _permissive_program()
    _losses(2)
    camp = _campaign(scope={"system_ids": [JITA]}, srp_budget_isk=Decimal("100000000"))

    warmed = combat_campaigns.campaign_stats(camp, overlays=False)
    assert warmed["srp"] is None

    officer_view = combat_campaigns.campaign_stats(camp, overlays=True)
    assert officer_view["srp"]["spend"] == Decimal("400000000")
    assert officer_view["srp"]["over_budget"] is True
    assert officer_view["kills"] == warmed["kills"]

    # And the overlay-less caller still gets None afterwards — no cross-contamination
    # from the officer's cache entry back into the public payload.
    assert combat_campaigns.campaign_stats(camp, overlays=False)["srp"] is None


@pytest.mark.django_db
def test_anonymous_campaign_permalink_never_walks_srp(client, sde):
    """``combat_campaign_public`` has no decorator at all — an unauthenticated visitor
    used to pay the full officer SRP walk for a block the template cannot render."""
    _permissive_program()
    _losses(4)
    camp = _campaign(
        visibility=CombatCampaign.Visibility.PUBLIC, scope={"system_ids": [JITA]},
        srp_budget_isk=Decimal("100000000"),
    )

    with CaptureQueriesContext(connection) as captured:
        resp = client.get(f"/killboard/campaigns/r/{camp.slug}/")

    assert resp.status_code == 200
    assert b"SRP spend" not in resp.content
    assert _table_queries(captured, _SRP_PROGRAM_TABLE) == []


@pytest.mark.django_db
def test_officer_campaign_detail_still_renders_the_overlay(client, django_user_model, sde):
    """The other half of the gate: an officer must still get the real figures."""
    _permissive_program()
    _losses(2)
    camp = _campaign(scope={"system_ids": [JITA]}, srp_budget_isk=Decimal("100000000"))

    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-camp"))
    body = client.get(f"/killboard/campaigns/{camp.pk}/").content
    assert b"SRP spend" in body


# --------------------------------------------------------------------------- #
#  P7 — the derived battle blocks are cached; the officer overlays are not
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_public_battle_page_recomputes_derived_blocks_only_once(client, sde):
    """A repeat anonymous hit must not replay the raw row scan.

    ``KillmailItem`` is read by exactly one thing on this page — the per-side role
    composition — so "did we touch the item table?" is a precise probe for the derived
    blocks being recomputed, and it does not depend on any unrelated query count.
    """
    report = _build_battle()
    report.is_public = True
    report.save(update_fields=["is_public"])

    with CaptureQueriesContext(connection) as first:
        assert client.get(f"/killboard/battles/r/{report.slug}/").status_code == 200
    with CaptureQueriesContext(connection) as second:
        assert client.get(f"/killboard/battles/r/{report.slug}/").status_code == 200

    assert _table_queries(first, _ITEM_TABLE), "the first request must build the blocks"
    assert _table_queries(second, _ITEM_TABLE) == [], "the second must read the cache"
    assert len(second.captured_queries) < len(first.captured_queries)


@pytest.mark.django_db
def test_cached_battle_blocks_match_a_fresh_computation(client, sde):
    """The cached payload must equal what the uncached functions return, or the cache is
    trading correctness for speed — the timeline drives a rendered ISK-swing figure."""
    from apps.killboard.views import _battle_report_context

    report = _build_battle()
    home = report.detected_sides.get(index=0)
    side_of_entity = {
        (m.entity_type, m.entity_id): s.index
        for s in report.detected_sides.all() for m in s.members.all()
    }
    expected_timeline = battle_sides.battle_timeline(report, home)
    expected_roles = roles.battle_role_composition(report, side_of_entity)

    request = _anon_request()
    cold = _battle_report_context(request, report, public=True)
    warm = _battle_report_context(request, report, public=True)  # served from the cache

    for ctx in (cold, warm):
        assert ctx["timeline"]["rows"] == expected_timeline["rows"]
        assert ctx["timeline"]["final_swing"] == expected_timeline["final_swing"]
        assert ctx["timeline"]["polyline"] == expected_timeline["polyline"]
        assert ctx["swing_series"] == [float(r["swing"]) for r in expected_timeline["rows"]]
        by_index = {s["index"]: s["roles"] for s in ctx["side_views"]}
        for index, rows in expected_roles.items():
            assert [(r["role"], r["count"]) for r in by_index[index]] == [
                (r["role"], r["count"]) for r in rows
            ]
            # The translated label survives the round trip through the prose-free cache.
            assert all(str(r["label"]) for r in by_index[index])


@pytest.mark.django_db
def test_battle_cache_is_busted_by_a_side_recompute(client, sde):
    """The stamp must move when the sides move, or an officer reassignment would render
    stale role composition for the whole TTL."""
    from apps.killboard.views import _battle_report_context

    report = _build_battle()
    request = _anon_request()
    _battle_report_context(request, report, public=True)  # warm

    battle_sides.recompute_sides(report)
    report.refresh_from_db()
    with CaptureQueriesContext(connection) as captured:
        _battle_report_context(request, report, public=True)
    assert _table_queries(captured, _ITEM_TABLE), "recomputed sides must invalidate the cache"


@pytest.mark.django_db
def test_officer_battle_overlays_never_reach_an_anonymous_viewer(client, django_user_model, sde):
    """S4-class guard: the officer overlays must not share a cache key with the public
    payload, in EITHER direction.

    An officer warms the page first, then an anonymous caller fetches the public
    permalink: the anonymous body must carry no SRP liability. Then the reverse — the
    anonymous entry must not be able to suppress the officer's overlay.
    """
    _permissive_program()
    report = _build_battle()
    report.is_public = True
    report.save(update_fields=["is_public"])

    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-battle"))
    officer_first = client.get(f"/killboard/battles/{report.pk}/").content
    assert b"SRP liability" in officer_first

    client.logout()
    anon_body = client.get(f"/killboard/battles/r/{report.slug}/").content
    assert b"SRP liability" not in anon_body

    # Reverse order, from a cold cache: anonymous first, officer second.
    cache.clear()
    anon_first = client.get(f"/killboard/battles/r/{report.slug}/").content
    assert b"SRP liability" not in anon_first
    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER, "-battle"))
    officer_second = client.get(f"/killboard/battles/{report.pk}/").content
    assert b"SRP liability" in officer_second


@pytest.mark.django_db
def test_battle_srp_liability_resolves_the_programme_once():
    """The same P4 hoist applies to the battle overlay's per-loss eligibility loop."""
    _permissive_program()
    report = _build_battle()
    # Two more home losses inside the report so the loop runs more than once.
    for km in _losses(2, first_id=200):
        report.killmails.add(km)
    home = report.detected_sides.get(index=0)

    with CaptureQueriesContext(connection) as captured:
        liability = battle_sides.srp_liability(report, home)

    assert liability["losses"] == 3 and liability["eligible"] == 3
    assert len(_table_queries(captured, _SRP_PROGRAM_TABLE)) == 1


def _anon_request():
    """A bare anonymous request suitable for ``_battle_report_context``."""
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory

    request = RequestFactory().get("/killboard/battles/r/x/")
    request.user = AnonymousUser()
    return request


# --------------------------------------------------------------------------- #
#  P9 — watchlist intel is bounded in SQL, not in Python
# --------------------------------------------------------------------------- #
def _watched_entity(kill_count: int, entity_id: int = 500):
    """``kill_count`` mails the watched corp attacked on, plus one it was the victim of."""
    now = timezone.now()
    for i in range(kill_count):
        km = _km(1000 + i, now - dt.timedelta(minutes=i), victim_ship=RIFTER,
                 victim_corp=ENEMY_CORP, victim_char=ENEMY_A, value=1_000_000,
                 role=_ATTACKER)
        _vic(km, ENEMY_A, ENEMY_CORP, RIFTER)
        _att(km, 1, OUR_A, entity_id, RIFTER)
    loss = _km(2000, now - dt.timedelta(hours=1), victim_ship=GUARDIAN,
               victim_corp=entity_id, victim_char=OUR_B, value=9_000_000, role=_VICTIM)
    _vic(loss, OUR_B, entity_id, GUARDIAN)

    wl = Watchlist.objects.create(name="Targets")
    return WatchlistEntry.objects.create(
        watchlist=wl, entity_type=WatchlistEntry.EntityType.CORPORATION,
        entity_id=entity_id,
    )


@pytest.mark.django_db
def test_entry_activity_query_cost_is_independent_of_history_size(
    django_assert_num_queries,
):
    """Two queries, whatever the entity's history — and no id list crossing the wire.

    Comparing the full SQL text between a small and a large history is the direct proof
    that the killmail ids are no longer being pulled into Python and re-bound as an
    ``IN (…)`` list: an inlined list would make the second statement visibly longer.
    """
    entry = _watched_entity(3)
    with django_assert_num_queries(2):
        with CaptureQueriesContext(connection) as small:
            entry_activity(entry, limit=5)

    Watchlist.objects.all().delete()
    Killmail.objects.all().delete()
    entry = _watched_entity(40)
    with django_assert_num_queries(2):
        with CaptureQueriesContext(connection) as large:
            entry_activity(entry, limit=5)

    small_sql = [q["sql"] for q in small.captured_queries]
    large_sql = [q["sql"] for q in large.captured_queries]
    assert [len(s) for s in small_sql] == [len(s) for s in large_sql], (
        "the SQL text grew with the history — an id list is still being inlined"
    )


@pytest.mark.django_db
def test_entry_activity_results_are_unchanged():
    """Hand-derived expectation: 40 kills + 1 loss = 41 distinct mails, newest 5 returned."""
    entry = _watched_entity(40)
    activity = entry_activity(entry, limit=5)

    assert activity["kills"] == 40
    assert activity["losses"] == 1
    assert activity["total"] == 41
    assert len(activity["killmails"]) == 5
    # Newest first: the loss is an hour old, so the five most recent are attack mails.
    assert [k.killmail_id for k in activity["killmails"]] == [1000, 1001, 1002, 1003, 1004]


@pytest.mark.django_db
def test_entry_activity_counts_a_multi_pilot_mail_once():
    """``total``/``kills`` count DISTINCT mails, not participant rows — a five-pilot gang
    on one mail is one appearance, exactly as the old ``len(distinct id list)`` was."""
    now = timezone.now()
    km = _km(3000, now, victim_ship=RIFTER, victim_corp=ENEMY_CORP, victim_char=ENEMY_A,
             value=1_000_000, role=_ATTACKER)
    _vic(km, ENEMY_A, ENEMY_CORP, RIFTER)
    for seq in range(1, 6):
        _att(km, seq, OUR_A + seq, 777, RIFTER)

    wl = Watchlist.objects.create(name="Gang")
    entry = WatchlistEntry.objects.create(
        watchlist=wl, entity_type=WatchlistEntry.EntityType.CORPORATION, entity_id=777
    )
    activity = entry_activity(entry)
    assert activity["kills"] == 1 and activity["losses"] == 0 and activity["total"] == 1
    assert [k.killmail_id for k in activity["killmails"]] == [3000]


@pytest.mark.django_db
def test_watchlist_overview_cost_scales_with_entries_not_with_history(
    django_assert_num_queries,
):
    """The overview loops per entry; each entry must stay a fixed two queries even when
    it watches a busy alliance, which is what pushed the page over before."""
    entry = _watched_entity(40)
    watchlist = entry.watchlist
    WatchlistEntry.objects.create(
        watchlist=watchlist, entity_type=WatchlistEntry.EntityType.CORPORATION,
        entity_id=ENEMY_CORP,
    )
    # 1 query for the entry list + 2 per entry.
    with django_assert_num_queries(1 + 2 * 2):
        rows = watchlist_overview(watchlist, per_entry=5)

    assert len(rows) == 2
    watched = next(r for r in rows if r["entry"].entity_id == 500)
    assert watched["total"] == 41 and len(watched["killmails"]) == 5
