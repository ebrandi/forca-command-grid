"""KB-35: backfill each killmail's ``value_at_kill`` from period-accurate prices.

Prices every item + hull at the market on the day the ship died (EVE Ref daily The-Forge
history, downloaded + cached on demand; oracle routing for high-value/PLEX types) and stamps
``value_at_kill`` + ``value_source``. The daily re-value never touches these, so rankings can
read the at-kill value for fairness.

Resumable and idempotent: by default only mails with no at-kill value yet are processed
(``value_at_kill IS NULL``), walked in ``killmail_id`` order in batches, so an interrupted run
picks up where it left off. ``--reprice`` re-stamps everything in range (use after the oracle
routing or history data changes).

    manage.py backfill_value_at_kill                       # all un-stamped mails
    manage.py backfill_value_at_kill --since 2024-01-01 --until 2024-12-31
    manage.py backfill_value_at_kill --reprice --batch 500 --limit 5000
    manage.py backfill_value_at_kill --no-fetch            # read local history only, no downloads

Tip: pre-load history first for a big range (one download per day instead of on-demand):
    manage.py import_everef_market_history --from 2024-01-01 --to 2024-12-31 --all
"""
from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.killboard.models import Killmail
from apps.killboard.valuation import stamp_value_at_kill


def _parse_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise CommandError(f"Invalid date {value!r}; use YYYY-MM-DD.") from exc


class Command(BaseCommand):
    help = "Stamp value_at_kill on killmails from period-accurate (kill-date) prices."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--since", default=None, help="only kills on/after this date (YYYY-MM-DD)")
        parser.add_argument("--until", default=None, help="only kills on/before this date (YYYY-MM-DD)")
        parser.add_argument("--batch", type=int, default=500, help="killmails per DB page")
        parser.add_argument("--limit", type=int, default=0, help="stop after N killmails (0 = all)")
        parser.add_argument(
            "--reprice", action="store_true",
            help="re-stamp mails that already have an at-kill value (default: only NULL ones).",
        )
        parser.add_argument(
            "--no-fetch", action="store_true",
            help="read local MarketHistory only; never download a missing day-file.",
        )

    def handle(self, *args, **opts) -> None:
        qs = Killmail.objects.all()
        if opts["since"]:
            start = _parse_date(opts["since"])
            qs = qs.filter(killmail_time__gte=dt.datetime.combine(start, dt.time.min, tzinfo=dt.UTC))
        if opts["until"]:
            end = _parse_date(opts["until"])
            qs = qs.filter(killmail_time__lt=dt.datetime.combine(
                end + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC))
        if not opts["reprice"]:
            qs = qs.filter(value_at_kill__isnull=True)

        fetch = not opts["no_fetch"]
        batch = max(1, opts["batch"])
        limit = max(0, opts["limit"])
        total_target = qs.count()
        self.stdout.write(
            f"Stamping value_at_kill on {total_target:,} killmail(s) "
            f"({'re-pricing all in range' if opts['reprice'] else 'un-stamped only'}; "
            f"{'on-demand fetch' if fetch else 'local history only'})."
        )

        # Resumable cursor: walk by killmail_id so a re-run after an interruption continues past
        # the last stamped id without re-reading the whole table into memory.
        processed = 0
        sources: dict[str, int] = {}
        last_id = 0
        started = timezone.now()
        while True:
            page = list(
                qs.filter(killmail_id__gt=last_id)
                .order_by("killmail_id")
                .prefetch_related("items")[:batch]
            )
            if not page:
                break
            for km in page:
                label = stamp_value_at_kill(km, historical=True, fetch=fetch)
                sources[label] = sources.get(label, 0) + 1
                last_id = km.killmail_id
                processed += 1
                if limit and processed >= limit:
                    break
            self.stdout.write(f"  …{processed:,}/{total_target:,} (through id {last_id})")
            if limit and processed >= limit:
                break

        elapsed = (timezone.now() - started).total_seconds()
        breakdown = ", ".join(f"{k}={v:,}" for k, v in sorted(sources.items())) or "none"
        self.stdout.write(self.style.SUCCESS(
            f"Done — {processed:,} killmail(s) stamped in {elapsed:.0f}s. Sources: {breakdown}."
        ))
