"""Winner draw — cryptographically-secure, commit-reveal, reproducible, auditable.

Fairness model (kept deliberately simple so pilots can trust it):

1. When the draw is *prepared*, a 32-byte seed is generated with the OS CSPRNG
   (:mod:`secrets`) and only its SHA-256 **commitment** is published. The seed
   stays hidden until the draw runs.
2. The draw is a **hash chain**: draw *i* takes ``r_i = int(sha256(f"{seed}:{i}"))
   % total_tickets`` and maps ``r_i`` onto the fixed, ordered ticket ranges. This
   is deterministic — anyone with the revealed seed and the published ticket pool
   can recompute every pick and verify the winners.
3. An optional public ``external_entropy`` string (a beacon value, a block hash, a
   number called on comms) is folded into the effective seed, so not even the
   server can have pre-selected the outcome.
4. Every random value, every skipped draw (a ticket for a pilot who already won
   under one-prize-per-pilot), and the full eligibility census are stored in the
   :class:`RaffleDraw` manifest.

Only pilots who are **enrolled with a valid ESI token, a recognised corp pilot,
and not excluded at draw time** enter the pool — their eligibility is frozen into
:class:`RaffleParticipantEligibilitySnapshot` rows, and their tickets are counted;
everyone else's tickets are counted as *excluded* and recorded in the manifest.
"""
from __future__ import annotations

import bisect
import hashlib
import secrets
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from core.version import git_commit

from . import boosters
from . import eligibility as elig
from .models import (
    RaffleDraw,
    RaffleDrawResult,
    RaffleParticipantEligibilitySnapshot,
    RaffleTicketLedgerEntry,
)

_MAX_DRAW_ITERS_BUFFER = 10000  # safety cap on top of the ticket count


def _hash_int(seed: str, index: int) -> int:
    return int(hashlib.sha256(f"{seed}:{index}".encode()).hexdigest(), 16)


def _effective_seed(seed: str, external_entropy: str) -> str:
    if not external_entropy:
        return seed
    return hashlib.sha256(f"{seed}|{external_entropy}".encode()).hexdigest()


@transaction.atomic
def prepare_draw(contest, *, executed_by=None, external_entropy: str = "") -> RaffleDraw:
    """Create a committed draw: generate the secret seed, publish its commitment.

    Supersedes any earlier non-completed draw for the contest so there is always a
    single current commitment.
    """
    contest.draws.filter(
        status__in=[RaffleDraw.Status.PENDING, RaffleDraw.Status.COMMITTED]
    ).update(status=RaffleDraw.Status.FAILED, error="superseded by a new prepared draw")
    seed = secrets.token_hex(32)
    return RaffleDraw.objects.create(
        contest=contest,
        status=RaffleDraw.Status.COMMITTED,
        algorithm_version=contest.algorithm_version,
        code_version=git_commit(),
        seed=seed,
        seed_commitment=hashlib.sha256(seed.encode()).hexdigest(),
        external_entropy=external_entropy,
        committed_at=timezone.now(),
        executed_by=executed_by,
    )


def _build_census(contest, draw):
    """Freeze per-account eligibility + ticket totals at draw time.

    Returns ``(pool, excluded, snapshots, totals)`` where ``pool`` is the ordered
    list of eligible ``{user_id, character_id, name, tickets}`` and ``totals`` is a
    dict of manifest counters.
    """
    # Approved, positive tickets grouped by account (the eligibility + prize unit).
    per_user_tickets: dict[int, int] = defaultdict(int)
    per_user_char: dict[int, tuple[int, str]] = {}
    for e in RaffleTicketLedgerEntry.objects.filter(
        contest=contest, status=RaffleTicketLedgerEntry.Status.APPROVED, amount__gt=0
    ).values("user_id", "character_id", "character_name", "amount"):
        uid = e["user_id"]
        if uid is None:
            continue  # override grant to a non-enrolled character — never drawable
        per_user_tickets[uid] += e["amount"]
        per_user_char.setdefault(uid, (e["character_id"], e["character_name"]))

    # Account-level eligibility, bulk-resolved (matches the leaderboard/pilot page
    # semantics and avoids an N+1 inside the locked draw transaction).
    bulk = elig.for_users_bulk(contest, list(per_user_tickets.keys()))

    pool = []
    snapshots = []
    excluded_tickets = 0
    excluded_pilots = 0
    exclusion_summary: dict[str, int] = defaultdict(int)

    for uid, tickets in per_user_tickets.items():
        fallback_id, fallback_name = per_user_char.get(uid, (None, ""))
        e = bulk.get(uid) or elig.Eligibility(reason_code="not_enrolled", user_id=uid)
        # Prefer the character that made the account eligible; else the ticket char.
        char_id = e.character_id or fallback_id
        char_name = e.character_name or fallback_name
        snap = RaffleParticipantEligibilitySnapshot(
            draw=draw, user_id=uid, character_id=char_id, character_name=char_name,
            enrolled=e.enrolled, has_valid_token=e.has_valid_token,
            is_corp_member=e.is_corp_member, scopes_ok=e.scopes_ok,
            manually_excluded=e.excluded, eligible=e.eligible,
            exclusion_reason="" if e.eligible else (e.reason_code or "ineligible"),
            tickets_counted=tickets if e.eligible else 0,
            tickets_excluded=0 if e.eligible else tickets,
        )
        snapshots.append(snap)
        if e.eligible:
            pool.append({"user_id": uid, "character_id": char_id,
                         "name": char_name, "tickets": tickets})
        else:
            excluded_tickets += tickets
            excluded_pilots += 1
            exclusion_summary[e.reason_code or "ineligible"] += 1

    # Deterministic order → reproducible ranges (sort by user id).
    pool.sort(key=lambda p: p["user_id"])
    start = 0
    for p in pool:
        p["range_start"] = start
        p["range_end"] = start + p["tickets"]  # half-open [start, end)
        start = p["range_end"]
    totals = {
        "total_eligible_tickets": start,
        "total_excluded_tickets": excluded_tickets,
        "eligible_pilots": len(pool),
        "excluded_pilots": excluded_pilots,
        "exclusion_summary": dict(exclusion_summary),
    }
    return pool, snapshots, totals


@transaction.atomic
def execute_draw(draw: RaffleDraw) -> RaffleDraw:
    """Run a committed draw: build the census, draw winners, store the manifest.

    Idempotent at the row level via a status compare-and-set — a redelivered/retried
    call whose CAS loses simply returns the row untouched.
    """
    locked = RaffleDraw.objects.select_for_update().get(pk=draw.pk)
    if locked.status not in (RaffleDraw.Status.COMMITTED, RaffleDraw.Status.RUNNING):
        return locked  # already completed/failed — benign
    locked.status = RaffleDraw.Status.RUNNING
    locked.started_at = timezone.now()
    locked.save(update_fields=["status", "started_at", "updated_at"])

    contest = locked.contest
    prizes = list(contest.prizes.order_by("rank"))

    pool, snapshots, totals = _build_census(contest, locked)
    RaffleParticipantEligibilitySnapshot.objects.filter(draw=locked).delete()
    RaffleParticipantEligibilitySnapshot.objects.bulk_create(snapshots, batch_size=500)

    total = totals["total_eligible_tickets"]
    starts = [p["range_start"] for p in pool]
    effective_seed = _effective_seed(locked.seed, locked.external_entropy)

    # Activity safeguard + prize-value booster, frozen at draw time.
    activity = boosters.min_activity_status(contest)
    booster = boosters.prize_booster_status(contest)
    booster_achieved = booster["achieved"]

    won_users: set[int] = set()
    results: list[RaffleDrawResult] = []
    random_values: list[dict] = []
    skipped: list[dict] = []
    draw_index = 0
    one_per = contest.one_prize_per_pilot

    def _unwon_tickets() -> int:
        return total - sum(p["tickets"] for p in pool if p["user_id"] in won_users)

    for order, prize in enumerate(prizes, start=1):
        if total <= 0 or (one_per and _unwon_tickets() <= 0):
            break  # no eligible tickets left to award
        # Per-prize (not additive) budget: a dominant already-won pilot can never
        # starve a later prize of its own draw attempts.
        budget = total * 2 + _MAX_DRAW_ITERS_BUFFER
        winner = None
        for _ in range(budget):
            r = _hash_int(effective_seed, draw_index) % total
            draw_index += 1
            cand = pool[bisect.bisect_right(starts, r) - 1]
            random_values.append({
                "draw_index": draw_index - 1, "value": r,
                "user_id": cand["user_id"], "prize_rank": prize.rank,
            })
            if one_per and cand["user_id"] in won_users:
                skipped.append({
                    "draw_index": draw_index - 1, "value": r,
                    "user_id": cand["user_id"], "name": cand["name"],
                    "prize_rank": prize.rank, "reason": "already won a prize",
                })
                continue
            winner = cand
            break
        if winner is None:
            # Pathological skew only (a tiny unwon fraction exhausted the budget):
            # draw uniformly-by-ticket among the un-won pilots via the hash chain so
            # a fair winner is still guaranteed and the fallback is recorded.
            unwon = [p for p in pool if p["user_id"] not in won_users]
            unwon_total = sum(p["tickets"] for p in unwon)
            if not unwon or unwon_total <= 0:
                break
            r2 = _hash_int(effective_seed, draw_index) % unwon_total
            draw_index += 1
            acc = 0
            for p in unwon:
                acc += p["tickets"]
                if r2 < acc:
                    winner = p
                    break
            skipped.append({
                "draw_index": draw_index - 1, "value": r2, "prize_rank": prize.rank,
                "user_id": winner["user_id"], "name": winner["name"],
                "reason": "fallback draw among un-won pilots (extreme ticket skew)",
            })
        won_users.add(winner["user_id"])
        results.append(RaffleDrawResult(
            draw=locked, prize=prize, winner_user_id=winner["user_id"],
            winner_character_id=winner["character_id"],
            winner_character_name=winner["name"], draw_order=order,
            winning_ticket_index=winner["range_start"],
            winning_ticket_ref=f"ticket:{winner['range_start']}",
            awarded_value=boosters.effective_prize_value(prize, contest, achieved=booster_achieved),
        ))

    RaffleDrawResult.objects.filter(draw=locked).delete()
    RaffleDrawResult.objects.bulk_create(results, batch_size=100)

    locked.status = RaffleDraw.Status.COMPLETED
    locked.completed_at = timezone.now()
    locked.revealed_at = timezone.now()
    locked.total_eligible_tickets = totals["total_eligible_tickets"]
    locked.total_excluded_tickets = totals["total_excluded_tickets"]
    locked.eligible_pilots = totals["eligible_pilots"]
    locked.excluded_pilots = totals["excluded_pilots"]
    locked.min_activity_met = activity["met"]
    # If activity wasn't met but we still drew, it was a leadership override.
    locked.forced_below_minimum = bool(activity["configured"] and not activity["met"])
    locked.prize_booster_applied = booster_achieved
    locked.prize_booster_percent = booster["percent"] if booster_achieved else Decimal("0")
    locked.random_values = random_values
    locked.skipped_draws = skipped
    locked.manifest = {
        "contest_id": contest.id,
        "contest_name": contest.name,
        "slug": contest.slug,
        "draw_timestamp": locked.completed_at.isoformat(),
        "algorithm_version": locked.algorithm_version,
        "code_version": locked.code_version,
        "seed_commitment": locked.seed_commitment,
        "external_entropy": locked.external_entropy,
        "one_prize_per_pilot": one_per,
        **totals,
        "pool": pool,
        "prizes": [
            {"rank": p.rank, "name": p.name, "type": p.prize_type,
             "value": str(p.estimated_value)}
            for p in prizes
        ],
        "winners": [
            {"prize_rank": r.prize.rank, "user_id": r.winner_user_id,
             "character_id": r.winner_character_id, "name": r.winner_character_name,
             "winning_ticket": r.winning_ticket_index}
            for r in results
        ],
        "rules": {
            "require_enrolled": contest.require_enrolled,
            "require_valid_token": contest.require_valid_token,
            "include_alliance": contest.include_alliance,
            "retroactive_enabled": contest.retroactive_enabled,
        },
        "safeguards": {
            "min_activity_metric": activity.get("metric", ""),
            "min_activity_threshold": str(activity.get("threshold", 0)),
            "min_activity_value": str(activity.get("value", 0)),
            "min_activity_met": activity["met"],
            "forced_below_minimum": locked.forced_below_minimum,
            "prize_booster_metric": booster.get("metric", ""),
            "prize_booster_goal": str(booster.get("goal", 0)),
            "prize_booster_value": str(booster.get("value", 0)),
            "prize_booster_percent": str(locked.prize_booster_percent),
            "prize_booster_applied": booster_achieved,
        },
    }
    locked.save()
    return locked


def verify_draw(draw: RaffleDraw) -> dict:
    """Recompute a completed draw from its revealed seed + manifest and check it.

    Returns a report the transparency page renders — anyone can run the same maths.
    """
    if draw.status != RaffleDraw.Status.COMPLETED or not draw.seed:
        return {"verifiable": False,
                "reason": gettext("draw not completed / seed not revealed")}
    commitment_ok = hashlib.sha256(draw.seed.encode()).hexdigest() == draw.seed_commitment
    pool = draw.manifest.get("pool", [])
    total = draw.manifest.get("total_eligible_tickets", 0)
    if not pool or total <= 0:
        return {"verifiable": commitment_ok, "commitment_ok": commitment_ok,
                "winners_match": True, "reason": gettext("no eligible tickets")}
    starts = [p["range_start"] for p in pool]
    effective_seed = _effective_seed(draw.seed, draw.external_entropy)
    recomputed = [
        pool[bisect.bisect_right(starts, rv["value"]) - 1]["user_id"]
        for rv in draw.random_values
    ]
    recorded = [rv["user_id"] for rv in draw.random_values]
    values_ok = all(
        rv["value"] == _hash_int(effective_seed, rv["draw_index"]) % total
        for rv in draw.random_values
    )
    return {
        "verifiable": True,
        "commitment_ok": commitment_ok,
        "values_ok": values_ok,
        "winners_match": recomputed == recorded,
        "checked_draws": len(draw.random_values),
    }
