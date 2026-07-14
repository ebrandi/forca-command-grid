"""Telegram provider — Bot API ``sendMessage``.

Everything a Telegram channel needs is configured in the Admin Console and stored on the
``ChannelProvider`` row: the **bot token** as the Fernet‑encrypted secret and the target
group/channel id in ``routing['chat_id']``. Leadership never edits the server env.

Token resolution order: this row's stored secret → ``settings.PINGBOARD_TELEGRAM_BOT_TOKEN``
(legacy env fallback) → any other enabled Telegram channel's stored token (so per‑pilot DMs,
which dispatch with ``provider=None``, work off the same UI‑configured token).

Sends to a ``chat_id``: the row's group/channel, or an individual pilot's verified
``PilotContactChannel`` handle (the pilot must have ``/start``‑ed the bot first).
"""
from __future__ import annotations

from django.conf import settings
from django.utils.translation import gettext as _

from ._http import _json_body, post_json
from .base import AlertProvider, Recipient, SendResult


class TelegramProvider(AlertProvider):
    kind = "telegram"
    supports_direct = True
    supports_group = True
    supports_channel = True

    def _allowed(self):
        return settings.PINGBOARD_TELEGRAM_ALLOWED_HOSTS

    def _token(self) -> str:
        """Bot token: this row's secret, else env, else any enabled Telegram channel's token."""
        if self.row is not None and self.row.secret:
            return self.row.secret
        if settings.PINGBOARD_TELEGRAM_BOT_TOKEN:
            return settings.PINGBOARD_TELEGRAM_BOT_TOKEN
        from apps.pingboard.models import ChannelProvider

        row = (
            ChannelProvider.objects.filter(kind="telegram", enabled=True)
            .exclude(_secret="")
            .first()
        )
        return row.secret if row else ""

    def validate_configuration(self) -> tuple[bool, str]:
        if not self._token():
            return False, _("Bot token not set — add it on the channel")
        # A broadcast channel row must also carry a target chat id (DMs don't).
        if self.row is not None and not (self.row.routing or {}).get("chat_id"):
            return False, _("Target chat id not set")
        return True, ""

    def send(self, *, subject: str, body: str, recipients: list[Recipient]) -> SendResult:
        token = self._token()
        if not token:
            return SendResult(ok=False, skipped=True, error="Telegram not configured")

        targets = [r.recipient_ref for r in recipients if r.recipient_ref]
        if not targets and self.row:
            chat = (self.row.routing or {}).get("chat_id")
            if chat:
                targets = [chat]
        if not targets:
            return SendResult(ok=False, skipped=True, error="no Telegram target")

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        ok_n = fail_n = 0
        last = ""
        for chat_id in targets:
            resp, err = post_json(url, self._allowed(), json={"chat_id": chat_id, "text": body})
            data = _json_body(resp) if resp is not None else {}
            if resp is not None and 200 <= resp.status_code < 300 and data.get("ok"):
                ok_n += 1
                last = str((data.get("result") or {}).get("message_id", "")) or last
            else:
                fail_n += 1
        return SendResult(ok=ok_n > 0, recipients_ok=ok_n, recipients_failed=fail_n,
                          provider_message_id=last, error="" if ok_n else "telegram send failed")
