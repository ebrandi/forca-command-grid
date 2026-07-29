"""Winner draw — cryptographically-secure, commit-reveal, reproducible, auditable.

Fairness model (kept deliberately simple so pilots can trust it):

1. When accrual closes, the drawable ticket pool is frozen into an immutable, hashed
   :class:`~apps.raffle.models.RaffleTicketPoolSnapshot` and its SHA-256 is published.
2. When the draw is *prepared*, a 32-byte seed is generated with the OS CSPRNG
   (:mod:`secrets`) and only its SHA-256 **commitment** is published, alongside the hash
   of the pool it is bound to. The seed stays hidden until the draw runs.
3. The draw is a **hash chain**: roll *i* takes ``r_i = int(sha256(f"{seed}:{i}"), 16) %
   total_tickets`` and maps ``r_i`` onto the frozen pool. This is deterministic — anyone
   with the revealed seed and the published pool can recompute every roll.
4. An optional public ``external_entropy`` string (a beacon value, a block hash, a number
   called on comms) is folded into the effective seed, so not even the server can have
   pre-selected the outcome.
5. Every roll, every skip (an already-won pilot under one-prize-per-pilot, or a pilot no
   longer eligible at draw time) and the full eligibility census are stored on the draw.

Both halves of the input are therefore fixed and public *before* the outcome is known.
That is the whole point: with only a seed commitment, whoever could read the committed
seed could compute the winner and then move it by disqualifying one ledger entry, because
every later ticket range would shift. Binding the draw to a frozen pool removes the second
degree of freedom — see :mod:`apps.raffle.snapshot`.

Draw-time eligibility is applied as a **skip**, never as a re-packing of the pool: an
ineligible pilot's tickets keep their positions and are re-rolled past. Excluding someone
after the freeze can therefore only cost that pilot a win; it cannot hand the win to a
chosen pilot, because no one else's position moves.
"""
from __future__ import annotations

import hashlib
import secrets
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from core.version import git_commit

from . import boosters
from . import eligibility as elig
from . import snapshot as pool_snapshot
from .models import (
    RaffleDraw,
    RaffleDrawResult,
    RaffleParticipantEligibilitySnapshot,
)

_MAX_DRAW_ITERS_BUFFER = 10000  # safety cap on top of the ticket count

# How many individual rolls we persist on the draw row. The per-prize roll budget is
# ``2 × total + 10000``, so a badly skewed pool could otherwise write a six-figure list of
# dicts into a JSONB column on every draw. The decisive roll for each prize is ALWAYS
# recorded regardless of this cap, verification replays from the seed rather than from this
# list, and ``manifest["rolls_consumed"]`` discloses how many were dropped — a silent
# truncation would read as "this is the whole story" when it is not.
_MAX_RECORDED_ROLLS = 2000


def _hash_int(seed: str, index: int) -> int:
    return int(hashlib.sha256(f"{seed}:{index}".encode()).hexdigest(), 16)


def _effective_seed(seed: str, external_entropy: str) -> str:
    if not external_entropy:
        return seed
    return hashlib.sha256(f"{seed}|{external_entropy}".encode()).hexdigest()


@transaction.atomic
def prepare_draw(contest, *, executed_by=None, external_entropy: str = "") -> RaffleDraw:
    """Create a committed draw: freeze the pool if needed, generate the secret seed and
    publish both commitments.

    Supersedes any earlier non-completed draw for the contest so there is always a single
    current commitment.
    """
    contest.draws.filter(
        status__in=[RaffleDraw.Status.PENDING, RaffleDraw.Status.COMMITTED]
    ).update(status=RaffleDraw.Status.FAILED, error="superseded by a new prepared draw")
    snap = pool_snapshot.ensure_snapshot(contest, actor=executed_by)
    seed = secrets.token_hex(32)
    return RaffleDraw.objects.create(
        contest=contest,
        snapshot=snap,
        snapshot_hash=snap.content_hash,
        status=RaffleDraw.Status.COMMITTED,
        algorithm_version=contest.algorithm_version,
        code_version=git_commit(),
        seed=seed,
        seed_commitment=hashlib.sha256(seed.encode()).hexdigest(),
        external_entropy=external_entropy,
        committed_at=timezone.now(),
        executed_by=executed_by,
    )


def _census(contest, draw, snap):
    """Re-check eligibility at draw time over the frozen pool's pilots.

    The pool is fixed; this only decides who may *win* from it. Returns
    ``(eligible_user_ids, snapshots, totals)``.
    """
    pilots = pool_snapshot.pilot_index(snap)
    bulk = elig.for_users_bulk(contest, list(pilots.keys()))

    eligible: set[int] = set()
    snapshots = []
    excluded_tickets = excluded_pilots = eligible_tickets = 0
    exclusion_summary: dict[str, int] = {}

    for uid, info in sorted(pilots.items()):
        e = bulk.get(uid) or elig.Eligibility(reason_code="not_enrolled", user_id=uid)
        char_id = e.character_id or info["character_id"]
        char_name = e.character_name or info["name"]
        snapshots.append(RaffleParticipantEligibilitySnapshot(
            draw=draw, user_id=uid, character_id=char_id, character_name=char_name,
            enrolled=e.enrolled, has_valid_token=e.has_valid_token,
            is_corp_member=e.is_corp_member, scopes_ok=e.scopes_ok,
            manually_excluded=e.excluded, eligible=e.eligible,
            exclusion_reason="" if e.eligible else (e.reason_code or "ineligible"),
            tickets_counted=info["tickets"] if e.eligible else 0,
            tickets_excluded=0 if e.eligible else info["tickets"],
        ))
        if e.eligible:
            eligible.add(uid)
            eligible_tickets += info["tickets"]
        else:
            excluded_tickets += info["tickets"]
            excluded_pilots += 1
            code = e.reason_code or "ineligible"
            exclusion_summary[code] = exclusion_summary.get(code, 0) + 1

    totals = {
        "total_eligible_tickets": eligible_tickets,
        "total_excluded_tickets": excluded_tickets,
        "eligible_pilots": len(eligible),
        "excluded_pilots": excluded_pilots,
        "exclusion_summary": exclusion_summary,
        "pool_tickets": snap.total_tickets,
    }
    return eligible, snapshots, totals


@transaction.atomic
def execute_draw(draw: RaffleDraw) -> RaffleDraw:
    """Run a committed draw against its frozen pool and store the full record.

    Idempotent at the row level via a status compare-and-set — a redelivered/retried call
    whose CAS loses simply returns the row untouched.
    """
    locked = RaffleDraw.objects.select_for_update().get(pk=draw.pk)
    if locked.status not in (RaffleDraw.Status.COMMITTED, RaffleDraw.Status.RUNNING):
        return locked  # already completed/failed — benign
    locked.status = RaffleDraw.Status.RUNNING
    locked.started_at = timezone.now()
    locked.save(update_fields=["status", "started_at", "updated_at"])

    contest = locked.contest
    prizes = list(contest.prizes.order_by("rank"))

    snap = locked.snapshot or pool_snapshot.ensure_snapshot(contest)
    if locked.snapshot_id != snap.pk:
        locked.snapshot = snap
        locked.snapshot_hash = snap.content_hash

    eligible, snapshots, totals = _census(contest, locked, snap)
    RaffleParticipantEligibilitySnapshot.objects.filter(draw=locked).delete()
    RaffleParticipantEligibilitySnapshot.objects.bulk_create(snapshots, batch_size=500)

    total = snap.total_tickets
    starts = pool_snapshot.draw_starts(snap)
    pilots = pool_snapshot.pilot_index(snap)
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

    def _drawable_tickets() -> int:
        """Tickets that could still legitimately win the next prize."""
        return sum(
            info["tickets"] for uid, info in pilots.items()
            if uid in eligible and not (one_per and uid in won_users)
        )

    for order, prize in enumerate(prizes, start=1):
        if total <= 0 or _drawable_tickets() <= 0:
            break  # nothing left that may win
        # Per-prize (not additive) budget: a dominant already-won pilot can never starve a
        # later prize of its own roll attempts.
        budget = total * 2 + _MAX_DRAW_ITERS_BUFFER
        winner = None
        for _ in range(budget):
            r = _hash_int(effective_seed, draw_index) % total
            draw_index += 1
            hit = pool_snapshot.resolve_offset(snap, r, starts=starts)
            if hit is None:  # defensive: a malformed snapshot must not spin forever
                break
            uid = hit["user_id"]
            roll = {
                "draw_index": draw_index - 1, "value": r, "user_id": uid,
                "ticket_no": hit["ticket_no"], "prize_rank": prize.rank,
            }
            skip_reason = ""
            if uid not in eligible:
                skip_reason = "not eligible at draw time"
            elif one_per and uid in won_users:
                skip_reason = "already won a prize"

            if not skip_reason:
                # The decisive roll is always recorded, whatever the cap.
                random_values.append(roll)
                winner = hit
                break
            if len(random_values) < _MAX_RECORDED_ROLLS:
                random_values.append(roll)
            if len(skipped) < _MAX_RECORDED_ROLLS:
                skipped.append({
                    **roll, "name": pilots.get(uid, {}).get("name", ""),
                    "reason": skip_reason,
                })

        if winner is None:
            # Pathological skew only (a tiny drawable fraction exhausted the budget): roll
            # uniformly *by ticket* among the still-drawable awards, via the same hash
            # chain, so a fair winner is still guaranteed and the fallback is recorded.
            candidates = [
                row for row in snap.entries
                if row[1] in eligible and not (one_per and row[1] in won_users)
            ]
            candidate_total = sum(row[3] for row in candidates)
            if not candidates or candidate_total <= 0:
                break
            r2 = _hash_int(effective_seed, draw_index) % candidate_total
            draw_index += 1
            acc = 0
            for entry_id, uid, ticket_start, amount, draw_begin in candidates:
                if r2 < acc + amount:
                    winner = {
                        "entry_id": entry_id, "user_id": uid, "ticket_start": ticket_start,
                        "amount": amount, "draw_start": draw_begin,
                        "offset": draw_begin + (r2 - acc),
                        "ticket_no": ticket_start + (r2 - acc),
                    }
                    break
                acc += amount
            skipped.append({
                "draw_index": draw_index - 1, "value": r2, "prize_rank": prize.rank,
                "user_id": winner["user_id"], "name": pilots.get(winner["user_id"], {}).get("name", ""),
                "ticket_no": winner["ticket_no"],
                "reason": "fallback roll among drawable tickets (extreme ticket skew)",
            })

        uid = winner["user_id"]
        won_users.add(uid)
        info = pilots.get(uid, {})
        results.append(RaffleDrawResult(
            draw=locked, prize=prize, winner_user_id=uid,
            winner_character_id=info.get("character_id"),
            winner_character_name=info.get("name", ""), draw_order=order,
            winning_ticket_index=winner["offset"],
            winning_ticket_no=winner["ticket_no"],
            winning_ledger_entry_id=winner["entry_id"],
            winning_ticket_ref=f"ticket:{winner['ticket_no']}",
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
        "snapshot_version": snap.version,
        "snapshot_hash": snap.content_hash,
        # How many links of the hash chain this draw consumed. Authoritative for replay
        # and for continuing the chain on a replacement — ``random_values`` may be a
        # capped sample, this never is.
        "rolls_consumed": draw_index,
        "rolls_recorded": len(random_values),
        "snapshot_frozen_at": snap.frozen_at.isoformat(),
        "snapshot_cutoff_at": snap.cutoff_at.isoformat() if snap.cutoff_at else "",
        **totals,
        # Per-pilot ticket totals in the frozen pool. The per-award ranges live on the
        # snapshot itself (and are exported with the receipt) rather than being copied
        # here, so a large contest doesn't duplicate its whole pool into every draw row.
        "pool": [
            {"user_id": uid, "character_id": info["character_id"], "name": info["name"],
             "tickets": info["tickets"]}
            for uid, info in sorted(pilots.items())
        ],
        "prizes": [
            {"rank": p.rank, "name": p.name, "type": p.prize_type,
             "value": str(p.estimated_value)}
            for p in prizes
        ],
        "winners": [
            {"prize_rank": r.prize.rank, "user_id": r.winner_user_id,
             "character_id": r.winner_character_id, "name": r.winner_character_name,
             "winning_ticket": r.winning_ticket_no,
             "draw_offset": r.winning_ticket_index}
            for r in results
        ],
        "rules": snap.rules,
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


# --------------------------------------------------------------------------- #
#  Replacement winners (forfeit / disqualification)
# --------------------------------------------------------------------------- #
@transaction.atomic
def draw_replacement(forfeited: RaffleDrawResult, *, actor=None,
                     reason: str = "") -> RaffleDrawResult | None:
    """Draw a replacement for a forfeited result from the SAME frozen pool.

    The replacement continues the original draw's hash chain from where it stopped, so it
    verifies under exactly the same maths as the original picks — no second seed, no new
    pool, nothing for a sceptic to have to take on faith. Every roll it consumes is
    appended to the draw's record.

    Returns None when no eligible replacement exists (an exhausted or entirely ineligible
    pool), leaving the forfeit recorded and the prize unassigned.
    """
    draw = forfeited.draw
    snap = draw.snapshot
    if snap is None or snap.total_tickets <= 0:
        return None  # pre-snapshot draw: nothing to replay against

    contest = draw.contest
    one_per = contest.one_prize_per_pilot
    pilots = pool_snapshot.pilot_index(snap)
    starts = pool_snapshot.draw_starts(snap)
    total = snap.total_tickets

    # Eligibility is re-checked NOW: a replacement winner has to be eligible at the moment
    # they are awarded, not merely at the original draw.
    bulk = elig.for_users_bulk(contest, list(pilots.keys()))
    eligible = {uid for uid, e in bulk.items() if e.eligible}

    # Never re-award to the pilot who just forfeited, nor (under one-prize-per-pilot) to
    # anyone still holding a live win in this draw.
    blocked = {forfeited.winner_user_id}
    if one_per:
        blocked |= set(
            draw.results.filter(status=RaffleDrawResult.Status.WON)
            .values_list("winner_user_id", flat=True)
        )

    rolls = list(draw.random_values or [])
    # Continue from where the original draw stopped. The manifest's count is authoritative
    # — deriving it from the recorded rolls would reuse chain positions whenever those
    # rolls were capped, and a reused position is a rigged replacement.
    index = int(draw.manifest.get("rolls_consumed") or 0)
    if not index:
        used = [r["draw_index"] for r in rolls] + [
            s["draw_index"] for s in (draw.skipped_draws or []) if "draw_index" in s
        ]
        index = (max(used) + 1) if used else 0
    effective_seed = _effective_seed(draw.seed, draw.external_entropy)

    winner = None
    skips = list(draw.skipped_draws or [])
    for _ in range(total * 2 + _MAX_DRAW_ITERS_BUFFER):
        r = _hash_int(effective_seed, index) % total
        hit = pool_snapshot.resolve_offset(snap, r, starts=starts)
        index += 1
        if hit is None:
            break
        uid = hit["user_id"]
        roll = {"draw_index": index - 1, "value": r, "user_id": uid,
                "ticket_no": hit["ticket_no"], "prize_rank": forfeited.prize.rank,
                "replacement_for": forfeited.pk}
        if uid in blocked or uid not in eligible:
            if len(rolls) < _MAX_RECORDED_ROLLS:
                rolls.append(roll)
            if len(skips) < _MAX_RECORDED_ROLLS:
                skips.append({
                    **roll, "name": pilots.get(uid, {}).get("name", ""),
                    "reason": ("forfeited winner" if uid == forfeited.winner_user_id
                               else "already won a prize" if uid in blocked
                               else "not eligible at replacement time"),
                })
            continue
        rolls.append(roll)  # the decisive roll is always recorded
        winner = hit
        break

    draw.random_values = rolls
    draw.skipped_draws = skips
    # Advance the authoritative chain position so a second forfeit continues after these
    # rolls rather than replaying them.
    draw.manifest = {**(draw.manifest or {}), "rolls_consumed": index,
                     "rolls_recorded": len(rolls)}
    draw.save(update_fields=["random_values", "skipped_draws", "manifest", "updated_at"])
    if winner is None:
        return None

    info = pilots.get(winner["user_id"], {})
    next_order = (
        draw.results.order_by("-draw_order").values_list("draw_order", flat=True).first() or 0
    ) + 1
    return RaffleDrawResult.objects.create(
        draw=draw, prize=forfeited.prize, winner_user_id=winner["user_id"],
        winner_character_id=info.get("character_id"),
        winner_character_name=info.get("name", ""), draw_order=next_order,
        winning_ticket_index=winner["offset"], winning_ticket_no=winner["ticket_no"],
        winning_ledger_entry_id=winner["entry_id"],
        winning_ticket_ref=f"ticket:{winner['ticket_no']}",
        awarded_value=forfeited.awarded_value,
        replaces=forfeited,
        status_reason=reason[:300],
        status_changed_by=actor,
        status_changed_at=timezone.now(),
    )


# --------------------------------------------------------------------------- #
#  Verification
# --------------------------------------------------------------------------- #
def verify_draw(draw: RaffleDraw) -> dict:
    """Recompute a completed draw from its revealed seed + frozen pool and check it.

    This checks the three things that can actually go wrong, against the *persisted*
    records rather than against itself:

    * ``commitment_ok`` — the revealed seed hashes to the commitment published first.
    * ``snapshot_ok`` — the frozen pool still serialises to its published hash.
    * ``winners_match`` — replaying the hash chain over that pool reproduces exactly the
      :class:`~apps.raffle.models.RaffleDrawResult` rows on file, ticket numbers included.

    The last one is the point. The previous implementation compared ``random_values``
    against itself and never read the results table at all, so an edited winner row still
    reported "verified".
    """
    if draw.status != RaffleDraw.Status.COMPLETED or not draw.seed:
        return {"verifiable": False,
                "reason": gettext("draw not completed / seed not revealed")}

    commitment_ok = hashlib.sha256(draw.seed.encode()).hexdigest() == draw.seed_commitment
    snap = draw.snapshot
    if snap is None:
        # A draw taken before pools were frozen. Its seed commitment is still checkable,
        # but there is no recorded pool to replay against — say so rather than implying a
        # verification that never happened.
        return {
            "verifiable": False, "legacy": True, "commitment_ok": commitment_ok,
            "reason": gettext("This draw predates verifiable ticket pools, so the winners "
                              "cannot be independently recomputed."),
        }

    snap_report = pool_snapshot.verify_snapshot(snap)
    hash_ok = snap_report["hash_ok"] and (
        not draw.snapshot_hash or draw.snapshot_hash == snap.content_hash
    )

    total = snap.total_tickets
    recorded = list(draw.results.order_by("draw_order"))
    if total <= 0:
        return {
            "verifiable": True, "commitment_ok": commitment_ok, "snapshot_ok": hash_ok,
            "values_ok": True, "winners_match": not recorded, "checked_draws": 0,
            "snapshot": snap_report,
            "reason": gettext("no eligible tickets"),
        }

    starts = pool_snapshot.draw_starts(snap)
    effective_seed = _effective_seed(draw.seed, draw.external_entropy)

    # 1. Every stored roll really is the next link in the hash chain.
    values_ok = all(
        rv["value"] == _hash_int(effective_seed, rv["draw_index"]) % total
        for rv in draw.random_values
    )
    # 2. Every stored roll maps onto the ticket the record claims it did.
    tickets_ok = True
    for rv in draw.random_values:
        hit = pool_snapshot.resolve_offset(snap, rv["value"], starts=starts)
        if hit is None or hit["user_id"] != rv["user_id"]:
            tickets_ok = False
            break
        if "ticket_no" in rv and hit["ticket_no"] != rv["ticket_no"]:
            tickets_ok = False
            break

    # 3. Replaying the chain reproduces the winners actually on file.
    #
    # The extreme-skew fallback roll is not part of the main chain (it re-rolls against a
    # different modulus over just the drawable awards), so a draw that used it cannot be
    # replayed by this routine. Say so explicitly rather than reporting a mismatch that
    # would look like tampering — the fallback itself is recorded in ``skipped_draws``.
    used_fallback = any(
        str(s.get("reason", "")).startswith("fallback") for s in (draw.skipped_draws or [])
    )
    replay = [] if used_fallback else _replay_winners(draw, snap, starts, effective_seed, total)
    # Compare against what the CHAIN produced, not against who currently holds the prize:
    # a later forfeit turns an original pick into a FORFEITED row and adds a replacement
    # drawn further along the chain. Both are recorded and both verify — but the original
    # sequence is what replaying the seed reproduces, so that is what must match.
    actual = [
        (r.prize_id, r.winner_user_id, r.winning_ticket_no)
        for r in recorded if r.replaces_id is None
    ]
    winners_match = None if used_fallback else (replay == actual)

    return {
        "verifiable": True,
        "commitment_ok": commitment_ok,
        "snapshot_ok": hash_ok,
        "values_ok": values_ok,
        "tickets_ok": tickets_ok,
        "winners_match": winners_match,
        "replay_partial": used_fallback,
        "checked_draws": len(draw.random_values),
        "snapshot": snap_report,
        "expected_winners": replay,
    }


def _replay_winners(draw, snap, starts, effective_seed, total) -> list[tuple]:
    """Re-run the recorded chain and return ``[(prize_id, user_id, ticket_no)]``.

    Replays from the draw's own record of which pilots were eligible (the frozen
    per-pilot eligibility snapshots), so the replay is reproducible later even after a
    pilot's live ESI state has moved on.
    """
    eligible = set(
        draw.eligibility_snapshots.filter(eligible=True).values_list("user_id", flat=True)
    )
    one_per = bool(draw.manifest.get("one_prize_per_pilot", True))
    # Replacement results are drawn later from the same chain; the original sequence is
    # what this routine reproduces.
    prize_ids = [
        r.prize_id for r in draw.results.filter(replaces__isnull=True).order_by("draw_order")
    ]
    if not prize_ids or total <= 0:
        return []

    # Replay the chain itself from the start; ``random_values`` may be a capped sample and
    # must never bound the replay.
    limit = int(draw.manifest.get("rolls_consumed") or 0)
    if not limit and draw.random_values:
        limit = max(rv["draw_index"] for rv in draw.random_values) + 1
    if not limit:
        return []

    out: list[tuple] = []
    won: set[int] = set()
    index = 0
    for prize_id in prize_ids:
        winner = None
        while index < limit:
            value = _hash_int(effective_seed, index) % total
            index += 1
            hit = pool_snapshot.resolve_offset(snap, value, starts=starts)
            if hit is None:
                break
            uid = hit["user_id"]
            if uid not in eligible or (one_per and uid in won):
                continue
            winner = hit
            break
        if winner is None:
            break
        won.add(winner["user_id"])
        out.append((prize_id, winner["user_id"], winner["ticket_no"]))
    return out
