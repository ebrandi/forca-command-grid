"""KB-23 — main+alt rollup for the killboard rankings."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.killboard.aggregation import historical_leaderboards
from apps.killboard.leaderboards import _rollup_by_main, leaderboards
from apps.killboard.models import Killmail, KillmailParticipant, MonthlyPilotKillStat
from core.pilots import mains_for

HOME = 98000001
ENEMY = 55555


def _kill(km_id, *, attackers, value="100000000"):
    """A home-corp KILL (home on the attacker side) with the given attacker tuples."""
    km = Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}", killmail_time=timezone.now(),
        solar_system_id=30000142, victim_ship_type_id=587,
        total_value=Decimal(value), points=10, is_solo=False, is_npc=False,
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.ATTACKER,
        victim_character_id=9999, victim_corporation_id=ENEMY,
    )
    for i, (char, corp, fb) in enumerate(attackers, start=1):
        KillmailParticipant.objects.create(
            killmail=km, role="attacker", seq=i, character_id=char,
            corporation_id=corp, ship_type_id=587, final_blow=fb, damage_done=100,
        )
    return km


def _linked(django_user_model, username, main_id, alt_ids=()):
    from apps.sso.models import EveCharacter

    u = django_user_model.objects.create(username=username)
    EveCharacter.objects.create(character_id=main_id, user=u, name=f"{username}-main",
                                is_main=True, is_corp_member=True)
    for aid in alt_ids:
        EveCharacter.objects.create(character_id=aid, user=u, name=f"{username}-alt{aid}",
                                    is_main=False, is_corp_member=True)
    return u


def _board(data, key):
    for cat in data["categories"]:
        if cat["key"] == key:
            return cat["rows"]
    return []


# --- mains_for ----------------------------------------------------------------
def test_mains_for_empty():
    assert mains_for([]) == {}


@pytest.mark.django_db
def test_mains_for_maps_alts_and_unlinked(django_user_model):
    _linked(django_user_model, "u1", 1001, alt_ids=[1003, 1004])
    # 2001 has no EveCharacter row (external/enemy pilot) -> maps to itself
    assert mains_for([1001, 1003, 1004, 2001]) == {1001: 1001, 1003: 1001, 1004: 1001, 2001: 2001}


@pytest.mark.django_db
def test_mains_for_no_main_falls_back_to_self(django_user_model):
    from apps.sso.models import EveCharacter

    u = django_user_model.objects.create(username="nomain")
    EveCharacter.objects.create(character_id=3001, user=u, name="x",
                                is_main=False, is_corp_member=True)
    assert mains_for([3001]) == {3001: 3001}


# --- _rollup_by_main ----------------------------------------------------------
@pytest.mark.django_db
def test_rollup_by_main_sums_rekeys_and_recomputes(django_user_model):
    _linked(django_user_model, "u1", 1001, alt_ids=[1003])
    pilots = [
        {"character_id": 1001, "kills": 2, "final_blows": 1, "solo_kills": 0,
         "isk_destroyed": 100, "points": 20, "losses": 1, "isk_lost": 50,
         "active_days": 2, "engagements": 3, "efficiency": 0.0},
        {"character_id": 1003, "kills": 1, "final_blows": 0, "solo_kills": 1,
         "isk_destroyed": 100, "points": 10, "losses": 0, "isk_lost": 0,
         "active_days": 1, "engagements": 1, "efficiency": 0.0},
        {"character_id": 2001, "kills": 5, "final_blows": 2, "solo_kills": 0,
         "isk_destroyed": 300, "points": 50, "losses": 0, "isk_lost": 0,
         "active_days": 3, "engagements": 5, "efficiency": 0.0},
    ]
    out = {p["character_id"]: p for p in _rollup_by_main(pilots)}

    assert set(out) == {1001, 2001}  # alt folded into main, unlinked stays
    m = out[1001]
    assert (m["kills"], m["final_blows"], m["solo_kills"]) == (3, 1, 1)
    assert m["isk_destroyed"] == 200 and m["losses"] == 1 and m["active_days"] == 3
    assert m["engagements"] == 4                      # 3 kills + 1 loss, recomputed
    assert round(m["efficiency"], 1) == 80.0          # 200/(200+50)*100
    assert out[2001]["kills"] == 5                     # unlinked passes through unchanged


# --- live leaderboards --------------------------------------------------------
@pytest.mark.django_db
def test_leaderboards_by_main_collapses_alts(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME
    _linked(django_user_model, "u1", 1001, alt_ids=[1003])
    _kill(1, attackers=[(1001, HOME, True)])
    _kill(2, attackers=[(1001, HOME, True)])
    _kill(3, attackers=[(1003, HOME, True)])   # the alt's kill
    _kill(4, attackers=[(2001, HOME, True)])   # an unlinked pilot's kill

    by_char = _board(leaderboards("all", use_cache=False), "top_killers")
    assert 1003 in {r["character_id"] for r in by_char}  # alt ranks on its own

    by_main = _board(leaderboards("all", use_cache=False, by_main=True), "top_killers")
    vals = {r["character_id"]: r["value"] for r in by_main}
    assert 1003 not in vals          # alt folded into the main
    assert vals[1001] == 3           # 2 (main) + 1 (alt) kills under the main
    assert vals[2001] == 1           # unlinked pilot unchanged


@pytest.mark.django_db
def test_leaderboards_by_main_cache_key_is_separate(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME
    _linked(django_user_model, "u1", 1001, alt_ids=[1003])
    _kill(1, attackers=[(1001, HOME, True)])
    _kill(2, attackers=[(1003, HOME, True)])

    # Cached variants must not collide: by-main has 1 row (the main), by-char has 2.
    char_ids_char = {r["character_id"] for r in _board(leaderboards("all"), "top_killers")}
    char_ids_main = {r["character_id"] for r in _board(leaderboards("all", by_main=True), "top_killers")}
    assert char_ids_char == {1001, 1003}
    assert char_ids_main == {1001}


# --- historical ---------------------------------------------------------------
@pytest.mark.django_db
def test_historical_by_main(django_user_model, settings):
    settings.FORCA_HOME_CORP_ID = HOME
    _linked(django_user_model, "u1", 1001, alt_ids=[1003])
    for cid, kills, isk in [(1001, 2, "100"), (1003, 1, "50"), (2001, 1, "30")]:
        MonthlyPilotKillStat.objects.create(
            character_id=cid, year=2026, month=6, kills=kills, losses=0, solo_kills=0,
            final_blows=0, isk_destroyed=Decimal(isk), isk_lost=Decimal("0"),
            points=0, active_days=1,
        )

    by_main = _board(historical_leaderboards(2026, 6, use_cache=False, by_main=True), "top_killers")
    vals = {r["character_id"]: r["value"] for r in by_main}
    assert 1003 not in vals
    assert vals[1001] == 3 and vals[2001] == 1


# --- view ---------------------------------------------------------------------
@pytest.mark.django_db
def test_rankings_view_by_main_toggle(client, django_user_model, settings, sde):
    settings.FORCA_HOME_CORP_ID = HOME
    _linked(django_user_model, "u1", 1001, alt_ids=[1003])
    _kill(1, attackers=[(1001, HOME, True)])
    _kill(2, attackers=[(1003, HOME, True)])

    resp = client.get("/killboard/rankings/?window=all&by=main")
    assert resp.status_code == 200
    assert b"By main" in resp.content            # the toggle renders
    assert b"alts counted under" in resp.content  # active-mode hint
