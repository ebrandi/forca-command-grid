"""Backfill blank ``character_name`` on raffle ledger + ineligible-activity rows.

The PVP source now stamps a resolved name on every event, but rows written before
that fix (and any whose name could not be resolved at write time) can carry an
empty ``character_name`` — which makes the ineligible-activity *outreach* list
unusable. This one-off, idempotent, DB-only pass fills those blanks from the
resolved-name table / corp roster (see :func:`core.esi.names.names_for`). Safe to
re-run; ``--dry-run`` reports what would change without writing.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from core.esi.names import names_for


class Command(BaseCommand):
    help = "Resolve blank character_name on raffle ledger + ineligible-activity rows (DB-only)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report how many rows would be filled without writing.",
        )

    def handle(self, *args, **opts):
        from apps.raffle.models import RaffleIneligibleActivity, RaffleTicketLedgerEntry

        dry = opts["dry_run"]
        grand_total = 0
        for model in (RaffleIneligibleActivity, RaffleTicketLedgerEntry):
            # character_name is a non-nullable CharField, so a blank is always "".
            # Evaluate the set once (a one-off command over a bounded set of rows).
            blank = list(
                model.objects.filter(character_name="").only("id", "character_id", "character_name")
            )
            if not blank:
                self.stdout.write(f"{model.__name__}: no blank names.")
                continue
            names = names_for({row.character_id for row in blank})
            to_update = []
            for row in blank:
                name = names.get(row.character_id)
                if name:
                    row.character_name = name
                    to_update.append(row)
            if to_update and not dry:
                model.objects.bulk_update(to_update, ["character_name"], batch_size=500)
            blank_count = len(blank)
            grand_total += len(to_update)
            self.stdout.write(
                f"{model.__name__}: {len(to_update)}/{blank_count} blank rows "
                f"{'would be ' if dry else ''}resolved "
                f"({blank_count - len(to_update)} still unknown)."
            )
        self.stdout.write(self.style.SUCCESS(
            f"Done. {grand_total} row(s) {'would be ' if dry else ''}updated."
        ))
