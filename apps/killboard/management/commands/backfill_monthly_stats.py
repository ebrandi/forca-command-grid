"""Backfill the per-pilot, per-month PvP aggregate (``MonthlyPilotKillStat``).

Powers fast historical rankings (month / year) on ``/killboard/rankings/``. Safe to
run more than once — each month is recomputed and upserted (idempotent), and the run
is resumable a month at a time.

    manage.py backfill_monthly_stats                 # earliest killmail → now
    manage.py backfill_monthly_stats --since 2025-01 # resume from Jan 2025
    manage.py backfill_monthly_stats --current       # just the current + previous month
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.killboard import aggregation


class Command(BaseCommand):
    help = "Backfill/refresh the monthly per-pilot killboard aggregate."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--since", type=str, default=None,
            help="Resume from this month (YYYY-MM) instead of the earliest killmail.",
        )
        parser.add_argument(
            "--current", action="store_true",
            help="Only rebuild the current + previous calendar month (the incremental path).",
        )

    def handle(self, *args, **options) -> None:
        if options["current"]:
            n = aggregation.refresh_current_months()
            self.stdout.write(self.style.SUCCESS(f"Refreshed current window: {n} pilot-row(s)."))
            return

        since = None
        if options["since"]:
            try:
                y, m = options["since"].split("-")
                since = (int(y), int(m))
                if not (1 <= since[1] <= 12):
                    raise ValueError
            except ValueError as exc:
                raise CommandError("--since must be YYYY-MM (e.g. 2025-01).") from exc

        self.stdout.write("Backfilling monthly pilot stats…")
        total = aggregation.backfill(since=since, log=self.stdout.write)
        self.stdout.write(self.style.SUCCESS(f"Done — {total:,} pilot-row(s) written."))
