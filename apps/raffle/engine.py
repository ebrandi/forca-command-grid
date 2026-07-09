"""Ticket engine — the one place activity becomes (or is denied) tickets.

For each enabled source it re-scans the accrual window, and for every candidate
:class:`SourceEvent`:

* applies min-threshold, per-event cap, source caps (day/week/contest) and the
  contest booster multiplier;
* checks :mod:`apps.raffle.eligibility` (enrolment + valid ESI + corp membership +
  not excluded);
* **eligible** → writes an append-only :class:`RaffleTicketLedgerEntry`
  (idempotent via its unique constraint); ``officer_approved`` sources land as
  ``pending``;
* **ineligible** → records a :class:`RaffleIneligibleActivity` (adoption analytics
  + outreach), never a drawable ticket.

Idempotency + retroactive policy hinge on two pre-loaded sets: a ticket that was
already awarded is skipped; an event previously recorded as *ineligible* is only
converted to a ticket when the contest (and the source) explicitly enable
retroactive recalculation — otherwise activity from before a pilot enrolled never
earns, exactly as the spec requires.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from . import eligibility as elig
from .models import (
    RaffleIneligibleActivity,
    RaffleTicketLedgerEntry,
    RaffleTicketSourceConfig,
)
from .sources import get_source


@dataclass
class ProcessResult:
    source_key: str
    awarded_tickets: int = 0
    awarded_events: int = 0
    ineligible_events: int = 0
    retroactive_events: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


def _cap_period_key(scope: str, occurred_at) -> str:
    if scope == RaffleTicketSourceConfig.CapScope.DAILY:
        return occurred_at.date().isoformat()
    if scope == RaffleTicketSourceConfig.CapScope.WEEKLY:
        iso = occurred_at.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if scope == RaffleTicketSourceConfig.CapScope.CONTEST:
        return "all"
    return ""


def _final_tickets(contest, config, event) -> tuple[int, str]:
    """Apply min-threshold, per-event cap and booster. Returns (tickets, note)."""
    if config.min_threshold and Decimal(str(event.magnitude)) < config.min_threshold:
        return 0, "below threshold"
    tickets = int(event.base_tickets)
    if config.max_per_event is not None:
        tickets = min(tickets, config.max_per_event)
    mult = contest.booster_for(event.occurred_at)
    if mult != 1:
        tickets = int((Decimal(tickets) * mult).to_integral_value())
    return tickets, ""


def process_source(contest, source_key: str, *, dry_run: bool = False) -> ProcessResult:
    """Award (or record as ineligible) tickets for one source over the accrual window."""
    result = ProcessResult(source_key=source_key)
    config = contest.source_configs.filter(source_key=source_key).first()
    if config is None or not config.enabled:
        result.notes.append("source disabled")
        return result
    source = get_source(source_key)
    if source is None or source.manual_only:
        result.notes.append("not a sweepable source")
        return result

    since = contest.start_at
    until = min(timezone.now(), contest.end_at)
    if until <= since:
        return result

    retro = contest.retroactive_enabled and config.retroactive

    # Preload existing rows so a re-scan is cheap and correct (no per-event query).
    awarded_keys = set(
        RaffleTicketLedgerEntry.objects.filter(contest=contest, source_key=source_key)
        .values_list("source_ref", "character_id")
    )
    inelig_rows = {
        (r.source_ref, r.character_id): r
        for r in RaffleIneligibleActivity.objects.filter(contest=contest, source_key=source_key)
    }

    # Cap accounting: preload already-awarded ticket tallies per (character, period).
    cap_scope = config.cap_scope
    cap_amount = config.cap_amount or 0
    tally: dict[tuple[int, str], int] = {}
    if cap_scope != RaffleTicketSourceConfig.CapScope.NONE and cap_amount:
        for e in RaffleTicketLedgerEntry.objects.filter(
            contest=contest, source_key=source_key,
            status=RaffleTicketLedgerEntry.Status.APPROVED,
        ).values("character_id", "occurred_at", "amount"):
            k = (e["character_id"], _cap_period_key(cap_scope, e["occurred_at"]))
            tally[k] = tally.get(k, 0) + e["amount"]

    default_status = (
        RaffleTicketLedgerEntry.Status.PENDING
        if config.mode == RaffleTicketSourceConfig.Mode.OFFICER_APPROVED
        else RaffleTicketLedgerEntry.Status.APPROVED
    )

    new_ledger: list[RaffleTicketLedgerEntry] = []
    new_inelig: list[RaffleIneligibleActivity] = []
    retro_hits: list[RaffleIneligibleActivity] = []
    elig_cache: dict[int, elig.Eligibility] = {}

    for event in source.iter_events(contest, config, since, until):
        cid = event.character_id
        key = (event.source_ref, cid)
        if key in awarded_keys:
            continue  # already earned — idempotent
        tickets, note = _final_tickets(contest, config, event)
        if tickets <= 0:
            result.skipped += 1
            continue

        e = elig_cache.get(cid)
        if e is None:
            e = elig.for_character_id(contest, cid, character_name=event.character_name)
            elig_cache[cid] = e

        prior_inelig = inelig_rows.get(key)

        if e.eligible:
            # Non-retroactive: activity from BEFORE the pilot enrolled + connected a
            # valid token must never earn — even when first scanned after they enrol
            # (a back-dated start or a sweep gap). Record it as ineligible so it's
            # visible for outreach and convertible if retroactive is later enabled.
            if (not retro and e.eligible_since and event.occurred_at
                    and event.occurred_at < e.eligible_since):
                if prior_inelig is None:
                    new_inelig.append(RaffleIneligibleActivity(
                        contest=contest, character_id=cid,
                        character_name=e.character_name or event.character_name,
                        source_key=source_key, source_ref=event.source_ref,
                        reason=RaffleIneligibleActivity.Reason.NOT_ENROLLED,
                        would_be_tickets=tickets, detected_at=timezone.now(),
                        later_enrolled=True, metadata={**event.metadata, "pre_enrolment": True},
                    ))
                    inelig_rows[key] = new_inelig[-1]
                    result.ineligible_events += 1
                result.skipped += 1
                continue
            if prior_inelig is not None and not retro:
                # It was ineligible when it happened; non-retroactive → never award.
                result.skipped += 1
                continue
            # Apply source cap.
            if cap_scope != RaffleTicketSourceConfig.CapScope.NONE and cap_amount:
                pk = (cid, _cap_period_key(cap_scope, event.occurred_at))
                used = tally.get(pk, 0)
                if used >= cap_amount:
                    result.skipped += 1
                    continue
                tickets = min(tickets, cap_amount - used)
                tally[pk] = used + tickets
            new_ledger.append(RaffleTicketLedgerEntry(
                contest=contest, user_id=e.user_id, character_id=cid,
                character_name=e.character_name or event.character_name,
                source_key=source_key, source_ref=event.source_ref,
                amount=tickets, reason=event.reason, status=default_status,
                occurred_at=event.occurred_at,
                eligibility_snapshot=e.snapshot(), esi_status=e.esi_status,
                created_by_system=True, metadata=event.metadata,
            ))
            awarded_keys.add(key)
            result.awarded_events += 1
            result.awarded_tickets += tickets
            if prior_inelig is not None and retro:
                prior_inelig.retroactive_applied = True
                prior_inelig.later_enrolled = True
                retro_hits.append(prior_inelig)
                result.retroactive_events += 1
        else:
            if prior_inelig is None:
                new_inelig.append(RaffleIneligibleActivity(
                    contest=contest, character_id=cid,
                    character_name=e.character_name or event.character_name,
                    source_key=source_key, source_ref=event.source_ref,
                    reason=_map_reason(e.reason_code), would_be_tickets=tickets,
                    detected_at=timezone.now(),
                    later_enrolled=e.enrolled, metadata=event.metadata,
                ))
                inelig_rows[key] = new_inelig[-1]
                result.ineligible_events += 1
            elif e.enrolled and not prior_inelig.later_enrolled:
                prior_inelig.later_enrolled = True
                retro_hits.append(prior_inelig)

    if dry_run:
        result.notes.append("dry run — no writes")
        return result

    if new_ledger:
        RaffleTicketLedgerEntry.objects.bulk_create(new_ledger, batch_size=500, ignore_conflicts=True)
    if new_inelig:
        RaffleIneligibleActivity.objects.bulk_create(new_inelig, batch_size=500, ignore_conflicts=True)
    for row in retro_hits:
        row.save(update_fields=["retroactive_applied", "later_enrolled", "updated_at"])

    config.last_processed_at = timezone.now()
    config.save(update_fields=["last_processed_at", "updated_at"])
    return result


def _map_reason(reason_code: str) -> str:
    return {
        "not_enrolled": RaffleIneligibleActivity.Reason.NOT_ENROLLED,
        "no_token": RaffleIneligibleActivity.Reason.NO_TOKEN,
        "token_expired": RaffleIneligibleActivity.Reason.TOKEN_EXPIRED,
        "missing_scope": RaffleIneligibleActivity.Reason.MISSING_SCOPE,
        "not_corp": RaffleIneligibleActivity.Reason.NOT_CORP,
        "excluded": RaffleIneligibleActivity.Reason.EXCLUDED,
    }.get(reason_code, RaffleIneligibleActivity.Reason.NOT_ENROLLED)


def process_all_sources(contest, *, dry_run: bool = False) -> list[ProcessResult]:
    """Process every enabled, sweepable source for a contest."""
    results = []
    for config in contest.source_configs.filter(enabled=True):
        source = get_source(config.source_key)
        if source is None or source.manual_only:
            continue
        results.append(process_source(contest, config.source_key, dry_run=dry_run))
    return results


def preview_source(contest, source_key: str, *, lookback_days: int = 30) -> dict:
    """Dry-run estimate of a source over recent history (for the admin simulator).

    Counts how many tickets a rule WOULD have generated and how many pilots would
    have been excluded for missing enrolment/ESI — without writing anything.
    """
    config = contest.source_configs.filter(source_key=source_key).first()
    source = get_source(source_key)
    if config is None or source is None or source.manual_only:
        return {"tickets": 0, "events": 0, "ineligible": 0, "excluded_pilots": 0, "pilots": 0}
    until = timezone.now()
    since = until - timedelta(days=lookback_days)
    tickets = events = ineligible = 0
    eligible_pilots: set[int] = set()
    excluded_pilots: set[int] = set()
    cache: dict[int, elig.Eligibility] = {}
    for event in source.iter_events(contest, config, since, until):
        n, _ = _final_tickets(contest, config, event)
        if n <= 0:
            continue
        cid = event.character_id
        e = cache.get(cid) or elig.for_character_id(contest, cid)
        cache[cid] = e
        if e.eligible:
            tickets += n
            events += 1
            eligible_pilots.add(cid)
        else:
            ineligible += 1
            excluded_pilots.add(cid)
    return {
        "tickets": tickets, "events": events, "ineligible": ineligible,
        "excluded_pilots": len(excluded_pilots), "pilots": len(eligible_pilots),
    }
