"""Access-provider registry.

A provider drives one external platform's role/group membership. The registry is the
one-line seam for adding a platform (the ``apps.pingboard.providers`` idiom).

With no provider for a platform the reconcile engine records ``SKIPPED`` and never touches
anything. Discord (5.1) is registered below; Slack/Mumble follow only when a corp requests
them (their entries stay absent so arming those platforms remains a no-op until then).
"""
from __future__ import annotations

from .base import AccessProvider, ApplyResult
from .discord import DiscordAccessProvider

PROVIDERS: dict[str, type[AccessProvider]] = {
    DiscordAccessProvider.platform: DiscordAccessProvider,
}


def provider_class(platform: str) -> type[AccessProvider] | None:
    return PROVIDERS.get(platform)


__all__ = ["AccessProvider", "ApplyResult", "PROVIDERS", "provider_class"]
