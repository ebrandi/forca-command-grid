"""zKillboard enrichment adapter (optional, supplementary source).

zKillboard aggregates killmail (id, hash) pairs far more completely than ESI's
own recent-killmail endpoints (which only expose a limited recent window of
kills CCP attributes directly to an entity). We use zKill purely to *discover*
(killmail_id, hash) pairs, then fetch the canonical body from ESI ourselves.
See ADR-0001 (zKill is optional enrichment) and research/03.
"""
from __future__ import annotations

import requests
from django.conf import settings

ZKILL_BASE = "https://zkillboard.com/api"
_TIMEOUT = 30


def _get(path: str) -> list[dict]:
    headers = {
        # zKill requires a descriptive User-Agent with contact info.
        "User-Agent": settings.ESI_USER_AGENT,
        "Accept-Encoding": "gzip",
        "Accept": "application/json",
    }
    resp = requests.get(f"{ZKILL_BASE}/{path}", headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _refs(items: list[dict]) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for it in items:
        kid = it.get("killmail_id")
        khash = (it.get("zkb") or {}).get("hash")
        if kid and khash:
            out.append((int(kid), khash))
    return out


def corporation_killmail_refs(corporation_id: int) -> list[tuple[int, str]]:
    """Recent (id, hash) pairs for a corporation from zKillboard."""
    return _refs(_get(f"corporationID/{corporation_id}/"))


# zKill paginates the per-entity history at ~200 killmails per page (page 1 is
# the most recent). A page shorter than this marks the end of the history.
ZKILL_PAGE_SIZE = 200


def corporation_killmail_refs_page(corporation_id: int, page: int) -> list[tuple[int, str]]:
    """One page of (id, hash) pairs for a corporation (for full-history walks).

    Raises ``requests.HTTPError`` (e.g. on a 429) so the caller can pace/back off.
    """
    return _refs(_get(f"corporationID/{corporation_id}/page/{page}/"))


def character_killmail_refs(character_id: int) -> list[tuple[int, str]]:
    """Recent (id, hash) pairs for a character from zKillboard."""
    return _refs(_get(f"characterID/{character_id}/"))
