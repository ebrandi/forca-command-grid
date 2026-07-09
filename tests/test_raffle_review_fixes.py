"""Regression tests for the adversarial-review fixes.

Covers: account-level draw eligibility for multi-character accounts, reversal
netting in the leaderboard, the non-retroactive pre-enrolment gate, manual-grant
separation of duties, and run_draw idempotency.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.raffle import engine, services
from apps.raffle.models import (
    RaffleContest,
    RaffleIneligibleActivity,
    RaffleParticipantEligibilitySnapshot,
    RaffleParticipantSummary,
    RaffleTicketLedgerEntry,
)
from apps.sso.models import AuthToken, EveCharacter
from core import rbac
from tests._raffle_utils import (
    add_prizes,
    add_token,
    approved_total,
    enable_source_retroactive,
    enrol_pilot,
    home_kill,
    make_contest,
)

pytestmark = pytest.mark.django_db


def _ledger(contest, user, character_id, ref, amount):
    return RaffleTicketLedgerEntry.objects.create(
        contest=contest, user=user, character_id=character_id, source_key="pvp",
        source_ref=ref, amount=amount, status=RaffleTicketLedgerEntry.Status.APPROVED,
        occurred_at=timezone.now(),
    )


def test_multichar_account_eligible_via_main_despite_revoked_alt(django_user_model):
    """A multi-character account stays in the draw pool when its MAIN is valid even
    if a revoked alt owns the newest ticket (the census must be account-level)."""
    user, main = enrol_pilot(django_user_model, 4001)  # valid token, corp
    alt = EveCharacter.objects.create(
        character_id=4002, user=user, is_corp_member=True, name="Alt",
        added_at=timezone.now() - timedelta(days=60),
    )
    revoked = add_token(alt)
    revoked.revoked_at = timezone.now()
    revoked.save(update_fields=["revoked_at"])

    contest = make_contest()
    add_prizes(contest, n=1)
    # Main earns first, the revoked alt earns the NEWEST ticket.
    _ledger(contest, user, 4001, "killmail:1", 10)
    _ledger(contest, user, 4002, "killmail:2", 1)

    services.set_status(contest, RaffleContest.Status.CLOSED)
    draw = services.run_draw(contest)

    snap = RaffleParticipantEligibilitySnapshot.objects.get(draw=draw, user=user)
    assert snap.eligible is True
    assert snap.tickets_counted == 11          # both characters' tickets aggregate
    assert draw.total_eligible_tickets == 11
    assert draw.results.filter(winner_user=user).exists()


def test_reversal_nets_out_of_leaderboard(django_user_model):
    """A reversed grant leaves a net-zero total (no double count, no negative)."""
    user, _ = enrol_pilot(django_user_model, 5001)
    officer = enrol_pilot(django_user_model, 5099, roles=(rbac.ROLE_OFFICER,))[0]
    contest = make_contest()

    grant = services.grant_manual_tickets(contest, officer, character_id=5001, amount=10,
                                          reason="test")
    services.reverse_entry(grant.ledger_entry, officer, reason="correction")

    services.recompute_summaries(contest)
    # The reversed original (status REVERSED) + the -10 reversal net to zero.
    assert not RaffleParticipantSummary.objects.filter(contest=contest, user=user).exists()
    assert approved_total(contest) == 0  # +10 reversed out, -10 reversal remains balanced


def test_non_retroactive_gate_blocks_pre_enrolment_activity(django_user_model):
    """Activity that predates enrolment earns nothing under the default policy,
    but is recorded as ineligible and converts once retroactive is enabled."""
    # Enrolled "now"; the kill happened 2h earlier (before enrolment).
    user, _ = enrol_pilot(django_user_model, 6001, enrolled_days_ago=0)
    contest = make_contest()  # retroactive_enabled defaults False
    home_kill(6100, attackers=[(6001, 98000001, True)], is_solo=True,
              when=timezone.now() - timedelta(hours=2))

    engine.process_source(contest, "pvp")
    assert approved_total(contest) == 0
    inelig = RaffleIneligibleActivity.objects.get(contest=contest, character_id=6001)
    assert inelig.metadata.get("pre_enrolment") is True
    assert inelig.would_be_tickets == 100

    # Turn retroactive on (contest + source) and re-sweep → it converts.
    contest.retroactive_enabled = True
    contest.save(update_fields=["retroactive_enabled"])
    enable_source_retroactive(contest, "pvp")
    engine.process_source(contest, "pvp")
    assert approved_total(contest) == 100
    inelig.refresh_from_db()
    assert inelig.retroactive_applied is True


def test_officer_cannot_self_grant_but_director_can(django_user_model):
    """Separation of duties: an officer can't grant to their own account; a director
    (break-glass) can; and an officer granting to another pilot works."""
    officer, _ = enrol_pilot(django_user_model, 7001, roles=(rbac.ROLE_OFFICER,))
    director, _ = enrol_pilot(django_user_model, 7002, roles=(rbac.ROLE_DIRECTOR,))
    other, _ = enrol_pilot(django_user_model, 7003)
    contest = make_contest()

    with pytest.raises(services.GrantBlocked):
        services.grant_manual_tickets(contest, officer, character_id=7001, amount=5,
                                      reason="self")

    # Director may grant to their own account (break-glass).
    g = services.grant_manual_tickets(contest, director, character_id=7002, amount=5,
                                      reason="ok")
    assert g.amount == 5
    # Officer -> a different eligible pilot is fine.
    g2 = services.grant_manual_tickets(contest, officer, character_id=7003, amount=3,
                                       reason="recognition")
    assert g2.amount == 3


def test_adoption_conversion_is_against_full_corp_roster(django_user_model):
    """ESI-adoption % is enrolled-vs-the-FULL-corp-roster (CorpMember), not vs the
    app-known pilots (which would be circular — everyone in the app has a token)."""
    from apps.corporation.models import CorpMember
    from apps.raffle import stats

    HOME = 98000001  # settings.FORCA_HOME_CORP_ID in tests
    # A corp roster of 10 pilots from ESI member-tracking; only 4 use the app.
    for cid in range(9001, 9011):
        CorpMember.objects.create(character_id=cid, corporation_id=HOME, name=f"Roster {cid}")
    enrol_pilot(django_user_model, 9001)
    enrol_pilot(django_user_model, 9002)
    enrol_pilot(django_user_model, 9003)
    _u4, ch4 = enrol_pilot(django_user_model, 9004)
    # 9004 enrolled but its token was later revoked → not "valid token".
    AuthToken.objects.filter(character=ch4).update(revoked_at=timezone.now())

    m = stats.adoption_metrics(use_cache=False)
    assert m["active_pilots"] == 10          # full roster, NOT the 4 app pilots
    assert m["enrolled"] == 4
    assert m["with_valid_token"] == 3        # 9004 revoked
    assert m["expired_or_revoked"] == 1
    assert m["unenrolled"] == 6
    assert m["conversion_rate"] == 40.0
    assert m["roster_synced"] is True


def test_run_draw_is_idempotent(django_user_model):
    """A second run_draw on a completed contest returns the same draw (no duplicate
    current draw); re-drawing is the explicit redraw path."""
    user, _ = enrol_pilot(django_user_model, 8001)
    contest = make_contest()
    add_prizes(contest, n=1)
    _ledger(contest, user, 8001, "killmail:9", 5)
    services.set_status(contest, RaffleContest.Status.CLOSED)

    d1 = services.run_draw(contest)
    d2 = services.run_draw(contest)
    assert d1.pk == d2.pk
    from apps.raffle.models import RaffleDraw
    assert contest.draws.filter(
        status=RaffleDraw.Status.COMPLETED, superseded_by__isnull=True
    ).count() == 1
