"""In-app provider — the alert is delivered by virtue of its rows existing.

The in-app inbox reads ``Alert`` + ``AlertRecipient`` rows scoped to the viewer,
so there is nothing external to send. This adapter just confirms delivery and
counts the resolved recipients.
"""
from __future__ import annotations

from .base import AlertProvider, Recipient, SendResult


class InAppProvider(AlertProvider):
    kind = "in_app"
    supports_direct = True

    def validate_configuration(self) -> tuple[bool, str]:
        return True, ""

    def send(self, *, subject: str, body: str, recipients: list[Recipient]) -> SendResult:
        return SendResult(ok=True, recipients_ok=len(recipients))
