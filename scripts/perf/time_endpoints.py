#!/usr/bin/env python
"""Time key pages + count their SQL queries — track p50/query-count across deploys.

Run inside the app container (uses the Django test Client against the in-process app, so
it needs no running gunicorn and hits the real DB it is pointed at):
    docker compose exec web python scripts/perf/time_endpoints.py

For each URL it prints wall-clock ms and the number of SQL queries the request issued
(via CaptureQueriesContext) — the query count is the more stable signal, since it does not
depend on cache warmth or machine load. It force-logs the highest-role user it can find so
member/officer pages resolve; pages that still redirect (302) are reported as such.

This is a *relative* tool: run it before and after a change and compare query counts.
"""
from __future__ import annotations

import os
import sys
import time

# Make the project root importable so `config` resolves however the script is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402

# Representative hot pages (server-rendered). Add/remove freely.
URLS = [
    "/",
    "/dashboard/",
    "/killboard/",
    "/killboard/rankings/",
    "/doctrines/",
    "/operations/",
    "/readiness/",
    "/industry/jobs/",
    "/market/",
    "/tools/maps/",
]


def _login_best(client) -> str:
    from apps.identity.models import User
    from core import rbac

    best = None
    best_rank = -1
    for u in User.objects.all()[:500]:
        r = rbac.effective_rank(u)
        if r > best_rank:
            best, best_rank = u, r
    if best is not None:
        client.force_login(best)
        return f"{best.get_username()} (rank {best_rank})"
    return "anonymous (no users)"


def main() -> None:
    client = Client()
    who = _login_best(client)
    print(f"Timing {len(URLS)} pages as: {who}\n")
    print(f"  {'status':>6}  {'ms':>7}  {'queries':>7}  url")
    for url in URLS:
        try:
            with CaptureQueriesContext(connection) as ctx:
                t0 = time.perf_counter()
                resp = client.get(url)
                dt = (time.perf_counter() - t0) * 1000
            print(f"  {resp.status_code:>6}  {dt:>7.0f}  {len(ctx.captured_queries):>7}  {url}")
        except Exception as exc:  # noqa: BLE001 - a timing run reports, never aborts
            print(f"  {'ERR':>6}  {'-':>7}  {'-':>7}  {url}  ({type(exc).__name__}: {exc})")


if __name__ == "__main__":
    main()
