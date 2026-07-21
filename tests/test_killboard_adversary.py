"""KB-33 adversary entity pages (WS-C3) — profile maths, gating, link-swap, watchlist.

Every figure an adversary profile shows is computed from OUR killmail history ONLY — the
board never ingests anything that doesn't touch the home corp, so an external entity's record
in our DB *is* the record of its fights with us. The synthetic history below (home corp
98000001 in test settings) is hand-derived so the aggregation is pinned:

  * km1  we LOSE OUR_A's Guardian in Jita/The Forge (250M) — our loss.
         Attackers: ENEMY_A (Stabber, ENEMY_CORP/ALLIANCE), ENEMY_B (Vexor, ENEMY_CORP),
                    ALLY_C (Rifter, ALLY_CORP).
  * km2  we KILL ENEMY_A's Stabber in Jita/The Forge (100M) — our kill.
         Attackers: OUR_A, OUR_B (both HOME_CORP).
  * km3  we LOSE OUR_B's Rifter in Tama/lowsec region (50M) — our loss.
         Attacker: ENEMY_A (Stabber, ENEMY_CORP).

So vs ENEMY_CORP: they killed us on km1+km3 (kills_vs_us=2, ISK we lost 300M) and we killed
them on km2 (losses_to_us=1, ISK we took 100M). danger_rating(2,1) → ratio 2/3 → "Risky".
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.killboard import adversary
from apps.killboard.models import Killmail, KillmailParticipant, Watchlist, WatchlistEntry
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role

HOME_CORP, ENEMY_CORP, ALLY_CORP = 98000001, 98000002, 98000003
ENEMY_ALLIANCE = 99000001
OUR_A, OUR_B = 95000001, 95000002
ENEMY_A, ENEMY_B, ALLY_C = 97000001, 97000002, 97000003
GUARDIAN, STABBER, RIFTER, VEXOR = 11987, 622, 587, 626
JITA, TAMA = 30000142, 30002813
THE_FORGE, THE_CITADEL = 10000002, 10000033

_VICTIM = Killmail.HomeRole.VICTIM
_ATTACKER = Killmail.HomeRole.ATTACKER


# --------------------------------------------------------------------------- #
#  Fixtures / helpers
# --------------------------------------------------------------------------- #
def _user(django_user_model, role=None, suffix=""):
    user, _ = django_user_model.objects.get_or_create(username=f"kb-c3-{role or 'none'}{suffix}")
    if role:
        RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


def _km(kid, t, *, victim_ship, victim_corp, victim_char, value, role, system, region,
        sec_band="highsec", victim_alliance=None):
    return Killmail.objects.create(
        killmail_id=kid, killmail_hash=f"h{kid}", killmail_time=t, solar_system_id=system,
        region_id=region, sec_band=sec_band, victim_ship_type_id=victim_ship,
        victim_corporation_id=victim_corp, victim_character_id=victim_char,
        victim_alliance_id=victim_alliance, total_value=Decimal(value),
        involves_home_corp=True, home_corp_role=role,
    )


def _att(km, seq, char, corp, ship, alliance=None):
    KillmailParticipant.objects.create(
        killmail=km, role="attacker", seq=seq, character_id=char, corporation_id=corp,
        ship_type_id=ship, alliance_id=alliance,
    )


def _vic(km, char, corp, ship, alliance=None):
    KillmailParticipant.objects.create(
        killmail=km, role="victim", seq=0, character_id=char, corporation_id=corp,
        ship_type_id=ship, alliance_id=alliance,
    )


def _build_history():
    """The three-mail synthetic set with fixed EVE-time buckets for the heatmap test.

    km1 at a Monday 14:00 UTC, km3 at the same slot (so that bucket holds 2), km2 elsewhere.
    """
    # A known Monday (2026-07-20 is a Monday) at 14:00 and 09:00 UTC.
    mon_14 = dt.datetime(2026, 7, 20, 14, 0, tzinfo=dt.UTC)
    mon_09 = dt.datetime(2026, 7, 20, 9, 0, tzinfo=dt.UTC)

    km1 = _km(1, mon_14, victim_ship=GUARDIAN, victim_corp=HOME_CORP, victim_char=OUR_A,
              value=250_000_000, role=_VICTIM, system=JITA, region=THE_FORGE)
    _vic(km1, OUR_A, HOME_CORP, GUARDIAN)
    _att(km1, 1, ENEMY_A, ENEMY_CORP, STABBER, alliance=ENEMY_ALLIANCE)
    _att(km1, 2, ENEMY_B, ENEMY_CORP, VEXOR, alliance=ENEMY_ALLIANCE)
    _att(km1, 3, ALLY_C, ALLY_CORP, RIFTER)

    km2 = _km(2, mon_09, victim_ship=STABBER, victim_corp=ENEMY_CORP, victim_char=ENEMY_A,
              victim_alliance=ENEMY_ALLIANCE, value=100_000_000, role=_ATTACKER,
              system=JITA, region=THE_FORGE)
    _vic(km2, ENEMY_A, ENEMY_CORP, STABBER, alliance=ENEMY_ALLIANCE)
    _att(km2, 1, OUR_A, HOME_CORP, RIFTER)
    _att(km2, 2, OUR_B, HOME_CORP, RIFTER)

    km3 = _km(3, mon_14, victim_ship=RIFTER, victim_corp=HOME_CORP, victim_char=OUR_B,
              value=50_000_000, role=_VICTIM, system=TAMA, region=THE_CITADEL, sec_band="lowsec")
    _vic(km3, OUR_B, HOME_CORP, RIFTER)
    _att(km3, 1, ENEMY_A, ENEMY_CORP, STABBER, alliance=ENEMY_ALLIANCE)
    return km1, km2, km3


# --------------------------------------------------------------------------- #
#  Service maths — corporation profile
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_corp_summary_counts_isk_directions():
    _build_history()
    s = adversary.adversary_profile("corporation", ENEMY_CORP, use_cache=False)["summary"]
    # They killed us on km1 + km3; we killed them (ENEMY_A) on km2.
    assert s["kills_vs_us"] == 2
    assert s["losses_to_us"] == 1
    assert s["isk_we_lost"] == 250_000_000 + 50_000_000
    assert s["isk_we_took"] == 100_000_000
    assert s["engagements"] == 3


@pytest.mark.django_db
def test_danger_rating_matches_documented_derivation():
    """danger-to-us reuses leaderboards.danger_rating with their-vs-us tallies verbatim."""
    from apps.killboard.leaderboards import danger_rating

    _build_history()
    s = adversary.adversary_profile("corporation", ENEMY_CORP, use_cache=False)["summary"]
    # kills=2 (they beat us), losses=1 (we beat them): ratio 0.667 ≥ 0.5 → "Risky".
    expected = danger_rating(kills=2, losses=1)
    assert s["danger"]["label"] == expected["label"]
    assert s["danger"]["label"] == "Risky"
    assert s["danger"]["ratio"] == pytest.approx(2 / 3)


@pytest.mark.django_db
def test_top_hulls_are_what_they_bring_on_our_losses():
    _build_history()
    hulls = adversary.adversary_profile("corporation", ENEMY_CORP, use_cache=False)["hulls"]
    counts = {h["ship_type_id"]: h["count"] for h in hulls}
    # Stabber on km1 + km3 = 2 (participation count); Vexor on km1 = 1. Rifter is ALLY_CORP's.
    assert counts[STABBER] == 2
    assert counts[VEXOR] == 1
    assert RIFTER not in counts
    assert hulls[0]["ship_type_id"] == STABBER  # ranked most-brought first


@pytest.mark.django_db
def test_systems_and_regions_span_both_engagement_directions():
    _build_history()
    prof = adversary.adversary_profile("corporation", ENEMY_CORP, use_cache=False)
    systems = {s["system_id"]: s["count"] for s in prof["systems"]}
    regions = {r["region_id"]: r["count"] for r in prof["regions"]}
    # km1 + km2 in Jita, km3 in Tama.
    assert systems[JITA] == 2 and systems[TAMA] == 1
    assert regions[THE_FORGE] == 2 and regions[THE_CITADEL] == 1


@pytest.mark.django_db
def test_co_attackers_exclude_home_and_self():
    _build_history()
    co = adversary.adversary_profile("corporation", ENEMY_CORP, use_cache=False)["co_attackers"]
    corp_ids = {c["entity_id"] for c in co["corporations"]}
    char_ids = {c["entity_id"] for c in co["characters"]}
    # On our losses ENEMY_CORP was on (km1, km3): the only external ally is ALLY_CORP / ALLY_C.
    assert corp_ids == {ALLY_CORP}
    assert char_ids == {ALLY_C}
    # Never the home corp, never ENEMY_CORP's own pilots.
    assert HOME_CORP not in corp_ids and ENEMY_CORP not in corp_ids
    assert ENEMY_A not in char_ids and ENEMY_B not in char_ids


@pytest.mark.django_db
def test_corp_page_aggregates_its_pilots():
    _build_history()
    pilots = adversary.adversary_profile("corporation", ENEMY_CORP, use_cache=False)["pilots"]
    by_id = {p["character_id"]: p["count"] for p in pilots}
    # ENEMY_A: attacker on km1+km3 (2) + victim on km2 (1) = 3; ENEMY_B: attacker km1 = 1.
    assert by_id[ENEMY_A] == 3
    assert by_id[ENEMY_B] == 1
    assert pilots[0]["character_id"] == ENEMY_A  # most-seen first


@pytest.mark.django_db
def test_heatmap_bucket_placement():
    _build_history()
    hm = adversary.adversary_profile("corporation", ENEMY_CORP, use_cache=False)["heatmap"]
    # cells[day][hour]; ISO Monday = index 0. km1 + km3 both land Monday 14:00 → that cell = 2.
    monday = hm["cells"][0]["hours"]
    assert monday[14]["n"] == 2   # km1 + km3
    assert monday[9]["n"] == 1    # km2
    assert hm["total"] == 3
    assert hm["peak"] == 2


# --------------------------------------------------------------------------- #
#  Service maths — character profile
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_character_profile_counts_and_no_pilots_section():
    _build_history()
    prof = adversary.adversary_profile("character", ENEMY_A, use_cache=False)
    s = prof["summary"]
    # ENEMY_A attacked us on km1 + km3, died to us on km2.
    assert s["kills_vs_us"] == 2 and s["losses_to_us"] == 1
    assert prof["pilots"] == []  # a character is one pilot — no roster section
    co = prof["co_attackers"]
    # On km1 ENEMY_A flew with ENEMY_B (ENEMY_CORP) and ALLY_C (ALLY_CORP); km3 was solo.
    assert {c["entity_id"] for c in co["characters"]} == {ENEMY_B, ALLY_C}
    assert {c["entity_id"] for c in co["corporations"]} == {ENEMY_CORP, ALLY_CORP}


@pytest.mark.django_db
def test_alliance_profile_rolls_up_the_alliance():
    _build_history()
    s = adversary.adversary_profile("alliance", ENEMY_ALLIANCE, use_cache=False)["summary"]
    # ENEMY_ALLIANCE killed us on km1 + km3 (its pilots attacked); we killed it on km2.
    assert s["kills_vs_us"] == 2 and s["losses_to_us"] == 1


# --------------------------------------------------------------------------- #
#  Empty state — 200, honest, not 404
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_empty_profile_has_no_history():
    _build_history()
    prof = adversary.adversary_profile("character", 91234567, use_cache=False)
    assert prof["has_history"] is False
    assert prof["summary"]["engagements"] == 0
    assert prof["summary"]["danger"]["label"] == "Untested"


@pytest.mark.django_db
def test_empty_state_page_is_200_not_404(client, django_user_model, sde):
    _build_history()
    client.force_login(_member(django_user_model, "empty", 40001))
    resp = client.get(reverse("killboard:adversary_character", args=[91234567]))
    assert resp.status_code == 200
    assert b"No engagements with us" in resp.content


# --------------------------------------------------------------------------- #
#  Member gating
# --------------------------------------------------------------------------- #
def _member(django_user_model, username, cid, role="member"):
    u = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    EveCharacter.objects.create(character_id=cid, user=u, name=username,
                                is_main=True, is_corp_member=True)
    return u


@pytest.mark.django_db
def test_adversary_page_denied_to_anonymous(client, sde):
    _build_history()
    resp = client.get(reverse("killboard:adversary_corporation", args=[ENEMY_CORP]))
    assert resp.status_code in (302, 403)  # login_required → redirect


@pytest.mark.django_db
def test_adversary_page_denied_to_non_member(client, django_user_model, sde):
    _build_history()
    # Logged in but with no corp/alliance standing.
    client.force_login(_user(django_user_model, role=None, suffix="-x"))
    resp = client.get(reverse("killboard:adversary_corporation", args=[ENEMY_CORP]))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_member_sees_corp_profile(client, django_user_model, sde):
    _build_history()
    client.force_login(_member(django_user_model, "viewer", 40002))
    resp = client.get(reverse("killboard:adversary_corporation", args=[ENEMY_CORP]))
    assert resp.status_code == 200
    body = resp.content
    assert b"kills vs us" in body and b"Hulls they bring against us" in body
    assert b"Risky" in body  # the danger badge


# --------------------------------------------------------------------------- #
#  Entry-point link swap on killmail detail
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_detail_link_swap_home_vs_adversary(client, django_user_model, sde):
    """On a loss detail: our victim → the pilot page; the enemy attacker/corp → adversary
    pages; the home pilot never gets an adversary link (the link-swap contract)."""
    _build_history()
    client.force_login(_member(django_user_model, "det", 40003))
    html = client.get(reverse("killboard:detail", args=[1])).content

    # Our own victim pilot → internal pilot analytics, NOT an adversary link.
    assert reverse("killboard:pilot", args=[OUR_A]).encode() in html
    assert reverse("killboard:adversary_character", args=[OUR_A]).encode() not in html
    # The enemy attacker and their corp → adversary pages.
    assert reverse("killboard:adversary_character", args=[ENEMY_A]).encode() in html
    assert reverse("killboard:adversary_corporation", args=[ENEMY_CORP]).encode() in html
    # The home corp itself is "us" — never adversary-linked.
    assert reverse("killboard:adversary_corporation", args=[HOME_CORP]).encode() not in html


@pytest.mark.django_db
def test_detail_no_intel_links_for_anonymous(client, sde):
    """The public detail page keeps plain names for anonymous viewers — no intel links."""
    _build_history()
    html = client.get(reverse("killboard:detail", args=[1])).content
    assert reverse("killboard:adversary_character", args=[ENEMY_A]).encode() not in html
    assert reverse("killboard:pilot", args=[OUR_A]).encode() not in html


# --------------------------------------------------------------------------- #
#  Add-to-watchlist integration
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_officer_add_to_watchlist_and_watched_state(client, django_user_model, sde):
    _build_history()
    wl = Watchlist.objects.create(name="Hostiles")
    officer = _member(django_user_model, "officer", 40004, role="officer")
    client.force_login(officer)

    # POST adds the entity to the chosen watchlist and returns to the adversary page.
    resp = client.post(
        reverse("killboard:adversary_watch", args=["corporation", ENEMY_CORP]),
        {"watchlist_id": wl.id},
    )
    assert resp.status_code == 302
    assert resp.url == reverse("killboard:adversary_corporation", args=[ENEMY_CORP])
    assert WatchlistEntry.objects.filter(
        watchlist=wl, entity_type="corporation", entity_id=ENEMY_CORP
    ).exists()

    # The page now renders the "watched" state.
    body = client.get(reverse("killboard:adversary_corporation", args=[ENEMY_CORP])).content
    assert b"Watched" in body


@pytest.mark.django_db
def test_add_to_watchlist_is_officer_gated(client, django_user_model, sde):
    _build_history()
    Watchlist.objects.create(name="Hostiles")
    client.force_login(_member(django_user_model, "plain", 40005, role="member"))
    resp = client.post(
        reverse("killboard:adversary_watch", args=["corporation", ENEMY_CORP]),
        {"watchlist_id": 1},
    )
    assert resp.status_code in (302, 403)
    assert not WatchlistEntry.objects.filter(entity_id=ENEMY_CORP).exists()


# --------------------------------------------------------------------------- #
#  Query budget — a profile build stays bounded (short-TTL cache in production)
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_profile_build_query_budget(django_assert_max_num_queries):
    """A full corp profile build (the heaviest kind) is a bounded set of indexed queries.

    The view caches this per entity for a short TTL, so a warm page pays almost nothing; a
    cold build must still stay sane. 15 is the documented ceiling for a profile with data.
    """
    _build_history()
    with django_assert_max_num_queries(15):
        prof = adversary.adversary_profile("corporation", ENEMY_CORP, use_cache=False)
        # Force evaluation of the lazily-built lists that back the page.
        assert prof["hulls"] and prof["systems"] and prof["pilots"]
