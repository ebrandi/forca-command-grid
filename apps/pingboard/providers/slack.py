"""Slack provider.

Two modes:
* **Bot token** (``settings.PINGBOARD_SLACK_BOT_TOKEN``, env) → ``chat.postMessage`` to a
  channel (``ChannelProvider.routing['channel']``) or a DM (a linked Slack user id).
* **Incoming webhook** (per-``ChannelProvider`` secret, ``hooks.slack.com``) → one channel.

Slack's Web API returns HTTP 200 even on logical failure, so success requires
``response.ok`` in the JSON body. Ships inert when no token/webhook is configured.
"""
from __future__ import annotations

from django.conf import settings

from ._http import _json_body, host_allowed, post_json
from .base import AlertProvider, Recipient, SendResult

_POST_MESSAGE = "https://slack.com/api/chat.postMessage"


class SlackProvider(AlertProvider):
    kind = "slack"
    supports_channel = True
    supports_direct = True
    supports_group = True

    def _allowed(self):
        return settings.PINGBOARD_SLACK_ALLOWED_HOSTS

    def validate_configuration(self) -> tuple[bool, str]:
        if self.row and self.row.secret and host_allowed(self.row.secret, self._allowed()):
            return True, ""
        if settings.PINGBOARD_SLACK_ENABLED:
            return True, ""
        return False, "Slack bot token not configured"

    def send(self, *, subject: str, body: str, recipients: list[Recipient]) -> SendResult:
        # Webhook mode: a per-channel incoming webhook on the provider row.
        if self.row and self.row.secret and host_allowed(self.row.secret, self._allowed()):
            resp, err = post_json(self.row.secret, self._allowed(), json={"text": body})
            ok = resp is not None and 200 <= resp.status_code < 300
            return SendResult(ok=ok, recipients_ok=1 if ok else 0,
                              error=err or ("" if ok else f"slack webhook http {resp.status_code}"))

        token = settings.PINGBOARD_SLACK_BOT_TOKEN
        if not token:
            return SendResult(ok=False, skipped=True, error="Slack not configured")

        targets = [r.recipient_ref for r in recipients if r.recipient_ref]
        if not targets and self.row:
            ch = (self.row.routing or {}).get("channel")
            if ch:
                targets = [ch]
        if not targets:
            return SendResult(ok=False, skipped=True, error="no Slack target")

        headers = {"Authorization": f"Bearer {token}"}
        ok_n = fail_n = 0
        last = ""
        for channel in targets:
            resp, err = post_json(_POST_MESSAGE, self._allowed(),
                                  json={"channel": channel, "text": body}, headers=headers)
            data = _json_body(resp) if resp is not None else {}
            if resp is not None and 200 <= resp.status_code < 300 and data.get("ok"):
                ok_n += 1
                last = data.get("ts", "") or last
            else:
                fail_n += 1
        return SendResult(ok=ok_n > 0, recipients_ok=ok_n, recipients_failed=fail_n,
                          provider_message_id=last, error="" if ok_n else "slack send failed")
