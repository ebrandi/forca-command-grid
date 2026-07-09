"""REC-KB-2 (roadmap 3.8) — combat evidence from FORCA's own killboard in recruit vetting.

Three signals from local (public) killmail data: we killed them, they fought alongside us, or
they fought against us. Read-only; keyed by character id.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.killboard.models import Killmail, KillmailParticipant
from apps.recruitment.services import home_killboard_evidence

pytestmark = pytest.mark.django_db
CAND = 77777
HOME_PILOT = 101
ENEMY = 55555
HOME = 98000001


def _km(km_id, *, role, victim_char=None, when=None):
    return Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}",
        killmail_time=when or timezone.now(), solar_system_id=30000142, region_id=10000002,
        victim_ship_type_id=587, total_value=Decimal("1000000"), points=1,
        is_solo=False, is_npc=False, involves_home_corp=True, home_corp_role=role,
        victim_character_id=victim_char, sec_band="nullsec",
        victim_corporation_id=HOME if role == Killmail.HomeRole.VICTIM else ENEMY,
    )


def _attacker(km, character_id, corp=ENEMY):
    return KillmailParticipant.objects.create(
        killmail=km, role="attacker", seq=1, character_id=character_id,
        corporation_id=corp, ship_type_id=22456, final_blow=True, damage_done=100)


def test_no_evidence_returns_none():
    assert home_killboard_evidence(CAND) is None
    assert home_killboard_evidence(0) is None


def test_killed_by_us():
    _km(1, role=Killmail.HomeRole.ATTACKER, victim_char=CAND)  # we killed the candidate
    ev = home_killboard_evidence(CAND)
    assert ev["killed_by_us"] == 1
    assert ev["fought_against"] == 0 and ev["fought_with"] == 0
    assert ev["is_hostile"] is False and ev["is_friendly"] is False


def test_fought_with_us():
    km = _km(2, role=Killmail.HomeRole.ATTACKER, victim_char=ENEMY)
    _attacker(km, CAND)  # candidate co-attacked on a kill we made
    ev = home_killboard_evidence(CAND)
    assert ev["fought_with"] == 1 and ev["is_friendly"] is True and ev["is_hostile"] is False


def test_fought_against_us():
    km = _km(3, role=Killmail.HomeRole.VICTIM, victim_char=HOME_PILOT)  # a home pilot died
    _attacker(km, CAND)  # candidate was an attacker → they killed one of ours
    ev = home_killboard_evidence(CAND)
    assert ev["fought_against"] == 1 and ev["is_hostile"] is True and ev["is_friendly"] is False


def test_hostile_wins_over_friendly():
    km_with = _km(4, role=Killmail.HomeRole.ATTACKER, victim_char=ENEMY)
    _attacker(km_with, CAND)
    km_against = _km(5, role=Killmail.HomeRole.VICTIM, victim_char=HOME_PILOT)
    _attacker(km_against, CAND)
    ev = home_killboard_evidence(CAND)
    assert ev["fought_with"] == 1 and ev["fought_against"] == 1
    assert ev["is_hostile"] is True and ev["is_friendly"] is False  # any hostility flags it


def test_two_attacker_rows_on_one_killmail_count_once():
    # A pilot can hold two attacker rows (different seq) on one killmail — the Count(distinct)
    # must still count it as a single killmail, not two.
    km = _km(8, role=Killmail.HomeRole.ATTACKER, victim_char=ENEMY)
    _attacker(km, CAND)  # seq=1
    KillmailParticipant.objects.create(
        killmail=km, role="attacker", seq=2, character_id=CAND, corporation_id=ENEMY,
        ship_type_id=22456, final_blow=False, damage_done=50)
    ev = home_killboard_evidence(CAND)
    assert ev["fought_with"] == 1


def test_last_activity_is_most_recent():
    now = timezone.now()
    _km(6, role=Killmail.HomeRole.ATTACKER, victim_char=CAND, when=now - timedelta(days=10))
    km = _km(7, role=Killmail.HomeRole.VICTIM, victim_char=HOME_PILOT, when=now - timedelta(days=1))
    _attacker(km, CAND)
    ev = home_killboard_evidence(CAND)
    assert ev["killed_by_us"] == 1 and ev["fought_against"] == 1
    assert (now - ev["last_activity"]).days == 1  # the day-1 involvement, not the day-10 one
