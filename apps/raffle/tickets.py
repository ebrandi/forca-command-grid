"""Permanent ticket identity — allocation, lookup and pilot-facing grouping.

A ticket is not a row. Materialising one row per ticket would turn a single 100-ticket
solo kill into 100 inserts and a busy contest into millions of rows, for no gain: the
award event already records everything true about those tickets. Instead each award owns
a contiguous, half-open range ``[ticket_start, ticket_start + amount)`` allocated from the
contest's monotonic counter.

Two properties make the numbers trustworthy:

* **Append-only.** Numbers are handed out in ledger-``id`` order, and ``id`` is monotonic
  and immutable, so a newly-swept award always lands *after* every existing one. A pilot
  who was shown "#412–#511" yesterday still owns exactly those tickets tomorrow, no matter
  what anyone else earns in between.
* **Never reused.** A reversed or disqualified award keeps its numbers; its tickets simply
  stop being drawable. The gap is deliberate — it is the visible evidence that a
  correction happened, and reclaiming the range would silently renumber everything after
  it, which is the one thing that would make a pilot's screenshot a lie.

Because invalidated tickets leave gaps, ticket-number space is *not* contiguous. The draw
therefore runs over a second, contiguous "draw space" built at freeze time
(:mod:`apps.raffle.snapshot`), and the snapshot records the mapping between the two.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import F, Q, Sum

from .models import RaffleContest, RaffleTicketLedgerEntry


@transaction.atomic
def assign_ticket_numbers(contest) -> int:
    """Give every unnumbered award in ``contest`` its permanent ticket range.

    Idempotent and safe to call from any write path or beat: rows that already carry a
    ``ticket_start`` are untouched. The contest row is locked for the allocation so two
    concurrent sweeps can never hand out overlapping ranges (the DB's
    ``uniq_raffle_ticket_start`` is the backstop if one ever tries).

    Returns the number of awards numbered.
    """
    locked = RaffleContest.objects.select_for_update().get(pk=contest.pk)
    next_no = locked.next_ticket_number or 1

    # Order by id, never by occurred_at: a retroactive award can be back-dated, and
    # ordering by activity time would insert it *between* existing tickets and renumber
    # every ticket after it.
    rows = list(
        RaffleTicketLedgerEntry.objects
        .filter(contest=locked, ticket_start__isnull=True, amount__gt=0)
        .order_by("id")
        .only("id", "amount")
    )
    if not rows:
        return 0

    for row in rows:
        row.ticket_start = next_no
        next_no += row.amount

    RaffleTicketLedgerEntry.objects.bulk_update(rows, ["ticket_start"], batch_size=500)
    locked.next_ticket_number = next_no
    locked.save(update_fields=["next_ticket_number", "updated_at"])
    # Keep the caller's in-memory instance honest — callers often re-read the counter.
    contest.next_ticket_number = next_no
    return len(rows)


def unnumbered_count(contest) -> int:
    """Awards still waiting for a number — a pre-draw validation check."""
    return RaffleTicketLedgerEntry.objects.filter(
        contest=contest, ticket_start__isnull=True, amount__gt=0
    ).count()


def entry_owning_ticket(contest, number: int) -> RaffleTicketLedgerEntry | None:
    """The award that owns ticket ``number``, or None if no ticket has that number.

    A single indexed range scan: the owning award is the one with the greatest
    ``ticket_start`` not exceeding ``number``, provided its range actually reaches it.
    """
    if number is None or number < 1:
        return None
    candidate = (
        RaffleTicketLedgerEntry.objects
        .filter(contest=contest, ticket_start__isnull=False, ticket_start__lte=number)
        .order_by("-ticket_start")
        .first()
    )
    if candidate is None or not candidate.owns_ticket(number):
        return None
    return candidate


def ticket_count(contest, *, drawable_only: bool = True) -> int:
    """Total tickets in the contest — drawable ones by default."""
    qs = RaffleTicketLedgerEntry.objects.filter(contest=contest, amount__gt=0)
    if drawable_only:
        qs = qs.filter(status=RaffleTicketLedgerEntry.Status.APPROVED)
    return qs.aggregate(n=Sum("amount"))["n"] or 0


def evidence_for(entry) -> dict:
    """The checkable evidence behind one award, for the pilot's own ticket ledger.

    Only what the owner is entitled to see and can act on: what the activity was, when,
    and a link to the record it came from. Internal notes, other pilots' account ids and
    the raw eligibility snapshot are deliberately not surfaced here — the leadership
    ledger view is where those belong.
    """
    meta = entry.metadata or {}
    kind, _, ref = (entry.source_ref or "").partition(":")
    out = {
        "kind": kind or entry.source_key,
        "ref": ref,
        "url": "",
        "killmail_id": None,
        "final_blow": bool(meta.get("final_blow")),
        "solo": bool(meta.get("solo")),
        "value": meta.get("value"),
        "manual": entry.source_key == "manual",
        "granted_by": "",
        "grant_reason": "",
        "category": meta.get("category") or "",
        "override": bool(meta.get("override")),
        # How the final ticket count was reached — see apps.raffle.engine.
        "multiplier": meta.get("booster_multiplier"),
        "base_tickets": meta.get("base_tickets"),
        "capped": bool(meta.get("capped")),
        "cap_amount": meta.get("cap_amount"),
    }

    if kind == "killmail" and ref.isdigit():
        from django.urls import reverse

        out["killmail_id"] = int(ref)
        try:
            out["url"] = reverse("killboard:detail", args=[int(ref)])
        except Exception:  # noqa: BLE001 — a missing route must not break the ledger
            out["url"] = ""

    if out["manual"]:
        grant = getattr(entry, "manual_grant", None)
        if grant is not None:
            # Who granted it and why is exactly the information that makes a manual
            # ticket trustworthy rather than mysterious, so the owner sees both. The
            # grant's ``internal_notes`` stay leadership-only.
            granter = grant.granted_by
            # display_name, never username: usernames are opaque ``eve:<id>`` strings, so
            # showing one would name the granting officer in a form no pilot recognises.
            out["granted_by"] = getattr(granter, "display_name", "") if granter else ""
            out["grant_reason"] = grant.reason
            out["category"] = grant.category or out["category"]
    return out


def pilot_entries(contest, user, *, source_key: str = "", status: str = "",
                  ticket_no: int | None = None):
    """The pilot's own award events, newest first, with the ledger's filters applied.

    Returns a queryset (not a list) so the caller can paginate it — a pilot with
    thousands of tickets must never have their whole history materialised to render one
    page. ``ticket_no`` narrows to the single award owning that number.
    """
    qs = (
        RaffleTicketLedgerEntry.objects
        .filter(contest=contest, user=user)
        .select_related("manual_grant", "manual_grant__granted_by")
        # display_name walks the granter's characters; without this every manual ticket
        # on the page would cost its own query.
        .prefetch_related("manual_grant__granted_by__characters")
        .order_by("-id")
    )
    if source_key:
        qs = qs.filter(source_key=source_key)
    if status:
        qs = qs.filter(status=status)
    if ticket_no is not None:
        # The owning award is the one whose range covers the number: start <= n < end.
        # Expressed against stored columns so it stays a single indexed query.
        qs = qs.filter(
            Q(ticket_start__lte=ticket_no) & Q(ticket_start__gt=ticket_no - F("amount"))
        )
    return qs
