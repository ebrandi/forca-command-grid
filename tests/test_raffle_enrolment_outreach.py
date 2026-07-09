"""RAF-3 (roadmap 3.9) — one-click enrolment outreach from the ineligible report.

Nudge ranked active-but-unenrolled pilots to enrol; no-spam (once per contest/pilot, honour
opt-outs, skip the now-enrolled and the un-linked), future-only, moves no ISK.
"""
from __future__ import annotations

import pytest

from apps.raffle.models import (
    RaffleEnrolmentOutreach,
    RaffleIneligibleActivity,
    RaffleOutreachOptOut,
)
from apps.raffle.services import opt_out_of_outreach, send_enrolment_outreach
from apps.sso.models import EveCharacter
from tests._raffle_utils import detached_character, enrol_pilot, make_contest, make_user

pytestmark = pytest.mark.django_db
_EMIT = "apps.pingboard.services.emit_broadcast"


@pytest.fixture(autouse=True)
def _capture(monkeypatch):
    calls: list = []
    monkeypatch.setattr(_EMIT, lambda **kw: calls.append(kw))
    return calls


def _ineligible(contest, cid, tickets, name="Nud"):
    RaffleIneligibleActivity.objects.create(
        contest=contest, character_id=cid, character_name=name,
        source_key="pvp", source_ref=f"km{cid}",
        reason=RaffleIneligibleActivity.Reason.NOT_ENROLLED, would_be_tickets=tickets)


def test_sends_to_linked_unenrolled(django_user_model, _capture):
    contest = make_contest()
    enrol_pilot(django_user_model, 700001, with_token=False)  # linked account, no live ESI
    _ineligible(contest, 700001, 5)
    res = send_enrolment_outreach(contest)
    assert res["sent"] == 1
    assert len(_capture) == 1
    assert _capture[0]["audience"]["kind"] == "user"
    assert RaffleEnrolmentOutreach.objects.filter(contest=contest, character_id=700001).exists()


def test_idempotent_second_run_no_resend(django_user_model, _capture):
    contest = make_contest()
    enrol_pilot(django_user_model, 700002, with_token=False)
    _ineligible(contest, 700002, 5)
    send_enrolment_outreach(contest)
    send_enrolment_outreach(contest)
    assert len(_capture) == 1  # once total


def test_skips_opted_out(django_user_model, _capture):
    contest = make_contest()
    enrol_pilot(django_user_model, 700003, with_token=False)
    _ineligible(contest, 700003, 5)
    RaffleOutreachOptOut.objects.create(character_id=700003)
    res = send_enrolment_outreach(contest)
    assert res["sent"] == 0 and _capture == []


def test_skips_no_linked_account(_capture):
    contest = make_contest()
    detached_character(700004, name="Ghost")
    _ineligible(contest, 700004, 9, name="Ghost")
    res = send_enrolment_outreach(contest)
    assert res["sent"] == 0 and _capture == []


def test_skips_now_enrolled(django_user_model, _capture):
    contest = make_contest()
    enrol_pilot(django_user_model, 700005, name="Enrolled")  # with_token=True → eligible now
    _ineligible(contest, 700005, 9, name="Enrolled")
    res = send_enrolment_outreach(contest)
    assert res["sent"] == 0  # a live re-check finds them eligible → skip


def test_disabled_event_no_send(django_user_model, _capture):
    from apps.pingboard import config as pb_config

    contest = make_contest()
    enrol_pilot(django_user_model, 700006, with_token=False)
    _ineligible(contest, 700006, 5)
    doc = pb_config.get("notifications")
    doc["events"] = {**(doc.get("events") or {}), "raffle.enrolment_outreach": {"enabled": False}}
    pb_config.set("notifications", doc)
    res = send_enrolment_outreach(contest)
    assert res.get("reason") == "event_disabled" and _capture == []


def test_opt_out_is_account_level(django_user_model, _capture):
    contest = make_contest()
    # A pilot (no live ESI) opted out on their main; an alt of the SAME account then flies
    # ineligible activity — the account-level opt-out must still cover the alt.
    user, _main = enrol_pilot(django_user_model, 700010, with_token=False)
    EveCharacter.objects.create(character_id=700011, user=user, name="Alt", is_corp_member=True)
    RaffleOutreachOptOut.objects.create(character_id=700010)  # opted out on the main
    _ineligible(contest, 700011, 7, name="Alt")              # activity on the alt
    res = send_enrolment_outreach(contest)
    assert res["sent"] == 0 and _capture == []


def test_opt_out_records_all_user_characters(django_user_model):
    user = make_user(django_user_model, "optout")
    EveCharacter.objects.create(character_id=700007, user=user, name="A", is_main=True,
                                is_corp_member=True)
    EveCharacter.objects.create(character_id=700008, user=user, name="B", is_corp_member=True)
    assert opt_out_of_outreach(user) == 2
    assert RaffleOutreachOptOut.objects.filter(character_id__in=[700007, 700008]).count() == 2
