"""Ticket identity, frozen pools, verifiable draws, forfeits and redraws.

The two tests this file exists for are
:func:`test_winning_ticket_is_the_ticket_that_was_actually_drawn` and
:func:`test_verify_draw_detects_a_tampered_winner`. Before this work the draw recorded the
winner's *first* ticket instead of the one the hash chain rolled, and ``verify_draw``
compared the stored rolls against themselves without ever reading the results table — so a
tampered winner still rendered "Draw verified ✓". Both are pinned here so neither can come
back quietly.
"""
from __future__ import annotations

import hashlib

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.raffle import draw as draw_engine
from apps.raffle import readiness, services
from apps.raffle import snapshot as pool_snapshot
from apps.raffle import tickets as ticket_ids
from apps.raffle.draw import verify_draw
from apps.raffle.models import (
    RaffleContest,
    RaffleDraw,
    RaffleDrawResult,
    RaffleTicketLedgerEntry,
    RaffleTicketPoolSnapshot,
)
from core import rbac
from tests._raffle_utils import add_prizes, enrol_pilot, make_contest, make_user


def _grant(contest, actor, character_id, amount, reason="seed tickets"):
    return services.grant_manual_tickets(
        contest, actor, character_id=character_id, amount=amount, reason=reason
    )


def _entries(contest):
    return list(
        RaffleTicketLedgerEntry.objects.filter(contest=contest).order_by("id")
    )


# --------------------------------------------------------------------------- #
#  Ticket identity
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_ticket_numbers_are_contiguous_and_start_at_one(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7001)
    enrol_pilot(django_user_model, 7002)

    _grant(contest, director, 7001, 3)
    _grant(contest, director, 7002, 2)

    first, second = _entries(contest)
    assert first.ticket_start == 1
    assert first.ticket_numbers == [1, 2, 3]
    assert second.ticket_start == 4
    assert second.ticket_numbers == [4, 5]
    contest.refresh_from_db()
    assert contest.next_ticket_number == 6


@pytest.mark.django_db
def test_new_awards_append_and_never_renumber_existing_tickets(django_user_model):
    """The property a pilot's screenshot depends on."""
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7011)
    enrol_pilot(django_user_model, 7012)

    _grant(contest, director, 7011, 5)
    mine = _entries(contest)[0]
    before = mine.ticket_numbers

    # Somebody else earns a pile of tickets afterwards.
    for i in range(5):
        _grant(contest, director, 7012, 50, reason=f"later grant {i}")

    mine.refresh_from_db()
    assert mine.ticket_numbers == before == [1, 2, 3, 4, 5]


@pytest.mark.django_db
def test_reversed_award_keeps_its_numbers_and_leaves_a_gap(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7021)

    _grant(contest, director, 7021, 2)
    doomed = _entries(contest)[0]
    _grant(contest, director, 7021, 2, reason="second")
    later = _entries(contest)[1]

    services.reverse_entry(doomed, director, reason="wrong pilot")

    doomed.refresh_from_db()
    later.refresh_from_db()
    # The dead ticket keeps its identity — the gap is the evidence a correction happened.
    assert doomed.ticket_numbers == [1, 2]
    assert doomed.status == RaffleTicketLedgerEntry.Status.REVERSED
    assert not doomed.counts_for_draw
    # And the surviving award is NOT renumbered down into the gap.
    assert later.ticket_numbers == [3, 4]


@pytest.mark.django_db
def test_numbering_is_idempotent(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7031)
    _grant(contest, director, 7031, 4)

    before = [(e.pk, e.ticket_start) for e in _entries(contest)]
    assert ticket_ids.assign_ticket_numbers(contest) == 0  # nothing left to number
    assert [(e.pk, e.ticket_start) for e in _entries(contest)] == before


@pytest.mark.django_db
def test_entry_owning_ticket_finds_the_right_award(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7041)
    enrol_pilot(django_user_model, 7042)
    _grant(contest, director, 7041, 10)     # 1..10
    second = _grant(contest, director, 7042, 10)  # 11..20

    assert ticket_ids.entry_owning_ticket(contest, 1).character_id == 7041
    assert ticket_ids.entry_owning_ticket(contest, 10).character_id == 7041
    assert ticket_ids.entry_owning_ticket(contest, 11).pk == second.ledger_entry_id
    assert ticket_ids.entry_owning_ticket(contest, 20).character_id == 7042
    assert ticket_ids.entry_owning_ticket(contest, 21) is None
    assert ticket_ids.entry_owning_ticket(contest, 0) is None


@pytest.mark.django_db
def test_boosted_award_records_how_the_number_was_reached(client, django_user_model):
    """A double-ticket weekend must be legible on the ledger, not just a bigger number."""
    from datetime import timedelta
    from decimal import Decimal

    from apps.raffle import engine
    from tests._raffle_utils import home_kill

    now = timezone.now()
    contest = make_contest(
        booster_multiplier=Decimal("2"),
        booster_start_at=now - timedelta(days=2),
        booster_end_at=now + timedelta(days=2),
    )
    user, _ = enrol_pilot(django_user_model, 7051)
    home_kill(9101, attackers=[(7051, 98000001, True)], is_solo=True)
    engine.process_source(contest, "pvp")

    entry = RaffleTicketLedgerEntry.objects.get(contest=contest, character_id=7051)
    assert entry.amount == 200                       # solo kill 100, doubled
    assert entry.metadata["booster_multiplier"] == "2"
    assert entry.metadata["base_tickets"] == 100
    assert entry.ticket_start == 1                   # numbered by the sweep, not later

    client.force_login(user)
    body = client.get(reverse("raffle:tickets", args=[contest.slug])).content.decode()
    assert "#1–#200" in body
    assert "100 before the boost" in body


# --------------------------------------------------------------------------- #
#  Frozen pool snapshot
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_closing_a_contest_freezes_and_hashes_the_pool(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7101)
    enrol_pilot(django_user_model, 7102)
    _grant(contest, director, 7101, 30)
    _grant(contest, director, 7102, 20)

    assert pool_snapshot.current_snapshot(contest) is None
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    snap = pool_snapshot.current_snapshot(contest)
    assert snap is not None
    assert snap.version == 1
    assert snap.total_tickets == 50
    assert snap.total_entries == 2
    assert snap.total_pilots == 2
    assert len(snap.content_hash) == 64
    assert pool_snapshot.verify_snapshot(snap)["hash_ok"] is True
    # Draw space is contiguous and starts at zero even though ticket numbers start at 1.
    assert [row[4] for row in snap.entries] == [0, 30]


@pytest.mark.django_db
def test_snapshot_hash_detects_an_edited_pool(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7111)
    _grant(contest, director, 7111, 10)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    snap = pool_snapshot.current_snapshot(contest)

    assert pool_snapshot.verify_snapshot(snap)["hash_ok"] is True
    snap.entries[0][3] = 999          # quietly inflate the award
    snap.save(update_fields=["entries"])
    assert pool_snapshot.verify_snapshot(snap)["hash_ok"] is False


@pytest.mark.django_db
def test_snapshot_omits_unapproved_and_accountless_awards(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7121)
    _grant(contest, director, 7121, 10)
    excluded = _grant(contest, director, 7121, 5, reason="to be excluded").ledger_entry
    services.set_entry_status(excluded, director, RaffleTicketLedgerEntry.Status.EXCLUDED)
    # An award with no account can never be drawn, so it must not sit in the pool.
    RaffleTicketLedgerEntry.objects.create(
        contest=contest, user=None, character_id=999999, source_key="manual",
        source_ref="manual:orphan", amount=7,
        status=RaffleTicketLedgerEntry.Status.APPROVED, occurred_at=timezone.now(),
    )
    ticket_ids.assign_ticket_numbers(contest)

    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    snap = pool_snapshot.current_snapshot(contest)
    assert snap.total_tickets == 10
    assert snap.total_entries == 1


@pytest.mark.django_db
def test_refreezing_supersedes_the_previous_version(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7131)
    _grant(contest, director, 7131, 10)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    first = pool_snapshot.current_snapshot(contest)

    second = pool_snapshot.freeze_pool(contest, actor=director, supersede_reason="corrected")
    first.refresh_from_db()

    assert second.version == 2
    assert first.superseded_by_id == second.pk
    assert first.supersede_reason == "corrected"
    assert first.is_current is False
    assert pool_snapshot.current_snapshot(contest).pk == second.pk
    # History stays available to leadership.
    assert RaffleTicketPoolSnapshot.objects.filter(contest=contest).count() == 2


# --------------------------------------------------------------------------- #
#  Post-freeze ledger guard
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_ledger_cannot_be_changed_after_the_pool_is_frozen(django_user_model):
    """The attack the snapshot exists to stop: shift the pool after the seed is known."""
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7201)
    entry = _grant(contest, director, 7201, 10).ledger_entry
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    with pytest.raises(services.LedgerFrozen):
        services.set_entry_status(entry, director, RaffleTicketLedgerEntry.Status.DISQUALIFIED)
    with pytest.raises(services.LedgerFrozen):
        services.reverse_entry(entry, director, reason="nope")

    entry.refresh_from_db()
    assert entry.status == RaffleTicketLedgerEntry.Status.APPROVED


@pytest.mark.django_db
def test_audited_correction_refreezes_pool_and_discards_committed_seed(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7211)
    enrol_pilot(django_user_model, 7212)
    _grant(contest, director, 7211, 10)
    doomed = _grant(contest, director, 7212, 10).ledger_entry
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    committed = services.prepare_draw(contest, director)
    assert committed.status == RaffleDraw.Status.COMMITTED

    services.set_entry_status(doomed, director, RaffleTicketLedgerEntry.Status.DISQUALIFIED,
                              reason="awox", allow_frozen=True)

    committed.refresh_from_db()
    # The seed was generated against the OLD pool, so it must not survive the change.
    assert committed.status == RaffleDraw.Status.FAILED
    snap = pool_snapshot.current_snapshot(contest)
    assert snap.version == 2
    assert snap.total_tickets == 10


# --------------------------------------------------------------------------- #
#  The draw
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_winning_ticket_is_the_ticket_that_was_actually_drawn(django_user_model):
    """Regression: the recorded winner used to be the winner's FIRST ticket."""
    contest = make_contest(one_prize_per_pilot=True)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7301)
    _grant(contest, director, 7301, 500)   # one pilot, tickets #1..#500
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    draw = services.run_draw(contest, director)
    result = draw.results.get()
    snap = draw.snapshot

    # Recompute the roll independently, exactly as a pilot would.
    expected_offset = int(
        hashlib.sha256(f"{draw.seed}:0".encode()).hexdigest(), 16
    ) % snap.total_tickets
    owning = pool_snapshot.resolve_offset(snap, expected_offset)

    assert result.winning_ticket_index == expected_offset
    assert result.winning_ticket_no == owning["ticket_no"]
    assert result.winning_ticket_ref == f"ticket:{owning['ticket_no']}"
    # The whole point: it is a real drawn ticket, not the pilot's first one.
    assert result.winning_ticket_no == expected_offset + 1
    assert result.winning_ledger_entry_id == owning["entry_id"]
    assert result.winning_ledger_entry.owns_ticket(result.winning_ticket_no)


@pytest.mark.django_db
def test_verify_draw_detects_a_tampered_winner(django_user_model):
    """Regression: verification used to compare the roll log against itself."""
    contest = make_contest(one_prize_per_pilot=True)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    ua, _ = enrol_pilot(django_user_model, 7311)
    ub, _ = enrol_pilot(django_user_model, 7312)
    _grant(contest, director, 7311, 100)
    _grant(contest, director, 7312, 100)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    draw = services.run_draw(contest, director)
    assert verify_draw(draw)["winners_match"] is True

    # Somebody edits the winner straight in the database.
    result = draw.results.get()
    result.winner_user_id = ub.id if result.winner_user_id == ua.id else ua.id
    result.save(update_fields=["winner_user_id"])

    report = verify_draw(draw)
    assert report["winners_match"] is False
    assert report["commitment_ok"] is True   # the seed itself is untouched


@pytest.mark.django_db
def test_verify_draw_detects_a_tampered_ticket_number(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7321)
    _grant(contest, director, 7321, 50)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    draw = services.run_draw(contest, director)

    result = draw.results.get()
    result.winning_ticket_no = result.winning_ticket_no + 1
    result.save(update_fields=["winning_ticket_no"])

    assert verify_draw(draw)["winners_match"] is False


@pytest.mark.django_db
def test_draw_runs_against_the_frozen_pool_not_the_live_ledger(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7331)
    _grant(contest, director, 7331, 10)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    snap = pool_snapshot.current_snapshot(contest)

    # A row sneaks in after the freeze (bypassing the service guard entirely).
    RaffleTicketLedgerEntry.objects.create(
        contest=contest, user_id=director.pk, character_id=7331, source_key="manual",
        source_ref="manual:sneaky", amount=1000, ticket_start=10_000,
        status=RaffleTicketLedgerEntry.Status.APPROVED, occurred_at=timezone.now(),
    )

    draw = services.run_draw(contest, director)
    assert draw.snapshot_id == snap.pk
    assert draw.snapshot.total_tickets == 10        # the sneaked tickets are not in the pool
    assert draw.results.get().winning_ticket_no <= 10
    # And the pool no longer matches the ledger — surfaced, not swallowed.
    assert pool_snapshot.verify_snapshot(snap)["ledger_matches"] is False


@pytest.mark.django_db
def test_excluding_a_pilot_after_the_freeze_does_not_move_other_pilots(django_user_model):
    """Removing a pilot may cost them a win; it must not hand the win to a chosen pilot."""
    contest = make_contest(one_prize_per_pilot=True)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    ua, _ = enrol_pilot(django_user_model, 7341)
    ub, _ = enrol_pilot(django_user_model, 7342)
    _grant(contest, director, 7341, 40)
    _grant(contest, director, 7342, 60)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    snap = pool_snapshot.current_snapshot(contest)
    positions_before = [list(row) for row in snap.entries]

    services.exclude_pilot(contest, director, user=ub, reason="under review")
    draw = services.run_draw(contest, director)

    snap.refresh_from_db()
    assert [list(r) for r in snap.entries] == positions_before   # nothing repacked
    assert snap.total_tickets == 100                              # pool size unchanged
    assert draw.total_eligible_tickets == 40
    assert draw.total_excluded_tickets == 60
    assert draw.results.get().winner_user_id == ua.id
    # Whenever a roll DID land on the excluded pilot it was skipped, never converted into
    # a win. (Whether it happened at all depends on the seed — the deterministic case is
    # pinned in test_an_ineligible_pilots_drawn_ticket_is_skipped_and_recorded.)
    skipped_indexes = {s["draw_index"] for s in draw.skipped_draws if s["user_id"] == ub.id}
    for roll in draw.random_values:
        if roll["user_id"] == ub.id:
            assert roll["draw_index"] in skipped_indexes


@pytest.mark.django_db
def test_an_ineligible_pilots_drawn_ticket_is_skipped_and_recorded(django_user_model):
    """Pick a seed whose first roll provably lands on the excluded pilot."""
    contest = make_contest(one_prize_per_pilot=True)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    ua, _ = enrol_pilot(django_user_model, 7345)
    ub, _ = enrol_pilot(django_user_model, 7346)
    _grant(contest, director, 7345, 50)   # draw offsets 0..49
    _grant(contest, director, 7346, 50)   # draw offsets 50..99
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    snap = pool_snapshot.current_snapshot(contest)
    services.exclude_pilot(contest, director, user=ub, reason="under review")

    # Find a seed whose very first roll lands inside the excluded pilot's range.
    seed = next(
        s for s in (f"{i:064x}" for i in range(500))
        if pool_snapshot.resolve_offset(
            snap, int(hashlib.sha256(f"{s}:0".encode()).hexdigest(), 16) % 100
        )["user_id"] == ub.id
    )
    draw = RaffleDraw.objects.create(
        contest=contest, snapshot=snap, snapshot_hash=snap.content_hash,
        status=RaffleDraw.Status.COMMITTED, algorithm_version=contest.algorithm_version,
        seed=seed, seed_commitment=hashlib.sha256(seed.encode()).hexdigest(),
    )
    draw = draw_engine.execute_draw(draw)

    first_skip = draw.skipped_draws[0]
    assert first_skip["draw_index"] == 0
    assert first_skip["user_id"] == ub.id
    assert first_skip["reason"] == "not eligible at draw time"
    assert first_skip["ticket_no"] >= 51        # a real ticket of theirs, named in the log
    assert draw.results.get().winner_user_id == ua.id


@pytest.mark.django_db
def test_run_draw_twice_returns_the_same_draw(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7351)
    _grant(contest, director, 7351, 10)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    first = services.run_draw(contest, director)
    second = services.run_draw(contest, director)
    assert second.pk == first.pk
    assert RaffleDraw.objects.filter(contest=contest,
                                     status=RaffleDraw.Status.COMPLETED).count() == 1


@pytest.mark.django_db
def test_verification_survives_a_capped_roll_log(django_user_model):
    """The roll log is a capped sample; replay must come from the seed, not from it.

    A badly skewed pool can burn tens of thousands of re-rolls before it lands on an
    un-won pilot, so only the first ``_MAX_RECORDED_ROLLS`` are persisted (plus every
    decisive one). If verification replayed from that list it would silently start
    reporting tampering on exactly the draws that needed scrutiny most.
    """
    contest = make_contest(one_prize_per_pilot=True)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7361)
    enrol_pilot(django_user_model, 7362)
    _grant(contest, director, 7361, 60)
    _grant(contest, director, 7362, 40)
    add_prizes(contest, n=2)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    draw = services.run_draw(contest, director)

    assert draw.manifest["rolls_consumed"] >= len(draw.random_values)
    assert verify_draw(draw)["winners_match"] is True

    # Model the capped case: drop everything but the decisive rolls.
    winners = {(r.winner_user_id, r.winning_ticket_no) for r in draw.results.all()}
    draw.random_values = [
        rv for rv in draw.random_values if (rv["user_id"], rv["ticket_no"]) in winners
    ]
    draw.save(update_fields=["random_values"])

    report = verify_draw(draw)
    assert report["winners_match"] is True, "replay must not depend on the roll sample"
    assert report["values_ok"] is True


@pytest.mark.django_db
def test_empty_pool_draws_nothing_and_stays_verifiable(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    add_prizes(contest, n=2)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    draw = services.run_draw(contest, director)
    assert draw.results.count() == 0
    report = verify_draw(draw)
    assert report["commitment_ok"] is True
    assert report["winners_match"] is True


# --------------------------------------------------------------------------- #
#  Forfeit + replacement + redraw
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_forfeit_keeps_the_original_and_draws_a_replacement(django_user_model):
    contest = make_contest(one_prize_per_pilot=True)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7401)
    enrol_pilot(django_user_model, 7402)
    _grant(contest, director, 7401, 50)
    _grant(contest, director, 7402, 50)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    draw = services.run_draw(contest, director)
    original = draw.results.get()

    replacement = services.forfeit_result(original, director, reason="left the corp")

    original.refresh_from_db()
    assert original.status == RaffleDrawResult.Status.FORFEITED
    assert original.status_reason == "left the corp"
    assert original.status_changed_by_id == director.pk
    assert replacement is not None
    assert replacement.replaces_id == original.pk
    assert replacement.winner_user_id != original.winner_user_id
    assert replacement.prize_id == original.prize_id
    assert replacement.winning_ticket_no is not None
    # The original is still on file, and only one live winner holds the prize.
    assert draw.results.count() == 2
    assert draw.results.filter(status=RaffleDrawResult.Status.WON).count() == 1
    # The replacement came from the same frozen pool, so it is still verifiable.
    assert verify_draw(draw)["winners_match"] is True


@pytest.mark.django_db
def test_forfeit_requires_a_reason_and_cannot_repeat(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7411)
    _grant(contest, director, 7411, 10)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    result = services.run_draw(contest, director).results.get()

    with pytest.raises(services.GrantBlocked):
        services.forfeit_result(result, director, reason="  ")

    services.forfeit_result(result, director, reason="declined", draw_replacement=False)
    result.refresh_from_db()
    with pytest.raises(services.GrantBlocked):
        services.forfeit_result(result, director, reason="again")


@pytest.mark.django_db
def test_redraw_records_its_reason_and_marks_the_old_winners(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7421)
    _grant(contest, director, 7421, 20)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    first = services.run_draw(contest, director)
    first_result = first.results.get()

    second = services.redraw(contest, director, reason="wrong prize configured")

    first.refresh_from_db()
    first_result.refresh_from_db()
    assert first.superseded_by_id == second.pk
    assert second.redraw_reason == "wrong prize configured"
    assert second.redraw_authorised_by_id == director.pk
    # The superseded result is marked, never deleted.
    assert first_result.status == RaffleDrawResult.Status.REDRAWN
    assert first_result.status_reason == "wrong prize configured"
    assert RaffleDrawResult.objects.filter(draw=first).count() == 1


@pytest.mark.django_db
def test_redraw_requires_a_reason(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7431)
    _grant(contest, director, 7431, 10)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    services.run_draw(contest, director)

    with pytest.raises(services.GrantBlocked):
        services.redraw(contest, director, reason="")


# --------------------------------------------------------------------------- #
#  Pre-draw validation
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_checklist_blocks_an_unfrozen_contest(django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7501)
    _grant(contest, director, 7501, 10)
    add_prizes(contest, n=1)

    check = readiness.draw_checklist(contest)
    assert check["blocked"] is True
    keys = {c["key"]: c["state"] for c in check["checks"]}
    assert keys["pool"] == readiness.CRITICAL
    assert keys["closed"] == readiness.CRITICAL


@pytest.mark.django_db
def test_checklist_passes_once_closed_with_prizes_and_tickets(django_user_model):
    contest = make_contest(end_days_ahead=-1, draw_days_ahead=1)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7511)
    _grant(contest, director, 7511, 10)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    check = readiness.draw_checklist(contest)
    assert check["blocked"] is False, [
        (c["key"], str(c["detail"])) for c in check["checks"] if c["state"] == readiness.CRITICAL
    ]


@pytest.mark.django_db
def test_console_blocks_a_draw_that_fails_a_critical_check(client, django_user_model):
    """A critical failure has no override — the result could not be defended afterwards."""
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7531)
    _grant(contest, director, 7531, 10)
    add_prizes(contest, n=1)
    # Still ACTIVE: no frozen pool, so there is nothing legitimate to draw against.
    url = reverse("admin_audit:raffle_draw_action", args=[contest.pk])

    client.force_login(director)
    resp = client.post(url, {"action": "execute", "ack_warnings": "1"})

    assert resp.status_code == 302
    assert not contest.draws.filter(status=RaffleDraw.Status.COMPLETED).exists()


@pytest.mark.django_db
def test_console_requires_warnings_to_be_acknowledged(client, django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7541)
    _grant(contest, director, 7541, 10)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    url = reverse("admin_audit:raffle_draw_action", args=[contest.pk])
    client.force_login(director)

    # The fixture never sweeps its sources, so the checklist warns.
    assert readiness.draw_checklist(contest)["warnings"] >= 1
    client.post(url, {"action": "execute"})
    assert not contest.draws.filter(status=RaffleDraw.Status.COMPLETED).exists()

    client.post(url, {"action": "execute", "ack_warnings": "1"})
    assert contest.draws.filter(status=RaffleDraw.Status.COMPLETED).exists()


@pytest.mark.django_db
def test_checklist_warns_when_prizes_outnumber_eligible_pilots(django_user_model):
    contest = make_contest(end_days_ahead=-1, one_prize_per_pilot=True)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7521)
    _grant(contest, director, 7521, 10)
    add_prizes(contest, n=3)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    check = readiness.draw_checklist(contest)
    states = {c["key"]: c["state"] for c in check["checks"]}
    assert states["pilot_count"] == readiness.WARNING
    assert check["blocked"] is False


# --------------------------------------------------------------------------- #
#  Pilot-facing pages + authorisation
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_pilot_sees_their_own_ticket_numbers(client, django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    user, _ = enrol_pilot(django_user_model, 7601)
    _grant(contest, director, 7601, 7, reason="good fight")

    client.force_login(user)
    resp = client.get(reverse("raffle:tickets", args=[contest.slug]))
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "#1–#7" in body
    assert "good fight" in body


@pytest.mark.django_db
def test_ticket_ledger_shows_only_my_own_tickets(client, django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    mine, _ = enrol_pilot(django_user_model, 7611)
    enrol_pilot(django_user_model, 7612)
    _grant(contest, director, 7611, 3, reason="my own award")
    _grant(contest, director, 7612, 3, reason="somebody elses award")

    client.force_login(mine)
    body = client.get(reverse("raffle:tickets", args=[contest.slug])).content.decode()

    assert "my own award" in body
    assert "somebody elses award" not in body


@pytest.mark.django_db
def test_ticket_ledger_filters_by_ticket_number(client, django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    user, _ = enrol_pilot(django_user_model, 7621)
    _grant(contest, director, 7621, 5, reason="first award")     # 1..5
    _grant(contest, director, 7621, 5, reason="second award")    # 6..10

    client.force_login(user)
    url = reverse("raffle:tickets", args=[contest.slug])
    body = client.get(url, {"q": "7"}).content.decode()

    assert "second award" in body
    assert "first award" not in body


@pytest.mark.django_db
def test_ticket_ledger_ignores_a_junk_filter_instead_of_echoing_it(client, django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    user, _ = enrol_pilot(django_user_model, 7631)
    _grant(contest, director, 7631, 3, reason="real award")

    client.force_login(user)
    resp = client.get(reverse("raffle:tickets", args=[contest.slug]),
                      {"source": "'); alert(1);//", "status": "<script>"})
    body = resp.content.decode()

    assert resp.status_code == 200
    assert "alert(1)" not in body
    assert "<script>" not in body.replace("<script nonce", "")
    assert "real award" in body        # the junk filter is dropped, not applied


@pytest.mark.django_db
def test_pool_roster_is_shown_to_a_corp_member(client, django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    member, _ = enrol_pilot(django_user_model, 7641)
    _grant(contest, director, 7641, 10)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)

    client.force_login(member)
    resp = client.get(reverse("raffle:pool", args=[contest.slug]))
    assert resp.status_code == 200
    assert "#1–#10" in resp.content.decode()


@pytest.mark.django_db
def test_roster_gate_is_the_member_role(django_user_model):
    """The predicate itself — the reachable end-to-end cases are covered separately.

    A pilot with no role assignment never actually reaches a raffle page (onboarding
    intercepts them first), so this asserts the gate directly rather than dressing up an
    unreachable request as a test.
    """
    from apps.raffle.views import _is_member

    member, _ = enrol_pilot(django_user_model, 7645)
    roleless, _ = enrol_pilot(django_user_model, 7646, roles=())

    assert _is_member(member) is True
    assert _is_member(roleless) is False


@pytest.mark.django_db
def test_pool_is_never_rostered_to_the_open_internet(client, django_user_model, settings):
    """Even with the raffle opened to the public, the per-pilot roster stays corp-only."""
    from core.features import AUDIENCE_PUBLIC, set_feature_audiences

    settings.FORCA_HOME_CORP_ID = 98000001
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7643)
    _grant(contest, director, 7643, 10)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    set_feature_audiences({"raffle": AUDIENCE_PUBLIC})

    resp = client.get(reverse("raffle:pool", args=[contest.slug]))
    assert resp.status_code == 403
    body = resp.content.decode()
    assert "Pilot 7643" not in body
    # The proof itself is still published — just not the roster.
    assert pool_snapshot.current_snapshot(contest).content_hash in body


@pytest.mark.django_db
def test_receipt_json_hides_the_roster_from_non_members(client, django_user_model, settings):
    from core.features import AUDIENCE_PUBLIC, set_feature_audiences

    settings.FORCA_HOME_CORP_ID = 98000001
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    member, _ = enrol_pilot(django_user_model, 7651)
    _grant(contest, director, 7651, 10)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    services.run_draw(contest, director)
    set_feature_audiences({"raffle": AUDIENCE_PUBLIC})
    url = reverse("raffle:receipt_json", args=[contest.slug])

    public = client.get(url).json()
    assert public["snapshot"]["content_hash"]
    assert "entries" not in public["snapshot"]      # no per-pilot roster
    assert "pilots" not in public["snapshot"]
    assert public["winners"][0]["winning_ticket"]   # but the winning ticket IS public
    assert public["draw"]["seed"]                   # and so is the revealed seed

    client.force_login(member)
    as_member = client.get(url).json()
    assert len(as_member["snapshot"]["entries"]) == 1
    assert as_member["snapshot"]["pilots"]


@pytest.mark.django_db
def test_receipt_page_shows_the_winning_ticket_number(client, django_user_model):
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    user, _ = enrol_pilot(django_user_model, 7661)
    _grant(contest, director, 7661, 25)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    draw = services.run_draw(contest, director)

    client.force_login(user)
    body = client.get(reverse("raffle:transparency", args=[contest.slug])).content.decode()
    assert f"#{draw.results.get().winning_ticket_no}" in body
    assert draw.snapshot.content_hash in body
    assert "Draw verified" in body


@pytest.mark.django_db
def test_legacy_draw_is_reported_as_unverifiable_not_verified(django_user_model):
    """A pre-snapshot draw must never claim a verification that never happened."""
    contest = make_contest()
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    enrol_pilot(django_user_model, 7701)
    _grant(contest, director, 7701, 10)
    add_prizes(contest, n=1)
    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    draw = services.run_draw(contest, director)

    # Model the historical shape: a completed draw with no frozen pool behind it.
    draw.snapshot = None
    draw.save(update_fields=["snapshot"])

    report = verify_draw(draw)
    assert report["verifiable"] is False
    assert report["legacy"] is True
    assert report.get("winners_match") is None


# --------------------------------------------------------------------------- #
#  Scale
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
def test_large_pool_freezes_draws_and_paginates_without_blowing_up(
    client, django_user_model, django_assert_max_num_queries
):
    """200 pilots / ~100k tickets: the pool is per-award, so this stays small."""
    contest = make_contest(end_days_ahead=-1)
    director = make_user(django_user_model, "dir", rbac.ROLE_DIRECTOR)
    rows = []
    for i in range(200):
        user, _ = enrol_pilot(django_user_model, 8000 + i)
        for k in range(3):
            rows.append(RaffleTicketLedgerEntry(
                contest=contest, user=user, character_id=8000 + i,
                character_name=f"Pilot {8000 + i}", source_key="pvp",
                source_ref=f"killmail:{i}-{k}", amount=(i % 7 + 1) * 25,
                status=RaffleTicketLedgerEntry.Status.APPROVED,
                occurred_at=timezone.now(),
            ))
    RaffleTicketLedgerEntry.objects.bulk_create(rows)
    ticket_ids.assign_ticket_numbers(contest)
    add_prizes(contest, n=3)

    services.set_status(contest, RaffleContest.Status.CLOSED, director)
    snap = pool_snapshot.current_snapshot(contest)
    assert snap.total_entries == 600
    assert snap.total_tickets > 50_000

    draw = services.run_draw(contest, director)
    assert draw.results.filter(status=RaffleDrawResult.Status.WON).count() == 3
    assert verify_draw(draw)["winners_match"] is True

    # One page of the pool must not scale with the pool: the rows come from the
    # snapshot JSON, so the page is a fixed handful of queries however big it gets.
    member = django_user_model.objects.get(username="pilot-8000")
    client.force_login(member)
    resp = client.get(reverse("raffle:pool", args=[contest.slug]))
    assert resp.status_code == 200
    # 600 awards, one page of 100 rendered.
    assert b"Page 1 of 6" in resp.content


def _ledger_query_count(client, django_user_model, character_id, awards):
    """Render page 1 of one pilot's ledger over ``awards`` history rows; count queries."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    contest = make_contest(name=f"Scale {character_id}")
    user, _ = enrol_pilot(django_user_model, character_id)
    RaffleTicketLedgerEntry.objects.bulk_create([
        RaffleTicketLedgerEntry(
            contest=contest, user=user, character_id=character_id,
            character_name=f"Pilot {character_id}", source_key="pvp",
            source_ref=f"killmail:{i}", amount=10,
            status=RaffleTicketLedgerEntry.Status.APPROVED, occurred_at=timezone.now(),
        )
        for i in range(awards)
    ])
    ticket_ids.assign_ticket_numbers(contest)

    client.force_login(user)
    url = reverse("raffle:tickets", args=[contest.slug])
    # Warm first: the initial request in a test also fills the feature-audience and nav
    # caches, which would otherwise swamp the signal we are measuring.
    client.get(url)
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(url)
    assert resp.status_code == 200
    return len(ctx), resp


@pytest.mark.django_db
def test_ticket_ledger_page_cost_does_not_grow_with_history(client, django_user_model):
    """Not an arbitrary query budget — an equality. 8x the history, same page cost.

    This is the property that matters for a pilot with thousands of tickets: the page
    reads one page of awards and renders each as a RANGE, so nothing about it is
    proportional to how much they have earned.
    """
    small, _ = _ledger_query_count(client, django_user_model, 8500, 50)
    large, resp = _ledger_query_count(client, django_user_model, 8501, 400)

    assert large == small, f"ledger page cost grew from {small} to {large} queries"
    assert b"Page 1 of 8" in resp.content   # only one page rendered, not all 400
