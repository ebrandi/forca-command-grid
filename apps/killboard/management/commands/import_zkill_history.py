"""Bulk-import a corporation's FULL killmail history from zKillboard.

ESI only exposes a recent window of an entity's killmails; zKillboard has the
complete history. We page through zKill's corporation endpoint to discover every
(killmail_id, hash) pair, then fetch each canonical body from ESI — both steps
paced so we stay a good citizen and never trip a rate limit:

  * zKill: one page request every ``--zkill-delay`` seconds (their etiquette is
    ~1 req/s + a descriptive User-Agent, which ESI_USER_AGENT provides), with
    exponential back-off on a 429.
  * ESI: the per-killmail body fetch honours the client's own error-budget /
    token-bucket guard, plus a fixed ``--esi-delay`` between calls.

Idempotent and resumable: killmails already stored are skipped (no ESI re-fetch),
so an interrupted run can simply be started again.

Usage:
    manage.py import_zkill_history                 # home corp, full history
    manage.py import_zkill_history --corp 98028546
    manage.py import_zkill_history --limit 25      # smoke test
"""
from __future__ import annotations

import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.killboard.ingest import ingest_killmail
from apps.killboard.models import Killmail
from core.esi import ratelimit
from core.esi.adapters import zkill
from core.esi.client import ESIClient
from core.mixins import Source


class Command(BaseCommand):
    help = "Import a corporation's full killmail history from zKillboard (paced)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--corp", type=int, default=None, help="corporation_id (default: home corp)")
        parser.add_argument("--max-pages", type=int, default=500, help="safety cap on zKill pages")
        parser.add_argument("--zkill-delay", type=float, default=1.1, help="seconds between zKill page requests")
        parser.add_argument("--esi-delay", type=float, default=0.2, help="seconds between ESI body fetches")
        parser.add_argument("--limit", type=int, default=0, help="stop after N new ingests (0 = no limit)")

    def handle(self, *args, **opts) -> None:
        corp = opts["corp"] or getattr(settings, "FORCA_HOME_CORP_ID", 0)
        if not corp:
            raise CommandError("No corporation: pass --corp <id> or set FORCA_HOME_CORP_ID.")
        zdelay = max(0.0, opts["zkill_delay"])
        edelay = max(0.0, opts["esi_delay"])
        limit = opts["limit"]

        self.stdout.write(f"Discovering killmail refs for corp {corp} from zKillboard…")
        refs = self._discover(corp, opts["max_pages"], zdelay)
        self.stdout.write(f"  zKill returned {len(refs)} killmail refs.")

        have = set(Killmail.objects.values_list("killmail_id", flat=True))
        todo = [(kid, kh) for (kid, kh) in refs if kid not in have]
        self.stdout.write(f"  {len(todo)} new to ingest ({len(refs) - len(todo)} already stored).")

        client = ESIClient()
        ingested = errors = 0
        for i, (kid, kh) in enumerate(todo, 1):
            if limit and ingested >= limit:
                self.stdout.write(f"  reached --limit {limit}; stopping early.")
                break
            wait = ratelimit.seconds_until_unblocked()
            if wait > 0:
                time.sleep(min(wait, 60))
            try:
                ingest_killmail(kid, kh, source=Source.ZKILL, client=client)
                ingested += 1
            except Exception as exc:  # noqa: BLE001 - one bad mail must not stop the run
                errors += 1
                if errors <= 20:
                    self.stderr.write(f"    ingest failed for {kid}: {exc}")
            if i % 200 == 0:
                self.stdout.write(f"  …{i}/{len(todo)} processed ({ingested} stored, {errors} errors)")
            time.sleep(edelay)

        self._finalize()
        self.stdout.write(self.style.SUCCESS(
            f"Done. Stored {ingested} new killmail(s), {errors} error(s). "
            f"Corp killboard now holds {Killmail.objects.count()} killmail(s)."
        ))

    def _discover(self, corp: int, max_pages: int, zdelay: float) -> list[tuple[int, str]]:
        """Walk zKill pages (newest first) until the history ends or the cap hits."""
        refs: list[tuple[int, str]] = []
        seen: set[int] = set()
        for page in range(1, max_pages + 1):
            page_refs = self._fetch_page(corp, page)
            if page_refs is None:  # gave up on this page after retries
                break
            if not page_refs:  # past the last page
                break
            fresh = [(k, h) for (k, h) in page_refs if k not in seen]
            seen.update(k for k, _ in fresh)
            refs.extend(fresh)
            self.stdout.write(f"  page {page}: +{len(page_refs)} (running total {len(refs)})")
            if len(page_refs) < zkill.ZKILL_PAGE_SIZE:
                break  # short page = end of history
            time.sleep(zdelay)
        return refs

    def _fetch_page(self, corp: int, page: int) -> list[tuple[int, str]] | None:
        for attempt in range(5):
            try:
                return zkill.corporation_killmail_refs_page(corp, page)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status == 429:
                    backoff = 5 * (attempt + 1)
                    self.stderr.write(f"  zKill 429 on page {page}; backing off {backoff}s")
                    time.sleep(backoff)
                    continue
                self.stderr.write(f"  zKill HTTP {status} on page {page}; stopping discovery")
                return None
            except requests.RequestException as exc:
                self.stderr.write(f"  zKill error on page {page} ({exc}); retrying")
                time.sleep(3)
        self.stderr.write(f"  gave up on page {page} after retries")
        return None

    def _finalize(self) -> None:
        self.stdout.write("Resolving pilot/corp names and rebuilding stats…")
        try:
            from core.esi.names import backfill_killmail_names

            backfill_killmail_names()
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(f"  name backfill skipped: {exc}")
        try:
            from apps.killboard.stats import rebuild_corp_metrics

            rebuild_corp_metrics()
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(f"  stats rebuild skipped: {exc}")
