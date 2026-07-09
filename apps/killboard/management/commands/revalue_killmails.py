"""Re-value killmails with the current pricing/valuation logic, then rebuild the
combat-metric rollups so ISK-based rankings reflect the new values.

Run after refreshing prices (``price_types`` / ``import_adjusted_prices``) or after
any change to the valuation engine (e.g. the blueprint-copy fix that bumped
``VALUATION_VERSION``). Idempotent and safe to re-run.

    manage.py revalue_killmails                 # whole board, then rebuild metrics
    manage.py revalue_killmails --limit 5000    # most-recent N (spot check)
    manage.py revalue_killmails --no-rebuild    # values only, skip metric rebuild
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.killboard.models import Killmail
from apps.killboard.stats import rebuild_corp_metrics, rebuild_member_metrics
from apps.killboard.valuation import apply_valuation
from apps.market.pricing import build_price_index


class Command(BaseCommand):
    help = "Re-value killmails and rebuild combat-metric rollups."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--limit", type=int, default=None,
                            help="Only re-value the most recent N killmails.")
        parser.add_argument("--no-rebuild", action="store_true",
                            help="Skip the CombatMetric rebuild step.")

    def handle(self, *args, **options) -> None:
        qs = Killmail.objects.order_by("-killmail_time")
        if options["limit"]:
            qs = qs[: options["limit"]]
        total = qs.count()
        self.stdout.write(f"Re-valuing {total:,} killmails…")

        # One in-memory price snapshot for the whole pass — avoids a per-item DB
        # lookup across millions of items.
        price_lookup = build_price_index()
        done = 0
        # iterator() to avoid loading the whole table; valuation re-reads each
        # killmail's items, so memory stays flat regardless of board size.
        for km in qs.iterator(chunk_size=500):
            apply_valuation(km, price_lookup)
            done += 1
            if done % 5000 == 0:
                self.stdout.write(f"  …{done:,}/{total:,}")
        self.stdout.write(self.style.SUCCESS(f"Re-valued {done:,} killmails."))

        if options["no_rebuild"]:
            self.stdout.write("Skipped metric rebuild (--no-rebuild).")
            return

        corp = rebuild_corp_metrics()
        members = rebuild_member_metrics()
        self.stdout.write(self.style.SUCCESS(
            f"Rebuilt {corp} corp window(s) and {members} member metric row(s)."
        ))
