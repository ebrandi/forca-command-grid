"""Disciplined ESI HTTP client — the single chokepoint for all CCP calls.

Enforces good-citizen behaviour (handbooks/contributor-handbook/esi-integration.md §7): pinned
X-Compatibility-Date, descriptive User-Agent, ETag/If-None-Match, the error
budget (420) and token-bucket (429) guards, backoff+jitter, and X-Pages
pagination. Never called from a web request — only from Celery tasks.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

import requests
from django.conf import settings
from django.core.cache import cache

from . import ratelimit


class ESIError(Exception):
    pass


class ESIRateLimited(ESIError):
    """Raised when the error budget / token bucket forbids a call right now."""


@dataclass
class ESIResponse:
    status: int
    data: object | None
    headers: dict = field(default_factory=dict)
    not_modified: bool = False


class ESIClient:
    def __init__(self, session: requests.Session | None = None, max_retries: int = 3):
        self._session = session or requests.Session()
        self._max_retries = max_retries

    def _base_headers(self, token: str | None) -> dict:
        headers = {
            "User-Agent": settings.ESI_USER_AGENT,
            "X-Compatibility-Date": settings.ESI_COMPATIBILITY_DATE,
            "Accept": "application/json",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _etag_key(path: str) -> str:
        return f"esi:etag:{path}"

    def get(
        self,
        path: str,
        *,
        token: str | None = None,
        params: dict | None = None,
        essential: bool = False,
        use_etag: bool = True,
    ) -> ESIResponse:
        """GET an ESI path (e.g. '/characters/123/'). Returns ESIResponse."""
        if not ratelimit.can_call(essential=essential):
            raise ESIRateLimited("ESI error budget/token bucket exhausted; deferring call.")

        url = f"{settings.ESI_BASE_URL}{path}"
        headers = self._base_headers(token)
        etag_entry = cache.get(self._etag_key(path)) if use_etag else None
        if etag_entry and use_etag:
            headers["If-None-Match"] = etag_entry["etag"]

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            resp = self._session.get(url, headers=headers, params=params, timeout=30)
            ratelimit.record_response(dict(resp.headers))

            if resp.status_code == 304 and etag_entry:
                return ESIResponse(304, etag_entry["data"], dict(resp.headers), not_modified=True)

            if resp.status_code == 200:
                if use_etag and resp.headers.get("ETag"):
                    cache.set(
                        self._etag_key(path),
                        {"etag": resp.headers["ETag"], "data": resp.json()},
                        timeout=86400,
                    )
                return ESIResponse(200, resp.json(), dict(resp.headers))

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "1"))
                ratelimit.note_retry_after(retry_after)
                raise ESIRateLimited(f"429 from ESI; retry after {retry_after}s")

            if resp.status_code == 420:
                raise ESIRateLimited("420 from ESI; error limit reached")

            if 500 <= resp.status_code < 600:
                last_exc = ESIError(f"ESI {resp.status_code} for {path}")
                self._sleep_backoff(attempt)
                continue

            # 4xx other than 429: not retryable.
            raise ESIError(f"ESI {resp.status_code} for {path}: {resp.text[:200]}")

        raise last_exc or ESIError(f"ESI request failed for {path}")

    def post(
        self,
        path: str,
        *,
        json: dict | list | None = None,
        token: str | None = None,
        params: dict | None = None,
        essential: bool = False,
    ) -> ESIResponse:
        """POST an ESI path (e.g. the new body-based /route/). Returns ESIResponse.

        Same good-citizen guards as ``get`` (budget/bucket, backoff on 5xx, clean
        ESIError on 4xx). Not cached at the HTTP layer — callers cache results
        themselves where appropriate.
        """
        if not ratelimit.can_call(essential=essential):
            raise ESIRateLimited("ESI error budget/token bucket exhausted; deferring call.")

        url = f"{settings.ESI_BASE_URL}{path}"
        headers = self._base_headers(token)
        headers["Content-Type"] = "application/json"

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            resp = self._session.post(url, headers=headers, json=json, params=params, timeout=30)
            ratelimit.record_response(dict(resp.headers))

            # Any 2xx is success for a POST: /route/ returns 200, but creating
            # resources (e.g. POST /characters/{id}/mail/) returns 201, and some
            # endpoints answer 204 with no body.
            if 200 <= resp.status_code < 300:
                body = resp.json() if resp.content else None
                return ESIResponse(resp.status_code, body, dict(resp.headers))
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "1"))
                ratelimit.note_retry_after(retry_after)
                raise ESIRateLimited(f"429 from ESI; retry after {retry_after}s")
            if resp.status_code == 420:
                raise ESIRateLimited("420 from ESI; error limit reached")
            if 500 <= resp.status_code < 600:
                last_exc = ESIError(f"ESI {resp.status_code} for {path}")
                self._sleep_backoff(attempt)
                continue
            raise ESIError(f"ESI {resp.status_code} for {path}: {resp.text[:200]}")

        raise last_exc or ESIError(f"ESI request failed for {path}")

    def get_paged(self, path: str, *, token: str | None = None, params: dict | None = None) -> list:
        """Fetch all pages of a paginated endpoint via X-Pages."""
        params = dict(params or {})
        params["page"] = 1
        first = self.get(path, token=token, params=params, use_etag=False)
        items = list(first.data or [])
        pages = int(first.headers.get("X-Pages", "1"))
        for page in range(2, pages + 1):
            params["page"] = page
            resp = self.get(path, token=token, params=params, use_etag=False)
            items.extend(resp.data or [])
        return items

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        time.sleep(min(2**attempt + random.uniform(0, 0.5), 8))  # noqa: S311 - jitter, not crypto


def get_client() -> ESIClient:
    return ESIClient()
