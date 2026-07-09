"""Provider registry — in-app, Discord (webhook), EVE-mail, Slack, Telegram, WhatsApp.

A channel kind with no registered class is recorded ``SKIPPED`` (never an error).
"""
from __future__ import annotations

from .base import AlertProvider, Recipient, SendResult
from .discord import DiscordProvider
from .evemail import EveMailProvider
from .inapp import InAppProvider
from .slack import SlackProvider
from .telegram import TelegramProvider
from .whatsapp import WhatsAppProvider

PROVIDERS: dict[str, type[AlertProvider]] = {
    InAppProvider.kind: InAppProvider,
    DiscordProvider.kind: DiscordProvider,
    EveMailProvider.kind: EveMailProvider,
    SlackProvider.kind: SlackProvider,
    TelegramProvider.kind: TelegramProvider,
    WhatsAppProvider.kind: WhatsAppProvider,
}


def provider_class(kind: str) -> type[AlertProvider] | None:
    return PROVIDERS.get(kind)


__all__ = ["AlertProvider", "Recipient", "SendResult", "PROVIDERS", "provider_class"]
