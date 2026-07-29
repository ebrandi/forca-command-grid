"""Pre-draw validation — what leadership must see before committing a seed.

Every check returns one of three states, and the distinction matters operationally:

* ``critical`` — the draw is blocked. Running anyway would produce a result that cannot be
  defended afterwards (no frozen pool, no tickets, a pool that no longer matches its own
  fingerprint).
* ``warning`` — the draw is allowed but someone should look first, and the acknowledgement
  is recorded in the audit log rather than being silently clicked through.
* ``ok`` — nothing to say.

The checks are deliberately read-only. Nothing here freezes, fixes or approves anything: a
validator that quietly repairs the thing it is validating is how a broken pool gets drawn
against and nobody notices.
"""
from __future__ import annotations

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from . import snapshot as pool_snapshot
from . import tickets as ticket_ids
from .models import (
    RaffleContest,
    RaffleSuspiciousActivityFlag,
    RaffleTicketLedgerEntry,
)

CRITICAL = "critical"
WARNING = "warning"
OK = "ok"


def _check(key, state, label, detail="") -> dict:
    return {"key": key, "state": state, "label": label, "detail": detail,
            "passed": state == OK}


def draw_checklist(contest) -> dict:
    """Full pre-draw validation for ``contest``.

    Returns ``{"checks": [...], "blocked": bool, "warnings": int, "critical": int}``.
    """
    now = timezone.now()
    checks: list[dict] = []

    # --- configuration ---------------------------------------------------- #
    prizes = list(contest.prizes.order_by("rank"))
    if not prizes:
        checks.append(_check("prizes", CRITICAL, _("Prizes configured"),
                             _("This contest has no prizes, so there is nothing to draw.")))
    else:
        ranks = [p.rank for p in prizes]
        if sorted(ranks) != list(range(1, len(ranks) + 1)):
            checks.append(_check(
                "prize_ranks", WARNING, _("Prize ranks are sequential"),
                _("Prize ranks are %(ranks)s — they should run 1, 2, 3… so the draw order "
                  "reads the way pilots expect.") % {"ranks": ", ".join(str(r) for r in ranks)}))
        else:
            checks.append(_check("prizes", OK, _("Prizes configured"),
                                 _("%(n)s prizes.") % {"n": len(prizes)}))

    if contest.draw_at < contest.end_at:
        checks.append(_check(
            "schedule", WARNING, _("Draw time is after the ticket cutoff"),
            _("The draw is scheduled before accrual ends, so tickets earned in between "
              "could not take part.")))

    # --- lifecycle -------------------------------------------------------- #
    closed = contest.status in (RaffleContest.Status.CLOSED, RaffleContest.Status.COMPLETED)
    # Closing IS the cutoff — accrual stops and the pool freezes on the transition, so a
    # contest that leadership closed early is legitimately drawable and this check passes.
    # The wall-clock end time only matters while the contest is still open, and that case
    # is already blocked by the "closed" check below.
    checks.append(_check(
        "cutoff", OK if (closed or contest.end_at <= now) else CRITICAL,
        _("Ticket accrual has stopped"),
        "" if (closed or contest.end_at <= now) else _("Accrual is still open until %(when)s.")
        % {"when": contest.end_at.strftime("%Y-%m-%d %H:%M")}))

    checks.append(_check(
        "closed", OK if closed else CRITICAL, _("Contest is closed"),
        "" if closed else _("Close the contest to freeze its ticket pool before drawing.")))

    # --- ticket processing ------------------------------------------------ #
    unprocessed = [
        c.source_key for c in contest.source_configs.filter(enabled=True)
        if c.last_processed_at is None or c.last_processed_at < contest.end_at
    ]
    if unprocessed:
        checks.append(_check(
            "sources", WARNING, _("Every source has been swept past the cutoff"),
            _("Not swept since the cutoff: %(sources)s. Tickets earned near the end may "
              "be missing.") % {"sources": ", ".join(sorted(unprocessed))}))
    else:
        checks.append(_check("sources", OK, _("Every source has been swept past the cutoff")))

    pending = RaffleTicketLedgerEntry.objects.filter(
        contest=contest, status=RaffleTicketLedgerEntry.Status.PENDING).count()
    if pending:
        checks.append(_check(
            "pending", WARNING, _("No tickets awaiting approval"),
            _("%(n)s awards are still pending officer approval and will NOT be in the "
              "draw.") % {"n": pending}))
    else:
        checks.append(_check("pending", OK, _("No tickets awaiting approval")))

    unnumbered = ticket_ids.unnumbered_count(contest)
    checks.append(_check(
        "numbering", OK if not unnumbered else CRITICAL, _("Every ticket has a number"),
        "" if not unnumbered else _("%(n)s awards have no ticket number yet and would be "
                                    "left out of the pool.") % {"n": unnumbered}))

    # An override grant to a not-yet-enrolled pilot has no account, so it can never be
    # drawn. Surfacing it stops a leader believing they awarded drawable tickets.
    orphan = RaffleTicketLedgerEntry.objects.filter(
        contest=contest, status=RaffleTicketLedgerEntry.Status.APPROVED,
        amount__gt=0, user__isnull=True).count()
    if orphan:
        checks.append(_check(
            "orphan_tickets", WARNING, _("Every approved ticket belongs to an account"),
            _("%(n)s approved awards have no FORCA account attached and cannot win.")
            % {"n": orphan}))

    # --- integrity -------------------------------------------------------- #
    open_flags = contest.suspicious_flags.filter(
        status=RaffleSuspiciousActivityFlag.Status.OPEN).count()
    if open_flags:
        checks.append(_check(
            "flags", WARNING, _("No unreviewed integrity flags"),
            _("%(n)s flagged ticket events are still open. Review them before drawing — "
              "afterwards the only remedy is a redraw.") % {"n": open_flags}))
    else:
        checks.append(_check("flags", OK, _("No unreviewed integrity flags")))

    # --- the pool --------------------------------------------------------- #
    snap = pool_snapshot.current_snapshot(contest)
    if snap is None:
        checks.append(_check(
            "pool", CRITICAL, _("Ticket pool is frozen"),
            _("The pool has not been frozen. Closing the contest freezes it and publishes "
              "its fingerprint.")))
        eligible_pilots = 0
    else:
        report = pool_snapshot.verify_snapshot(snap)
        if not report["hash_ok"]:
            checks.append(_check(
                "pool", CRITICAL, _("Ticket pool is frozen"),
                _("The frozen pool no longer matches its published fingerprint. Do not "
                  "draw — re-freeze the pool and record why.")))
        elif not report["ledger_matches"]:
            checks.append(_check(
                "pool", WARNING, _("Ticket pool is frozen"),
                _("The ledger has changed since the pool was frozen (v%(v)s, %(n)s "
                  "tickets). The draw will use the frozen pool.")
                % {"v": snap.version, "n": snap.total_tickets}))
        else:
            checks.append(_check(
                "pool", OK, _("Ticket pool is frozen"),
                _("v%(v)s · %(n)s tickets · %(hash)s")
                % {"v": snap.version, "n": snap.total_tickets, "hash": snap.short_hash}))

        has_tickets = snap.total_tickets > 0
        checks.append(_check(
            "tickets", OK if has_tickets else CRITICAL, _("The pool has tickets in it"),
            "" if has_tickets else _("No approved tickets were earned, so there is nobody "
                                     "to draw.")))
        eligible_pilots = sum(1 for p in snap.pilots if p[4])

    # --- winner feasibility ----------------------------------------------- #
    if prizes and contest.one_prize_per_pilot and eligible_pilots < len(prizes):
        checks.append(_check(
            "pilot_count", WARNING, _("Enough pilots for one prize each"),
            _("%(pilots)s eligible pilots for %(prizes)s prizes, and this contest allows "
              "only one prize per pilot — the remaining prizes will go unawarded.")
            % {"pilots": eligible_pilots, "prizes": len(prizes)}))
    elif prizes:
        checks.append(_check("pilot_count", OK, _("Enough pilots for one prize each")))

    critical = sum(1 for c in checks if c["state"] == CRITICAL)
    warnings = sum(1 for c in checks if c["state"] == WARNING)
    return {
        "checks": checks,
        "critical": critical,
        "warnings": warnings,
        "blocked": critical > 0,
        "snapshot": snap,
    }
