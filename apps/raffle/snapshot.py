"""The frozen, hashed ticket pool a draw runs against.

Why a snapshot exists at all
----------------------------
The seed commitment published before a draw proves the *seed* was not swapped afterwards.
It proves nothing about the *pool*. Before this module, the draw read the live ledger at
execution time, which could be days after the seed was generated and stored — so anyone
who could read the seed could compute the outcome in advance and then change it by
disqualifying a single ledger entry: every later ticket range shifts down, a different
pilot sits under the winning offset, and the commitment still verifies perfectly.

Freezing the pool and publishing its SHA-256 *before* the draw closes that hole. After the
freeze both halves of the input — pool and seed — are public and fixed, so the outcome is
already determined and can only be checked, not chosen.

Positions never move
--------------------
The snapshot fixes a contiguous **draw space**: offset 0 to ``total_tickets - 1``, with
each award occupying ``[draw_start, draw_start + amount)``. Ticket *numbers* may have gaps
(a reversed award keeps its numbers — see :mod:`apps.raffle.tickets`), so draw space and
ticket-number space are different coordinate systems and the snapshot records the mapping.

Eligibility is deliberately **not** baked into these positions. A pilot who loses their
ESI token between the freeze and the draw must still forfeit — the corp rule is "enrolled
and valid at draw time to win" — but making that removal shrink the pool would reopen the
manipulation hole from the other side. Instead an ineligible pilot's tickets stay in place
and are *skipped* when drawn, which is recorded in the draw's ``skipped_draws``. The
consequence is the property we want: excluding a pilot after the freeze can only cost that
pilot their win. It cannot move the win to a chosen someone else, because nobody else's
position changes.
"""
from __future__ import annotations

import bisect
import hashlib
import json

from django.db import transaction
from django.utils import timezone

from .models import (
    RaffleContest,
    RaffleTicketLedgerEntry,
    RaffleTicketPoolSnapshot,
)

# Bump only on a breaking change to the canonical serialisation below — the string is
# part of the hashed payload, so an old receipt keeps verifying under its own format.
SNAPSHOT_FORMAT = "forca-raffle-pool-v1"


# --------------------------------------------------------------------------- #
#  Canonical serialisation + hash
# --------------------------------------------------------------------------- #
def canonical_payload(*, contest_id: int, slug: str, version: int, cutoff_at,
                      frozen_at, algorithm_version: str, rules: dict,
                      total_tickets: int, entries: list) -> str:
    """The exact bytes the content hash is taken over.

    Deliberately line-oriented and boring: a pilot with a text editor and ``sha256sum``
    must be able to reproduce it from the published receipt. Sorted-key JSON for the
    rules block keeps dict ordering out of the hash.
    """
    head = [
        SNAPSHOT_FORMAT,
        f"contest={contest_id}",
        f"slug={slug}",
        f"version={version}",
        f"cutoff={cutoff_at.isoformat() if cutoff_at else ''}",
        f"frozen={frozen_at.isoformat() if frozen_at else ''}",
        f"algorithm={algorithm_version}",
        f"rules={json.dumps(rules, sort_keys=True, separators=(',', ':'))}",
        f"tickets={total_tickets}",
        f"entries={len(entries)}",
        "--",
    ]
    body = [",".join(str(v) for v in row) for row in entries]
    return "\n".join(head + body) + "\n"


def content_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def payload_for(snapshot: RaffleTicketPoolSnapshot) -> str:
    """Rebuild the canonical payload from a stored snapshot (for re-verification)."""
    return canonical_payload(
        contest_id=snapshot.contest_id,
        slug=snapshot.rules.get("slug", ""),
        version=snapshot.version,
        cutoff_at=snapshot.cutoff_at,
        frozen_at=snapshot.frozen_at,
        algorithm_version=snapshot.algorithm_version,
        rules=snapshot.rules,
        total_tickets=snapshot.total_tickets,
        entries=snapshot.entries,
    )


# --------------------------------------------------------------------------- #
#  Freezing
# --------------------------------------------------------------------------- #
def _rules_for(contest) -> dict:
    """The contest rules frozen into the snapshot (and therefore into its hash)."""
    return {
        "slug": contest.slug,
        "name": contest.name,
        "one_prize_per_pilot": contest.one_prize_per_pilot,
        "require_enrolled": contest.require_enrolled,
        "require_valid_token": contest.require_valid_token,
        "include_alliance": contest.include_alliance,
        "retroactive_enabled": contest.retroactive_enabled,
        "prize_count": contest.prizes.count(),
        "start_at": contest.start_at.isoformat() if contest.start_at else "",
        "end_at": contest.end_at.isoformat() if contest.end_at else "",
    }


@transaction.atomic
def freeze_pool(contest, *, actor=None, supersede_reason: str = "") -> RaffleTicketPoolSnapshot:
    """Freeze the current drawable ticket pool into a new, hashed snapshot version.

    Numbering is completed first: an award without a ticket number cannot be placed in the
    pool, and silently dropping it would understate the pool. Any existing current
    snapshot is superseded — which is the audited "the pool was reopened" event, never a
    silent overwrite.
    """
    from . import eligibility as elig
    from . import tickets as ticket_ids

    locked = RaffleContest.objects.select_for_update().get(pk=contest.pk)
    ticket_ids.assign_ticket_numbers(locked)

    previous = (
        RaffleTicketPoolSnapshot.objects
        .filter(contest=locked, superseded_by__isnull=True)
        .order_by("-version").first()
    )
    highest = (
        RaffleTicketPoolSnapshot.objects.filter(contest=locked)
        .order_by("-version").values_list("version", flat=True).first()
    )
    version = (highest or 0) + 1

    rows = list(
        RaffleTicketLedgerEntry.objects
        .filter(contest=locked, status=RaffleTicketLedgerEntry.Status.APPROVED,
                amount__gt=0, ticket_start__isnull=False, user__isnull=False)
        .order_by("ticket_start")
        .values("id", "user_id", "character_id", "character_name", "ticket_start", "amount")
    )

    entries: list[list[int]] = []
    per_user: dict[int, dict] = {}
    draw_start = 0
    for r in rows:
        entries.append([r["id"], r["user_id"], r["ticket_start"], r["amount"], draw_start])
        bucket = per_user.setdefault(r["user_id"], {
            "character_id": r["character_id"], "name": r["character_name"], "tickets": 0,
        })
        bucket["tickets"] += r["amount"]
        draw_start += r["amount"]
    total_tickets = draw_start

    # Eligibility AS OF THE FREEZE — recorded for transparency, not used to position
    # tickets (see the module docstring). The draw re-checks at execution time.
    bulk = elig.for_users_bulk(locked, list(per_user.keys()))
    pilots: list[list] = []
    excluded_tickets = excluded_pilots = 0
    exclusion_summary: dict[str, int] = {}
    for uid, bucket in sorted(per_user.items()):
        e = bulk.get(uid)
        eligible = bool(e and e.eligible)
        reason = "" if eligible else ((e.reason_code if e else "") or "ineligible")
        pilots.append([uid, bucket["character_id"], bucket["name"], bucket["tickets"],
                       eligible, reason])
        if not eligible:
            excluded_tickets += bucket["tickets"]
            excluded_pilots += 1
            exclusion_summary[reason] = exclusion_summary.get(reason, 0) + 1

    frozen_at = timezone.now()
    rules = _rules_for(locked)
    payload = canonical_payload(
        contest_id=locked.pk, slug=locked.slug, version=version,
        cutoff_at=locked.end_at, frozen_at=frozen_at,
        algorithm_version=locked.algorithm_version, rules=rules,
        total_tickets=total_tickets, entries=entries,
    )

    snapshot = RaffleTicketPoolSnapshot.objects.create(
        contest=locked, version=version, frozen_at=frozen_at, cutoff_at=locked.end_at,
        frozen_by=actor, total_tickets=total_tickets, total_entries=len(entries),
        total_pilots=len(pilots), excluded_tickets=excluded_tickets,
        excluded_pilots=excluded_pilots, entries=entries, pilots=pilots, rules=rules,
        exclusion_summary=exclusion_summary,
        algorithm_version=locked.algorithm_version, content_hash=content_hash(payload),
    )
    if previous is not None:
        previous.superseded_by = snapshot
        previous.supersede_reason = supersede_reason or "pool re-frozen"
        previous.save(update_fields=["superseded_by", "supersede_reason", "updated_at"])
    return snapshot


def current_snapshot(contest) -> RaffleTicketPoolSnapshot | None:
    """The live snapshot for a contest, or None if the pool has never been frozen."""
    return (
        RaffleTicketPoolSnapshot.objects
        .filter(contest=contest, superseded_by__isnull=True)
        .order_by("-version").first()
    )


def ensure_snapshot(contest, *, actor=None) -> RaffleTicketPoolSnapshot:
    """The current snapshot, freezing one now if the pool was never frozen."""
    return current_snapshot(contest) or freeze_pool(contest, actor=actor)


# --------------------------------------------------------------------------- #
#  Reading a snapshot
# --------------------------------------------------------------------------- #
def draw_starts(snapshot) -> list[int]:
    """The ordered draw-space offsets, for bisecting an offset onto its award."""
    return [row[4] for row in snapshot.entries]


def resolve_offset(snapshot, offset: int, *, starts: list[int] | None = None) -> dict | None:
    """Map a draw-space offset onto the exact ticket that occupies it.

    Returns the owning award, the account, and — the part that was missing before — the
    *permanent ticket number* that was actually drawn, rather than the winner's first
    ticket.
    """
    if not snapshot.entries or offset < 0 or offset >= snapshot.total_tickets:
        return None
    starts = starts if starts is not None else draw_starts(snapshot)
    row = snapshot.entries[bisect.bisect_right(starts, offset) - 1]
    entry_id, user_id, ticket_start, amount, draw_begin = row
    return {
        "entry_id": entry_id,
        "user_id": user_id,
        "ticket_start": ticket_start,
        "amount": amount,
        "draw_start": draw_begin,
        "offset": offset,
        # The winning ticket = the same distance into the award's ticket range as the
        # rolled offset is into its draw range.
        "ticket_no": ticket_start + (offset - draw_begin),
    }


def pilot_index(snapshot) -> dict[int, dict]:
    """``user_id -> {character_id, name, tickets, eligible_at_freeze, reason}``."""
    out = {}
    for uid, char_id, name, count, eligible, reason in snapshot.pilots:
        out[uid] = {"character_id": char_id, "name": name, "tickets": count,
                    "eligible_at_freeze": eligible, "reason": reason}
    return out


def tickets_for_user(snapshot, user_id: int) -> list[list[int]]:
    """This account's rows in the snapshot, so a pilot can see exactly which of their
    tickets entered the draw."""
    return [row for row in snapshot.entries if row[1] == user_id]


# --------------------------------------------------------------------------- #
#  Verification
# --------------------------------------------------------------------------- #
def verify_snapshot(snapshot) -> dict:
    """Check a stored snapshot against its own hash and against the live ledger.

    ``hash_ok`` false means the stored pool no longer serialises to the published hash —
    i.e. the row was edited after freezing. ``ledger_matches`` false is *not* necessarily
    wrongdoing (an audited post-draw correction moves the ledger on), but it must be
    surfaced rather than hidden, so leadership can explain the difference.
    """
    payload = payload_for(snapshot)
    hash_ok = content_hash(payload) == snapshot.content_hash

    live = {
        r["id"]: (r["ticket_start"], r["amount"])
        for r in RaffleTicketLedgerEntry.objects.filter(
            contest_id=snapshot.contest_id,
            status=RaffleTicketLedgerEntry.Status.APPROVED,
            amount__gt=0, ticket_start__isnull=False, user__isnull=False,
        ).values("id", "ticket_start", "amount")
    }
    snap = {row[0]: (row[2], row[3]) for row in snapshot.entries}
    added = sorted(set(live) - set(snap))
    removed = sorted(set(snap) - set(live))
    changed = sorted(k for k in set(snap) & set(live) if snap[k] != live[k])

    return {
        "hash_ok": hash_ok,
        "content_hash": snapshot.content_hash,
        "total_tickets": snapshot.total_tickets,
        "total_entries": snapshot.total_entries,
        "ledger_matches": not (added or removed or changed),
        "entries_added_since": added,
        "entries_removed_since": removed,
        "entries_changed_since": changed,
    }
