"""PVP ticket source + engine: award maths, precedence, idempotency, the
ineligible-activity record for unenrolled attackers, and retroactive conversion.
"""
from __future__ import annotations

import pytest

from apps.raffle import engine
from apps.raffle.models import (
    RaffleIneligibleActivity,
    RaffleTicketLedgerEntry,
)
from tests._raffle_utils import (
    HOME_CORP,
    approved_total,
    enable_source_retroactive,
    enrol_pilot,
    home_kill,
    make_contest,
)


def _ledger_for(contest, character_id):
    return RaffleTicketLedgerEntry.objects.filter(contest=contest, character_id=character_id)


@pytest.mark.django_db
def test_solo_kill_awards_100(django_user_model):
    contest = make_contest()
    enrol_pilot(django_user_model, 1001)
    home_kill(5001, is_solo=True, attackers=[(1001, HOME_CORP, True)])

    result = engine.process_source(contest, "pvp")
    assert result.awarded_tickets == 100
    assert result.awarded_events == 1
    entry = _ledger_for(contest, 1001).get()
    assert entry.amount == 100
    assert entry.source_ref == "killmail:5001"
    assert entry.metadata["solo"] is True


@pytest.mark.django_db
def test_final_blow_awards_10_and_participation_awards_1(django_user_model):
    """A shared kill: the finisher earns 10, a plain participant earns 1."""
    contest = make_contest()
    enrol_pilot(django_user_model, 1001, username="finisher")
    enrol_pilot(django_user_model, 1002, username="participant")
    home_kill(5002, is_solo=False, attackers=[
        (1001, HOME_CORP, True),   # final blow
        (1002, HOME_CORP, False),  # participation
    ])

    engine.process_source(contest, "pvp")
    assert _ledger_for(contest, 1001).get().amount == 10
    assert _ledger_for(contest, 1002).get().amount == 1
    assert approved_total(contest) == 11


@pytest.mark.django_db
def test_solo_takes_precedence_over_final_blow(django_user_model):
    """Solo (100) wins over final-blow (10) even when the sole attacker has the
    final blow flag set — the spec's fixed precedence."""
    contest = make_contest()
    enrol_pilot(django_user_model, 1001)
    home_kill(5003, is_solo=True, attackers=[(1001, HOME_CORP, True)])

    engine.process_source(contest, "pvp")
    assert _ledger_for(contest, 1001).get().amount == 100


@pytest.mark.django_db
def test_reprocess_is_idempotent(django_user_model):
    """Re-running the sweep over the same killmail never double-awards."""
    contest = make_contest()
    enrol_pilot(django_user_model, 1001)
    home_kill(5004, is_solo=True, attackers=[(1001, HOME_CORP, True)])

    engine.process_source(contest, "pvp")
    count_after_first = RaffleTicketLedgerEntry.objects.filter(contest=contest).count()
    total_after_first = approved_total(contest)

    second = engine.process_source(contest, "pvp")
    assert second.awarded_tickets == 0
    assert RaffleTicketLedgerEntry.objects.filter(contest=contest).count() == count_after_first
    assert approved_total(contest) == total_after_first == 100


@pytest.mark.django_db
def test_unenrolled_attacker_records_ineligible_no_ledger(django_user_model):
    """On a shared kill the enrolled pilot earns; an unenrolled corp-mate on the
    same mail creates a RaffleIneligibleActivity and NO drawable ticket."""
    contest = make_contest()
    enrol_pilot(django_user_model, 1001)
    # 2002 has no FORCA account at all.
    home_kill(5005, is_solo=False, attackers=[
        (1001, HOME_CORP, True),   # enrolled → 10
        (2002, HOME_CORP, False),  # unenrolled → ineligible record
    ])

    engine.process_source(contest, "pvp")
    assert _ledger_for(contest, 1001).get().amount == 10
    assert not _ledger_for(contest, 2002).exists()

    inelig = RaffleIneligibleActivity.objects.get(contest=contest, character_id=2002)
    assert inelig.reason == RaffleIneligibleActivity.Reason.NOT_ENROLLED
    assert inelig.would_be_tickets == 1
    assert inelig.source_ref == "killmail:5005"


@pytest.mark.django_db
def test_corp_on_corp_kill_awards_nothing(django_user_model):
    """Killing our own side (victim in a corp we credit attackers for) earns no
    ticket AND is never recorded as ineligible activity — the whole mail is skipped.
    Guards against friendly fire / corp-on-corp farming regardless of how the mail's
    home_corp_role was tagged or whether exclude_blue is on."""
    contest = make_contest()
    enrol_pilot(django_user_model, 1001)
    # Victim belongs to the home corp → same-side kill.
    home_kill(5020, is_solo=True, attackers=[(1001, HOME_CORP, True)],
              victim_corp=HOME_CORP, victim_char=2002)

    result = engine.process_source(contest, "pvp")
    assert result.awarded_tickets == 0
    assert result.awarded_events == 0
    assert not RaffleTicketLedgerEntry.objects.filter(contest=contest).exists()
    assert not RaffleIneligibleActivity.objects.filter(contest=contest).exists()


@pytest.mark.django_db
def test_alliance_mate_kill_awards_nothing_even_with_exclude_blue_off(django_user_model):
    """When the contest admits alliance pilots, killing an alliance-mate (a corp NOT
    separately registered as friendly) earns nothing even if exclude_blue is turned
    off — the friendly-fire guard mirrors eligibility's notion of 'our side' and is
    independent of the exclude_blue toggle."""
    from apps.corporation.access import invalidate_access_cache
    from apps.corporation.models import PartnerAlliance

    PartnerAlliance.objects.create(alliance_id=555000, name="Our Alliance")
    invalidate_access_cache()  # LocMemCache persists across tests — force a recompute

    contest = make_contest(include_alliance=True)
    # Officer disables the (toggleable) blue-exclusion filter.
    cfg = contest.source_configs.filter(source_key="pvp").first()
    cfg.filters["exclude_blue"] = False
    cfg.save(update_fields=["filters", "updated_at"])

    enrol_pilot(django_user_model, 1001)
    # Victim's corp (999) is not ours, but their alliance (555000) is our side.
    home_kill(5025, is_solo=True, attackers=[(1001, HOME_CORP, True)],
              victim_corp=999, victim_alliance=555000)

    result = engine.process_source(contest, "pvp")
    assert result.awarded_tickets == 0
    assert not RaffleTicketLedgerEntry.objects.filter(contest=contest).exists()
    assert not RaffleIneligibleActivity.objects.filter(contest=contest).exists()


@pytest.mark.django_db
def test_enemy_kill_still_awards_after_corp_guard(django_user_model):
    """The corp-on-corp guard must not block a normal enemy kill (victim not ours)."""
    contest = make_contest()
    enrol_pilot(django_user_model, 1001)
    home_kill(5021, is_solo=True, attackers=[(1001, HOME_CORP, True)],
              victim_corp=999)  # 999 != HOME_CORP → legitimate enemy kill
    result = engine.process_source(contest, "pvp")
    assert result.awarded_tickets == 100


@pytest.mark.django_db
def test_attacker_name_stamped_on_ledger_and_ineligible(django_user_model):
    """Both the ledger row (enrolled) and the ineligible/outreach row (unenrolled)
    carry a resolved name instead of a blank — the F2 fix."""
    from apps.corporation.models import EveName

    contest = make_contest()
    enrol_pilot(django_user_model, 1001, name="Main Pilot")
    EveName.objects.create(entity_id=2002, name="Unenrolled Alt", category="character")
    home_kill(5022, is_solo=False, attackers=[
        (1001, HOME_CORP, True),    # enrolled → ledger, name from EveCharacter
        (2002, HOME_CORP, False),   # unenrolled → ineligible, name from EveName
    ])

    engine.process_source(contest, "pvp")
    led = RaffleTicketLedgerEntry.objects.get(contest=contest, character_id=1001)
    assert led.character_name == "Main Pilot"
    inelig = RaffleIneligibleActivity.objects.get(contest=contest, character_id=2002)
    assert inelig.character_name == "Unenrolled Alt"  # was "" before the fix


@pytest.mark.django_db
def test_attacker_name_falls_back_to_corp_roster(django_user_model):
    """When the resolved-name table has no entry, the corp member-tracking roster
    supplies the name for an unenrolled home-corp attacker."""
    from apps.corporation.models import CorpMember

    contest = make_contest()
    CorpMember.objects.create(character_id=2003, corporation_id=HOME_CORP, name="Roster Only")
    home_kill(5023, is_solo=True, attackers=[(2003, HOME_CORP, True)])

    engine.process_source(contest, "pvp")
    inelig = RaffleIneligibleActivity.objects.get(contest=contest, character_id=2003)
    assert inelig.character_name == "Roster Only"


@pytest.mark.django_db
def test_backfill_command_fills_blank_names(django_user_model):
    """The one-off backfill resolves blank character_name on existing rows."""
    from django.core.management import call_command

    from apps.corporation.models import EveName

    contest = make_contest()
    inelig = RaffleIneligibleActivity.objects.create(
        contest=contest, character_id=2004, character_name="",
        source_key="pvp", source_ref="killmail:5024",
        reason=RaffleIneligibleActivity.Reason.NOT_ENROLLED, would_be_tickets=1,
    )
    EveName.objects.create(entity_id=2004, name="Backfilled Pilot", category="character")

    call_command("backfill_raffle_names")
    inelig.refresh_from_db()
    assert inelig.character_name == "Backfilled Pilot"


@pytest.mark.django_db
def test_retroactive_off_does_not_award_after_enrolment(django_user_model):
    """Activity first recorded as ineligible stays ineligible after the pilot
    enrols when retroactive is off — exactly as the spec requires."""
    contest = make_contest(retroactive_enabled=False)
    home_kill(5006, is_solo=True, attackers=[(3003, HOME_CORP, True)])

    # Pass 1: 3003 is not enrolled → ineligible record, no ticket.
    engine.process_source(contest, "pvp")
    assert RaffleTicketLedgerEntry.objects.filter(contest=contest).count() == 0
    assert RaffleIneligibleActivity.objects.filter(contest=contest, character_id=3003).exists()

    # 3003 enrols…
    enrol_pilot(django_user_model, 3003)
    # Pass 2: retroactive off → the prior-ineligible event is never converted.
    engine.process_source(contest, "pvp")
    assert RaffleTicketLedgerEntry.objects.filter(contest=contest).count() == 0


@pytest.mark.django_db
def test_retroactive_on_converts_prior_ineligible(django_user_model):
    """With contest.retroactive_enabled AND the source retroactive flag, a
    re-process converts a prior-ineligible event into a real ticket."""
    contest = make_contest(retroactive_enabled=True)
    enable_source_retroactive(contest, "pvp")
    home_kill(5007, is_solo=True, attackers=[(3004, HOME_CORP, True)])

    engine.process_source(contest, "pvp")  # not enrolled yet → ineligible
    assert RaffleTicketLedgerEntry.objects.filter(contest=contest).count() == 0

    enrol_pilot(django_user_model, 3004)
    result = engine.process_source(contest, "pvp")  # now converts
    assert result.awarded_tickets == 100
    assert result.retroactive_events == 1
    assert _ledger_for(contest, 3004).get().amount == 100

    inelig = RaffleIneligibleActivity.objects.get(contest=contest, character_id=3004)
    assert inelig.retroactive_applied is True
    assert inelig.later_enrolled is True
