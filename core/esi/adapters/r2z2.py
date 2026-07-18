"""R2Z2 killstream adapter — zKillboard's sequence-cursor realtime feed.

R2Z2 is zKillboard's successor to RedisQ (which hard-sunset 2026-05-31; we never
depended on it). It publishes every New Eden killmail under a strictly-increasing,
global, non-reused **sequence** number, so a consumer resumes simply by persisting the
last sequence it processed and incrementing. Each per-sequence file carries the full ESI
killmail body inline under the ``esi`` key, plus the killmail ``hash`` and zKillboard's
``zkb`` metadata — so no second ESI round-trip is needed.

This adapter is pure fetch/parse (no DB, no app imports), mirroring :mod:`core.esi.adapters.zkill`.
The home-corp filtering and ingestion live in :mod:`apps.killboard.killstream`, which uses
R2Z2 only as an **optional realtime fallback** on top of the authoritative ESI/zKill-query/
EVE-Ref feeds — never as a replacement.

Contract (verified live 2026-07-18, https://github.com/zKillboard/zKillboard/wiki/API-(R2Z2)):
- ``GET /ephemeral/sequence.json`` -> ``{"sequence": <int>}`` (the current tip).
- ``GET /ephemeral/{seq}.json`` -> ``{esi, hash, killmail_id, sequence_id, uploaded_at, zkb}``;
  a **404** means that sequence isn't available yet (you've caught up) or is a rare gap.
- Etiquette: max **15 requests/second/IP** (violators get HTTP 403 for an hour); a
  **descriptive User-Agent is mandatory** (a blank one is blocked by Cloudflare); the
  ephemeral files expire after ≥24h, so R2Z2 is a realtime tap, not a history store
  (EVE Ref remains the historical backfill).
"""
from __future__ import annotations

import requests
from django.conf import settings

R2Z2_BASE = "https://r2z2.zkillboard.com/ephemeral"
_TIMEOUT = 20


def _headers() -> dict[str, str]:
    return {
        # Mandatory: a blank User-Agent is blocked by Cloudflare. We reuse the ESI UA,
        # which production requires to carry a real contact address.
        "User-Agent": settings.ESI_USER_AGENT,
        "Accept-Encoding": "gzip",
        "Accept": "application/json",
    }


def latest_sequence() -> int | None:
    """The current sequence tip, or ``None`` if the payload is malformed.

    Raises ``requests.HTTPError`` on a non-2xx response (e.g. a 403 rate-limit ban) so the
    caller can record the failure and back off.
    """
    resp = requests.get(f"{R2Z2_BASE}/sequence.json", headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    seq = (resp.json() or {}).get("sequence")
    return int(seq) if seq is not None else None


def fetch_package(seq: int) -> dict | None:
    """One sequence file's package, or ``None`` when it 404s (caught up / a gap).

    Raises ``requests.HTTPError`` on other non-2xx responses (403 ban, 5xx) so a real
    outage surfaces to the caller rather than looking like "caught up".
    """
    resp = requests.get(f"{R2Z2_BASE}/{int(seq)}.json", headers=_headers(), timeout=_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def package_to_ingest(package: dict) -> tuple[int, str, dict] | None:
    """``(killmail_id, hash, esi_body)`` from an R2Z2 package, or ``None`` if malformed.

    The ESI killmail body — the exact shape ``ingest_killmail(..., body=...)`` expects —
    is the ``esi`` sub-object; the killmail hash is the top-level ``hash``.
    """
    if not isinstance(package, dict):
        return None
    kid = package.get("killmail_id")
    khash = package.get("hash")
    esi = package.get("esi")
    if not kid or not khash or not isinstance(esi, dict):
        return None
    return int(kid), str(khash), esi
