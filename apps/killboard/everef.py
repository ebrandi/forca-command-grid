"""Read EVE Ref's daily killmail archives and pick out the ones that are ours.

EVE Ref (https://everef.net) publishes every killmail since 2007 as a daily
``killmails-YYYY-MM-DD.tar.bz2`` of verbatim ESI bodies — one ``killmails/{id}.json``
entry per kill. The archive carries no killmail hash (it's not in the ESI body), but we
don't need one: our ``ingest_killmail(..., body=...)`` path takes the body directly and
never re-fetches from ESI.

This module is pure parsing — no network, no DB — so it's easy to unit-test. The command
``import_everef_killmails`` does the downloading and ingesting.
"""
from __future__ import annotations

import datetime as dt
import json
import tarfile
from collections.abc import Iterator
from typing import BinaryIO

KILLMAILS_BASE = "https://data.everef.net/killmails"


def day_url(day: dt.date) -> str:
    """Download URL for a single day's killmail archive."""
    return f"{KILLMAILS_BASE}/{day:%Y}/killmails-{day:%Y-%m-%d}.tar.bz2"


def _involves(body: dict, corp_ids: set[int]) -> bool:
    if (body.get("victim") or {}).get("corporation_id") in corp_ids:
        return True
    return any(a.get("corporation_id") in corp_ids for a in body.get("attackers") or [])


def iter_matching_killmails(fileobj: BinaryIO, corp_ids: set[int]) -> Iterator[dict]:
    """Yield each killmail body in the archive that involves one of ``corp_ids``.

    A cheap byte-substring pre-filter (the corp id as ASCII digits) skips parsing the
    overwhelming majority of kills that can't possibly match, then the survivors are
    JSON-parsed and checked precisely (victim or any attacker in ``corp_ids``).
    """
    from core.netcap import MAX_DECOMPRESSED_BYTES, MAX_MEMBER_BYTES, DataTooLarge

    needles = [str(c).encode() for c in corp_ids if c]
    if not needles:
        return
    total = 0
    with tarfile.open(fileobj=fileobj, mode="r:bz2") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".json"):
                continue
            if member.size and member.size > MAX_MEMBER_BYTES:
                continue  # a single absurd "killmail" — a decompression bomb; skip it
            total += member.size or 0
            if total > MAX_DECOMPRESSED_BYTES:
                raise DataTooLarge("killmail archive exceeded the decompressed ceiling")
            handle = tar.extractfile(member)
            if handle is None:
                continue
            raw = handle.read()
            if not any(n in raw for n in needles):
                continue  # corp id can't appear → not ours, skip the parse
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if _involves(body, corp_ids):
                yield body
