"""Resolve comms-platform credentials — console-managed rows first, env fallback.

Leadership configures the Discord bot token + OAuth client entirely in the Director console
(:class:`apps.comms_access.models.PlatformCredential`). The historical env settings
(``DISCORD_BOT_TOKEN`` / ``DISCORD_OAUTH_*``) remain a fallback so existing env-based
deployments keep working. A non-empty console value always wins.

Every getter is best-effort and never raises: a decrypt failure or a missing row degrades
to the env value (or ``""``), never a crash on the login / reconcile path.
"""
from __future__ import annotations

from django.conf import settings

from .models import Platform, PlatformCredential


def _row(platform: str):
    try:
        return PlatformCredential.objects.filter(platform=platform).first()
    except Exception:  # noqa: BLE001 - never let a credentials lookup break a reconcile/login
        return None


def _env(name: str) -> str:
    return getattr(settings, name, "") or ""


# -- Discord ------------------------------------------------------------------
def discord_bot_token() -> str:
    row = _row(Platform.DISCORD)
    if row and row.has_bot_token:
        token = row.bot_token
        if token:
            return token
    return _env("DISCORD_BOT_TOKEN")


def discord_oauth() -> dict:
    """Resolved ``{client_id, client_secret, callback_url}`` (console wins, env fills gaps)."""
    row = _row(Platform.DISCORD)
    client_id = (row.oauth_client_id if row else "") or _env("DISCORD_OAUTH_CLIENT_ID")
    callback = (row.oauth_callback_url if row else "") or _env("DISCORD_OAUTH_CALLBACK_URL")
    secret = ""
    if row and row.has_oauth_client_secret:
        secret = row.oauth_client_secret
    if not secret:
        secret = _env("DISCORD_OAUTH_CLIENT_SECRET")
    return {"client_id": client_id, "client_secret": secret, "callback_url": callback}


def discord_oauth_enabled() -> bool:
    o = discord_oauth()
    return bool(o["client_id"] and o["client_secret"])


def discord_bot_token_configured() -> bool:
    """True when a usable bot token exists from either source (no value exposed)."""
    row = _row(Platform.DISCORD)
    if row and row.has_bot_token:
        return True
    return bool(_env("DISCORD_BOT_TOKEN"))
