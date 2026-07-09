"""WhatsApp provider — provider-neutral (Meta Cloud API or Twilio).

Everything is configured in the Admin Console and stored on the ``ChannelProvider`` row —
leadership never edits the server env:

* ``routing['backend']`` — ``meta`` or ``twilio``
* the **secret** (Fernet‑encrypted) — the Meta access token *or* the Twilio auth token
* ``routing`` — the non‑secret ids: ``meta_phone_id`` / ``meta_api_version`` (Meta) or
  ``twilio_sid`` / ``twilio_from`` (Twilio), plus the destination ``to`` number.

Resolution prefers this row's own values, then any other enabled WhatsApp channel's config
(so per‑pilot DMs, dispatched with ``provider=None``, reuse it), then the legacy
``settings.PINGBOARD_WHATSAPP_*`` env values as a last resort.

Limitation (surfaced in the Admin Console): outside a 24‑hour user‑initiated session both
backends require a **pre‑approved message template** — free‑form text is delivered only inside
an open session window.
"""
from __future__ import annotations

from django.conf import settings

from ._http import _json_body, post_form, post_json
from .base import AlertProvider, Recipient, SendResult


class WhatsAppProvider(AlertProvider):
    kind = "whatsapp"
    supports_direct = True

    def _allowed(self):
        return settings.PINGBOARD_WHATSAPP_ALLOWED_HOSTS

    def _fallback_row(self):
        """Any other enabled WhatsApp channel with stored creds (for provider=None DMs)."""
        from apps.pingboard.models import ChannelProvider

        return (
            ChannelProvider.objects.filter(kind="whatsapp", enabled=True)
            .exclude(_secret="")
            .first()
        )

    def _cfg(self) -> dict:
        """Resolved backend + credentials: this row → an enabled row → env."""
        own = (self.row.routing or {}) if self.row else {}
        src = self._fallback_row()
        sr = (src.routing or {}) if src else {}
        secret = (self.row.secret if self.row and self.row.secret else "") or (
            src.secret if src else ""
        )

        def pick(key, env_val):
            return own.get(key) or sr.get(key) or env_val

        return {
            "backend": pick("backend", settings.PINGBOARD_WHATSAPP_BACKEND),
            "meta_token": secret or settings.PINGBOARD_WHATSAPP_META_TOKEN,
            "meta_phone_id": pick("meta_phone_id", settings.PINGBOARD_WHATSAPP_META_PHONE_ID),
            "meta_api_version": pick("meta_api_version", settings.PINGBOARD_WHATSAPP_META_API_VERSION)
            or "v21.0",
            "twilio_sid": pick("twilio_sid", settings.PINGBOARD_WHATSAPP_TWILIO_SID),
            "twilio_token": secret or settings.PINGBOARD_WHATSAPP_TWILIO_TOKEN,
            "twilio_from": pick("twilio_from", settings.PINGBOARD_WHATSAPP_TWILIO_FROM),
        }

    def validate_configuration(self) -> tuple[bool, str]:
        cfg = self._cfg()
        backend = cfg["backend"]
        if backend == "meta":
            if cfg["meta_token"] and cfg["meta_phone_id"]:
                return True, ""
            return False, "Meta access token / phone number id not set"
        if backend == "twilio":
            if cfg["twilio_sid"] and cfg["twilio_token"] and cfg["twilio_from"]:
                return True, ""
            return False, "Twilio SID / auth token / sender number not set"
        return False, "Choose a WhatsApp backend (Meta or Twilio)"

    def _targets(self, recipients):
        targets = [r.recipient_ref for r in recipients if r.recipient_ref]
        if not targets and self.row:
            to = (self.row.routing or {}).get("to")
            if to:
                targets = [to]
        return targets

    def send(self, *, subject: str, body: str, recipients: list[Recipient]) -> SendResult:
        cfg = self._cfg()
        backend = cfg["backend"]
        targets = self._targets(recipients)
        if backend == "meta":
            if not (cfg["meta_token"] and cfg["meta_phone_id"]):
                return SendResult(ok=False, skipped=True, error="WhatsApp (Meta) not configured")
            if not targets:
                return SendResult(ok=False, skipped=True, error="no WhatsApp target")
            return self._send_meta(cfg, body, targets)
        if backend == "twilio":
            if not (cfg["twilio_sid"] and cfg["twilio_token"] and cfg["twilio_from"]):
                return SendResult(ok=False, skipped=True, error="WhatsApp (Twilio) not configured")
            if not targets:
                return SendResult(ok=False, skipped=True, error="no WhatsApp target")
            return self._send_twilio(cfg, body, targets)
        return SendResult(ok=False, skipped=True, error="WhatsApp backend not set")

    def _send_meta(self, cfg, body, targets) -> SendResult:
        ver = cfg["meta_api_version"]
        phone_id = cfg["meta_phone_id"]
        url = f"https://graph.facebook.com/{ver}/{phone_id}/messages"
        headers = {"Authorization": f"Bearer {cfg['meta_token']}"}
        ok_n = fail_n = 0
        last = ""
        for to in targets:
            payload = {"messaging_product": "whatsapp", "to": to, "type": "text",
                       "text": {"body": body}}
            resp, err = post_json(url, self._allowed(), json=payload, headers=headers)
            data = _json_body(resp) if resp is not None else {}
            if resp is not None and 200 <= resp.status_code < 300 and data.get("messages"):
                ok_n += 1
                last = (data["messages"][0] or {}).get("id", "") or last
            else:
                fail_n += 1
        return SendResult(ok=ok_n > 0, recipients_ok=ok_n, recipients_failed=fail_n,
                          provider_message_id=last, error="" if ok_n else "whatsapp (meta) send failed")

    def _send_twilio(self, cfg, body, targets) -> SendResult:
        sid = cfg["twilio_sid"]
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        auth = (sid, cfg["twilio_token"])
        sender = cfg["twilio_from"]
        ok_n = fail_n = 0
        last = ""
        for to in targets:
            data = {"From": f"whatsapp:{sender}", "To": f"whatsapp:{to}", "Body": body}
            resp, err = post_form(url, self._allowed(), data=data, auth=auth)
            body_json = _json_body(resp) if resp is not None else {}
            if resp is not None and 200 <= resp.status_code < 300:
                ok_n += 1
                last = body_json.get("sid", "") or last
            else:
                fail_n += 1
        return SendResult(ok=ok_n > 0, recipients_ok=ok_n, recipients_failed=fail_n,
                          provider_message_id=last, error="" if ok_n else "whatsapp (twilio) send failed")
