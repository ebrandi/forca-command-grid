"""KB-39 (WS-D6) — the OBS overlay's per-corp token, threshold, and public-tier feed.

An OBS overlay runs in a streaming client with no login, so it authenticates with a
**per-corp overlay token** carried in the URL (``/killboard/overlay/?token=…``). The token is a
single director-managed secret stored in one ``AppSetting`` row; a director regenerates it from
the setup page, which instantly invalidates any previously-shared overlay URL.

The overlay shows **public-tier data only** — it reuses the WS-B2 stream's ``member=False``
payload, so the member-gated ``deviated-losses`` / ``needs-srp`` flags are never present. It
consumes the stream's **poll** contract rather than a long-lived SSE connection on purpose: an
OBS scene left open for hours would otherwise pin one of the small, bounded SSE semaphore slots
for the whole broadcast and starve the members' live feed (WS-B2's worker budget). Polling every
few seconds carries the same cursor contract with none of that risk.
"""
from __future__ import annotations

import secrets

from django.core.cache import cache

_SETTING_KEY = "killboard.overlay"
_CACHE_KEY = "killboard:overlay:v1"
_CACHE_TTL = 300

# Default "big kill" highlight threshold (ISK) — a kill at/above this pulses on the overlay.
_DEFAULT_THRESHOLD = 1_000_000_000


def _load() -> dict:
    cached = cache.get(_CACHE_KEY)
    if cached is None:
        from apps.admin_audit.models import AppSetting

        stored = AppSetting.get(_SETTING_KEY, {}) or {}
        cached = {
            "token": stored.get("token") or "",
            "threshold": _coerce_threshold(stored.get("threshold")),
        }
        cache.set(_CACHE_KEY, cached, _CACHE_TTL)
    return dict(cached)


def _coerce_threshold(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD
    return n if n > 0 else _DEFAULT_THRESHOLD


def _save(data: dict) -> None:
    from apps.admin_audit.models import AppSetting

    AppSetting.objects.update_or_create(key=_SETTING_KEY, defaults={"value": data})
    cache.delete(_CACHE_KEY)


def get_token() -> str:
    """The current overlay token, or "" when a director has not generated one yet."""
    return _load()["token"]


def big_kill_threshold() -> int:
    return _load()["threshold"]


def token_valid(token: str | None) -> bool:
    """Constant-time check of a supplied token against the stored one. Always False when no token
    has been generated (an unset overlay is closed, not open to any request)."""
    current = get_token()
    if not current or not token:
        return False
    return secrets.compare_digest(str(token), current)


def regenerate_token(*, user=None) -> str:
    """Mint a fresh overlay token (invalidating any old URL), preserving the threshold."""
    data = _load()
    data["token"] = secrets.token_urlsafe(24)
    _save({"token": data["token"], "threshold": data["threshold"]})
    return data["token"]


def set_threshold(value, *, user=None) -> int:
    """Persist the big-kill highlight threshold, preserving the token. Returns the stored value."""
    data = _load()
    threshold = _coerce_threshold(value)
    _save({"token": data["token"], "threshold": threshold})
    return threshold


def public_feed(cursor: int, topics: str | None = None) -> dict:
    """A public-tier poll batch after ``cursor`` (reuses the WS-B2 stream, ``member=False``).

    Returns the stream's ``{events, cursor, has_more}`` shape; every event is the anonymous
    payload, so no member-only flag is ever exposed on the overlay.
    """
    from . import stream

    matcher = stream.build_matcher(topics, member=False)
    return stream.poll_batch(cursor, matcher, member=False)
