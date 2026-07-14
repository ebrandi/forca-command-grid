"""EVE Mail provider — the unified ESI mailer (subsumes the two duplicated senders).

Wraps the exact working call in ``apps.readiness.mail.send_mail``: scope
``esi-mail.send_mail.v1``, ``POST /characters/{sender}/mail/``, payload
``{approved_cost, subject[:1000], body[:9500], recipients:[{recipient_id, recipient_type}]}``.
Improvements over the legacy senders: recipient chunking to ESI's ~50 cap, and it
captures the returned ``mail_id``. Never raises — degrades to a recorded failure.

Sender = a Director-owned character configured in ``ChannelProvider.routing
['sender_character_id']`` that granted the ``pingboard_mail`` scope.
"""
from __future__ import annotations

import logging

from django.utils.translation import gettext as _

from .base import AlertProvider, Recipient, SendResult

log = logging.getLogger("forca.pingboard")

SEND_SCOPE = "esi-mail.send_mail.v1"
_MAX_RECIPIENTS = 50


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class EveMailProvider(AlertProvider):
    kind = "eve_mail"
    supports_direct = True
    supports_group = True

    def _sender(self):
        cid = (self.row.routing or {}).get("sender_character_id") if self.row else None
        if not cid:
            return None
        from apps.sso.models import EveCharacter

        return EveCharacter.objects.filter(character_id=cid).first()

    def validate_configuration(self) -> tuple[bool, str]:
        sender = self._sender()
        if sender is None:
            return False, _("No sender character configured")
        from apps.sso.token_service import NoValidToken, get_valid_access_token

        try:
            get_valid_access_token(sender, [SEND_SCOPE])
        except NoValidToken:
            return False, _("Sender character has not granted the mail-send scope")
        return True, ""

    def send(self, *, subject: str, body: str, recipients: list[Recipient]) -> SendResult:
        sender = self._sender()
        if sender is None:
            return SendResult(ok=False, error="no EVE-mail sender configured")

        ids = []
        for r in recipients:
            if r.recipient_type == "character" and r.recipient_ref:
                try:
                    ids.append(int(r.recipient_ref))
                except (TypeError, ValueError):
                    continue
        ids = sorted(set(ids))
        if not ids:
            return SendResult(ok=False, skipped=True, error="no resolvable recipients")

        from apps.sso.token_service import NoValidToken, get_valid_access_token
        from core.esi.client import ESIError, get_client

        try:
            token = get_valid_access_token(sender, [SEND_SCOPE])
        except NoValidToken:
            return SendResult(ok=False, error="sender has no valid mail token")

        ok = 0
        failed = 0
        last_id = ""
        for chunk in _chunks(ids, _MAX_RECIPIENTS):
            payload = {
                "approved_cost": 0,
                "subject": (subject or "Alert")[:1000],
                "body": (body or "")[:9500],
                "recipients": [{"recipient_id": c, "recipient_type": "character"} for c in chunk],
            }
            try:
                resp = get_client().post(
                    f"/characters/{sender.character_id}/mail/", json=payload, token=token
                )
                ok += len(chunk)
                data = getattr(resp, "data", "")
                if data:
                    last_id = str(data)
            except ESIError as exc:
                log.warning("Pingboard EVE-mail: ESI rejected the send: %s", type(exc).__name__)
                failed += len(chunk)
            except Exception:  # noqa: BLE001 - delivery must never break the dispatcher
                log.exception("Pingboard EVE-mail: unexpected send failure")
                failed += len(chunk)

        return SendResult(
            ok=ok > 0,
            recipients_ok=ok,
            recipients_failed=failed,
            provider_message_id=last_id,
            error="" if ok else "all recipients failed",
        )
