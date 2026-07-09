"""Provider abstraction — the common interface every channel adapter implements.

Adapters are **best-effort**: ``send`` never raises into the dispatcher; any failure
comes back as a ``SendResult`` with a *redacted* error (no token/URL/PII). Outbound
network calls run worker-only (never in a web request) and each adapter carries its
own host allowlist + no-redirect discipline.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass
class Recipient:
    """A resolved delivery target for one channel kind."""

    kind: str
    recipient_type: str  # user | character | discord_user | channel | phone | chat
    recipient_ref: str   # never a secret; PII (phone/chat id) is kept out of the audit trail
    user_id: int | None = None
    display: str = ""


@dataclass
class SendResult:
    ok: bool = False
    provider_message_id: str = ""
    recipients_ok: int = 0
    recipients_failed: int = 0
    error: str = ""       # REDACTED, safe to store/log
    skipped: bool = False  # provider disabled / nothing to send (not a failure)


class AlertProvider(abc.ABC):
    kind: str = ""
    supports_direct: bool = False
    supports_group: bool = False
    supports_channel: bool = False

    def __init__(self, provider_row=None):
        self.row = provider_row

    @abc.abstractmethod
    def validate_configuration(self) -> tuple[bool, str]:
        """(ok, redacted message) — is this provider ready to send?"""

    @abc.abstractmethod
    def send(self, *, subject: str, body: str, recipients: list[Recipient]) -> SendResult:
        ...

    def send_test(self, to: Recipient | None = None) -> SendResult:
        recipients = [to] if to else []
        return self.send(
            subject="Pingboard test",
            body="This is a Pingboard test message. If you can read it, the channel works.",
            recipients=recipients,
        )

    def get_delivery_status(self, provider_message_id: str) -> str | None:
        return None

    def normalise_recipient(self, raw):
        return raw
