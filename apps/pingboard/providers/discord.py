"""Discord provider — webhook mode (Phase 0).

Carries the exact SSRF/abuse guard the legacy ``recommendations.notify._post_discord``
is pinned on: HTTPS + host allowlist + ``/api/webhooks/`` path + no redirects +
``allowed_mentions:{parse:[]}`` (never mass-ping — content is officer/service supplied)
+ 2000-char cap. Unlike the legacy fire-and-forget sink, this reports success so the
delivery row can be tracked. Bot-mode DMs land in the providers phase.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import requests
from django.utils.translation import gettext as _

from .base import AlertProvider, Recipient, SendResult

log = logging.getLogger("forca.pingboard")

_ALLOWED_WEBHOOK_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
_MAX_CONTENT = 2000


def _is_discord_webhook(url: str) -> bool:
    if not url:
        return False
    p = urlparse(url)
    return (
        p.scheme == "https"
        and p.hostname in _ALLOWED_WEBHOOK_HOSTS
        and p.path.startswith("/api/webhooks/")
    )


class DiscordProvider(AlertProvider):
    kind = "discord"
    supports_channel = True

    def _url(self) -> str:
        return self.row.secret if self.row else ""

    def validate_configuration(self) -> tuple[bool, str]:
        if _is_discord_webhook(self._url()):
            return True, ""
        return False, _("Webhook URL missing or not a valid Discord webhook")

    def send(self, *, subject: str, body: str, recipients: list[Recipient]) -> SendResult:
        url = self._url()
        if not _is_discord_webhook(url):
            # Refuse anything that isn't a genuine Discord webhook (SSRF guard).
            return SendResult(ok=False, error="invalid or missing Discord webhook")
        payload = {
            "content": (body or "")[:_MAX_CONTENT],
            "allowed_mentions": {"parse": []},  # never @everyone/@here/role/user pings
        }
        try:
            # No redirects: the allowlist is checked on the initial URL only.
            resp = requests.post(url, json=payload, timeout=10, allow_redirects=False)
        except requests.RequestException as exc:
            return SendResult(ok=False, error=f"discord post failed: {type(exc).__name__}")
        if 200 <= resp.status_code < 300:
            return SendResult(ok=True, recipients_ok=1)
        return SendResult(ok=False, error=f"discord http {resp.status_code}")
