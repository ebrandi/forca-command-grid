"""Backfill ``Killmail.doctrine_fit`` for home-corp losses (KB-13).

Inline tagging only covers newly-ingested mails, so run this once after deploying
doctrine tagging — and again after doctrines change — to (re)tag historical
losses. Idempotent and safe to re-run.

    manage.py retag_doctrine_fits                # whole board (home-corp losses)
    manage.py retag_doctrine_fits --limit 5000   # most-recent N (spot check)
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.killboard.doctrine_tag import compute_fit_deviation, tag_doctrine_fit
from apps.killboard.models import Killmail


class Command(BaseCommand):
    help = "Backfill Killmail.doctrine_fit for home-corp losses (KB-13)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--limit", type=int, default=None,
                            help="Only retag the most recent N home-corp losses.")

    def handle(self, *args, **options) -> None:
        # Delegate to the SAME module-aware path ingest uses (4.2) so the backfill can
        # never diverge from ingest / re-runs never clobber a module-aware tag with a
        # hull-only one. Items are prefetched (chunk_size keeps prefetch alive under
        # iterator()) so the module match + deviation don't N+1 over the whole board.
        qs = (
            Killmail.objects.filter(home_corp_role=Killmail.HomeRole.VICTIM)
            .prefetch_related("items")
            .order_by("-killmail_time")
        )
        if options["limit"]:
            qs = qs[: options["limit"]]

        tagged = cleared = 0
        for km in qs.iterator(chunk_size=500):
            before = km.doctrine_fit_id
            tag_doctrine_fit(km)  # module-aware, writes only on change
            if km.doctrine_fit_id != before:
                if km.doctrine_fit_id is None:
                    cleared += 1
                else:
                    tagged += 1
            compute_fit_deviation(km)  # KB-14: (re)build the deviation, or clear if untagged

        self.stdout.write(self.style.SUCCESS(f"Doctrine-tagged {tagged} losses ({cleared} cleared)."))
