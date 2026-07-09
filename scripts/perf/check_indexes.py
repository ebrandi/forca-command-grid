#!/usr/bin/env python
"""Index health check — the three objective checks from the performance audit.

Run inside the app container:
    docker compose exec web python scripts/perf/check_indexes.py

Reports, for the connected database:
  1. total index count + tables covered,
  2. foreign-key columns lacking a covering leading index (should be 0),
  3. single-column indexes shadowed by a composite/unique index (redundancy candidates),
     excluding varchar_pattern_ops (`*_like`) indexes, which are NOT redundant.

Exit code is non-zero if a missing FK index is found (a real regression); redundancy is
reported for review, not failed on. Run before/after any index migration.
"""
from __future__ import annotations

import collections
import os
import sys

# Make the project root importable so `config` resolves however the script is invoked
# (running a file puts its own dir on sys.path, not the repo root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import django  # noqa: E402

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.db import connection  # noqa: E402


def _cols(indexdef: str) -> list[str]:
    inside = indexdef[indexdef.find("(") + 1 : indexdef.rfind(")")]
    return [p.strip().split()[0].strip('"') for p in inside.split(",")]


def main() -> int:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname='public' ORDER BY tablename, indexname"
        )
        idx = cur.fetchall()
        cur.execute(
            "SELECT c.conrelid::regclass::text, a.attname "
            "FROM pg_constraint c JOIN pg_attribute a "
            "ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey) WHERE c.contype='f'"
        )
        fks = cur.fetchall()

    tbl_idx: dict[str, list] = collections.defaultdict(list)
    for tbl, name, defn in idx:
        tbl_idx[tbl].append((name, _cols(defn), defn))

    print(f"1. Indexes: {len(idx)} across {len(tbl_idx)} tables")

    missing_fk = []
    for tbl, col in fks:
        leads = {ci[1][0] for ci in tbl_idx.get(tbl, [])}
        if col not in leads:
            missing_fk.append((tbl, col))
    print(f"2. FK columns without a covering leading index: {len(missing_fk)}")
    for tbl, col in missing_fk:
        print(f"     MISSING  {tbl}.{col}")

    redundant = []
    for tbl, lst in tbl_idx.items():
        singles = {ci[1][0]: ci[0] for ci in lst if len(ci[1]) == 1 and "_like" not in ci[0]}
        for name, cols, _defn in lst:
            if len(cols) > 1 and cols[0] in singles and singles[cols[0]] != name:
                redundant.append((tbl, singles[cols[0]], name))
    print(f"3. Single-column indexes shadowed by a composite (redundancy candidates): "
          f"{len(redundant)}")
    for tbl, single, comp in redundant:
        print(f"     REDUNDANT  {tbl}: {single}  (prefix of {comp})")

    return 1 if missing_fk else 0


if __name__ == "__main__":
    sys.exit(main())
