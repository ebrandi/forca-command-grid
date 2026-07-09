"""Backfill the killboard from EVE Ref's historical killmail archives.

EVE Ref publishes every killmail since 2007 as daily ``.tar.bz2`` archives of verbatim
ESI bodies. We download each day in the range, keep only the kills involving the home
corporation (victim or attacker), and ingest the body directly — so, unlike the
zKillboard history importer, there is **no per-killmail ESI call**: a backfill is bound
by download speed, not the ESI rate limit.

Idempotent and resumable: killmails already stored are skipped, so an interrupted run can
simply be started again. ISK values come from our own valuation at current prices (EVE
Ref carries ESI fields only, no zKill totals), so old kills are valued indicatively.

Usage:
    manage.py import_everef_killmails --from 2012-01-01            # …to today, home corp
    manage.py import_everef_killmails --from 2024-01-01 --to 2024-01-31
    manage.py import_everef_killmails --from 2024-06-01 --dry-run  # count, don't write
    manage.py import_everef_killmails --from 2024-06-01 --corp 98028546 --limit 50
"""
from __future__ import annotations

import datetime as dt
import io
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.killboard.everef import day_url, iter_matching_killmails
from apps.killboard.ingest import ingest_killmail
from apps.killboard.models import Killmail
from core.mixins import Source


def _parse_date(value: str) -> dt.date:
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise CommandError(f"Invalid date {value!r}; use YYYY-MM-DD.") from exc


class Command(BaseCommand):
    help = "Backfill the killboard from EVE Ref's historical killmail archives."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--from", dest="from_date", required=True, help="start date YYYY-MM-DD")
        parser.add_argument("--to", dest="to_date", default=None, help="end date YYYY-MM-DD (default: today UTC)")
        parser.add_argument("--corp", type=int, default=None, help="corporation_id (default: home corp)")
        parser.add_argument("--limit", type=int, default=0, help="stop after N new ingests (0 = no limit)")
        parser.add_argument("--day-delay", type=float, default=0.0, help="seconds to pause between days")
        parser.add_argument("--dry-run", action="store_true", help="count matches without writing")

    def handle(self, *args, **opts) -> None:
        corp = opts["corp"] or getattr(settings, "FORCA_HOME_CORP_ID", 0)
        if not corp:
            raise CommandError("No corporation: pass --corp <id> or set FORCA_HOME_CORP_ID.")
        start = _parse_date(opts["from_date"])
        end = _parse_date(opts["to_date"]) if opts["to_date"] else dt.datetime.now(dt.UTC).date()
        if start > end:
            raise CommandError("--from must be on or before --to.")
        corp_ids = {corp}
        dry = opts["dry_run"]
        limit = opts["limit"]
        delay = max(0.0, opts["day_delay"])

        session = requests.Session()
        session.headers["User-Agent"] = settings.ESI_USER_AGENT

        have = set(Killmail.objects.values_list("killmail_id", flat=True))
        self.stdout.write(
            f"Backfilling corp {corp} from {start} to {end} "
            f"({(end - start).days + 1} days){' [dry-run]' if dry else ''}; "
            f"{len(have)} killmails already stored."
        )

        days = matched = ingested = errors = 0
        day = start
        while day <= end:
            days += 1
            try:
                blob = self._download(session, day)
            except Exception as exc:  # noqa: BLE001 - network hiccup: log and continue
                errors += 1
                self.stderr.write(f"  {day}: download failed ({exc}); skipping.")
                day += dt.timedelta(days=1)
                continue

            if blob is not None:
                day_matched = day_ingested = 0
                for body in iter_matching_killmails(blob, corp_ids):
                    kid = body.get("killmail_id")
                    if not kid:
                        continue
                    day_matched += 1
                    matched += 1
                    if kid in have:
                        continue
                    if not dry:
                        try:
                            ingest_killmail(kid, "", source=Source.EVEREF, body=body)
                        except Exception as exc:  # noqa: BLE001 - one bad kill must not stop the run
                            errors += 1
                            self.stderr.write(f"  killmail {kid}: ingest failed ({exc}).")
                            continue
                    have.add(kid)
                    day_ingested += 1
                    ingested += 1
                if day_matched:
                    self.stdout.write(
                        f"  {day}: matched {day_matched}, new {day_ingested} "
                        f"(running: {ingested} ingested)"
                    )

            if limit and ingested >= limit:
                self.stdout.write(f"  reached --limit {limit}; stopping early.")
                break
            if days % 60 == 0:
                self.stdout.write(f"  …{day} · {days} days scanned · {ingested} ingested")
            if delay:
                time.sleep(delay)
            day += dt.timedelta(days=1)

        verb = "would ingest" if dry else "ingested"
        self.stdout.write(self.style.SUCCESS(
            f"Done: {days} days, {matched} matched, {verb} {ingested}"
            + (f", {errors} errors" if errors else "")
        ))

    def _download(self, session: requests.Session, day: dt.date) -> io.BytesIO | None:
        """Fetch one day's archive into memory; ``None`` if the day has no file (404)."""
        url = day_url(day)
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = session.get(url, timeout=120, stream=True)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                last_exc = RuntimeError(f"HTTP {resp.status_code}")
                time.sleep(1.5 * (attempt + 1))
                continue
            from core.netcap import download_to_buffer
            return download_to_buffer(resp, chunk=65536)
        raise last_exc or RuntimeError("download failed")
