"""Discord access provider (5.1) — manage guild member roles with a bot token.

The bot needs the **Manage Roles** permission on the guild, and its own highest role must
sit **above** every managed role (Discord's role hierarchy) or the API returns 403 — this
is a documented setup step, surfaced as a redacted failure, never a crash.

Discipline (mirrors ``apps.pingboard.providers``):
* SSRF guard — HTTPS + ``discord.com`` host only, ``allow_redirects=False``.
* Best-effort — no method raises into the engine; failures come back on :class:`ApplyResult`.
* Rate limits — a 429 is honoured once (bounded ``retry_after``) then given up on; the
  reconcile is idempotent so the next sweep retries.

The bot token is resolved by :mod:`apps.comms_access.credentials` — the leadership-managed
console credential (encrypted at rest) first, ``settings.DISCORD_BOT_TOKEN`` as fallback;
never logged. The guild id comes from the platform config passed by the engine.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import requests

from .base import AccessProvider, ApplyResult

log = logging.getLogger("forca.comms_access")

_API = "https://discord.com/api/v10"
_ALLOWED_HOST = "discord.com"
_MAX_RETRY_AFTER = 5.0  # seconds — cap the 429 wait so a worker never blocks for long


def _host_ok(url: str) -> bool:
    p = urlparse(url or "")
    return p.scheme == "https" and p.hostname == _ALLOWED_HOST


class DiscordAccessProvider(AccessProvider):
    platform = "discord"
    supports_link = True
    supports_kick = True

    def _token(self) -> str:
        # Console-managed credential first (leadership-configurable), env as fallback.
        from ..credentials import discord_bot_token

        return discord_bot_token()

    def _guild(self) -> str:
        return str(self.config.get("guild_id", "") or "")

    def validate_configuration(self) -> tuple[bool, str]:
        if not self._token():
            return False, "Discord bot token not configured"
        if not self._guild():
            return False, "Discord guild id not configured"
        return True, ""

    # -- guarded HTTP -----------------------------------------------------------
    def _request(self, method: str, path: str):
        """One SSRF-guarded call with a single 429 retry. Returns ``(response, error)``."""
        url = f"{_API}{path}"
        if not _host_ok(url):
            return None, "outbound host not allowlisted"
        headers = {
            "Authorization": f"Bot {self._token()}",
            "User-Agent": "FORCA-CommandGrid (comms-access, 1.0)",
        }
        for attempt in (1, 2):
            try:
                resp = requests.request(
                    method, url, headers=headers, timeout=10, allow_redirects=False
                )
            except requests.RequestException as exc:
                return None, f"request failed: {type(exc).__name__}"
            if resp.status_code == 429 and attempt == 1:
                wait = _MAX_RETRY_AFTER
                try:
                    wait = min(float(resp.json().get("retry_after", wait)), _MAX_RETRY_AFTER)
                except Exception:  # noqa: BLE001, S110 - malformed 429 body: fall back to the cap
                    pass
                time.sleep(max(0.0, wait))
                continue
            return resp, ""
        return resp, ""

    # -- provider API -----------------------------------------------------------
    def read_current(self, account) -> set[str]:
        if not account.external_id:
            return set()
        resp, err = self._request("GET", f"/guilds/{self._guild()}/members/{account.external_id}")
        if err or resp is None or resp.status_code != 200:
            return set()  # 404 (not in guild) / error ⇒ unknown, treat as none
        try:
            roles = resp.json().get("roles", []) or []
        except Exception:  # noqa: BLE001
            return set()
        return {str(r) for r in roles}

    def apply(self, account, *, add: set[str], remove: set[str]) -> ApplyResult:
        if not account.external_id:
            return ApplyResult(ok=False, error="account has no Discord user id")
        guild = self._guild()
        applied_add: set[str] = set()
        applied_remove: set[str] = set()
        last_error = ""
        for ref in add:
            resp, err = self._request(
                "PUT", f"/guilds/{guild}/members/{account.external_id}/roles/{ref}"
            )
            if not err and resp is not None and 200 <= resp.status_code < 300:
                applied_add.add(ref)
            else:
                last_error = err or (f"discord http {resp.status_code}" if resp is not None else "no response")
        for ref in remove:
            resp, err = self._request(
                "DELETE", f"/guilds/{guild}/members/{account.external_id}/roles/{ref}"
            )
            if not err and resp is not None and 200 <= resp.status_code < 300:
                applied_remove.add(ref)
            else:
                last_error = err or (f"discord http {resp.status_code}" if resp is not None else "no response")
        ok = not last_error
        return ApplyResult(ok=ok, applied_add=applied_add, applied_remove=applied_remove, error=last_error)

    def kick(self, account, reason: str = "") -> ApplyResult:
        if not self.config.get("kick_enabled"):
            return ApplyResult(ok=False, skipped=True, error="kick not enabled")
        if not account.external_id:
            return ApplyResult(ok=False, error="account has no Discord user id")
        resp, err = self._request(
            "DELETE", f"/guilds/{self._guild()}/members/{account.external_id}"
        )
        if not err and resp is not None and 200 <= resp.status_code < 300:
            return ApplyResult(ok=True)
        detail = err or (f"discord http {resp.status_code}" if resp is not None else "no response")
        return ApplyResult(ok=False, error=detail)
