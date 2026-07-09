"""Per-pilot identity linking for DM channels (Slack / Telegram / WhatsApp).

A pilot links a channel, proves ownership, and only then receives DMs on it:

* **Telegram** — ``start_link`` mints a code; the pilot opens the ``t.me`` deep link,
  which makes their client send ``/start <code>`` to the bot; the inbound webhook
  (``views.telegram_webhook``) calls ``verify_by_code`` with the proven chat id.
* **Slack / WhatsApp** — ``start_link`` mints a code and (optionally) sends it to the
  handle via the provider; the pilot enters it back and ``confirm`` verifies.

Codes are single-use and cleared on success. PII (chat id / phone) lives only on the
``PilotContactChannel`` row, never in the audit log.
"""
from __future__ import annotations

import datetime as dt
import hmac
import secrets

from django.utils import timezone

_DM_KINDS = {"slack", "telegram", "whatsapp", "discord"}
# A verify code is a short-lived bearer token: it must not stay redeemable forever,
# so a leaked/stale code can't later be used to bind a chat id to someone's pilot.
_VERIFY_TTL = dt.timedelta(minutes=15)


def _code() -> str:
    return secrets.token_hex(4)  # 8 hex chars, unguessable, short enough for a deep link


def _code_live(row) -> bool:
    """True while the row's verify code is still within its TTL (NULL = expired)."""
    exp = getattr(row, "verify_code_expires_at", None)
    return bool(exp) and timezone.now() <= exp


def start_link(user, kind: str, handle: str = ""):
    """Create/refresh an *unverified* contact channel with a fresh, time-boxed verify code."""
    if kind not in _DM_KINDS:
        raise ValueError(f"not a DM channel kind: {kind!r}")
    from .models import PilotContactChannel

    row, _ = PilotContactChannel.objects.update_or_create(
        user=user, kind=kind,
        defaults={"handle": handle or "", "verified": False,
                  "verify_code": _code(), "verified_at": None,
                  "verify_code_expires_at": timezone.now() + _VERIFY_TTL},
    )
    return row


def verify_by_code(kind: str, code: str, handle: str):
    """Verify the channel whose code matches, binding the proven ``handle``. Returns it or None."""
    if not code:
        return None
    from .models import PilotContactChannel

    row = (PilotContactChannel.objects.filter(kind=kind, verify_code=code)
           .exclude(verify_code="").first())
    if row is None or not _code_live(row):
        return None
    row.handle = handle or row.handle
    row.verified = True
    row.verified_at = timezone.now()
    row.verify_code = ""
    row.verify_code_expires_at = None
    row.save(update_fields=["handle", "verified", "verified_at", "verify_code",
                            "verify_code_expires_at", "updated_at"])
    return row


def confirm(user, kind: str, code: str) -> bool:
    """Verify this user's own pending channel by the code they were sent."""
    from .models import PilotContactChannel

    row = PilotContactChannel.objects.filter(user=user, kind=kind).first()
    # Compare as bytes: hmac.compare_digest on str raises TypeError on non-ASCII input,
    # which would 500 on a user-submitted non-ASCII code instead of returning False.
    if (row is None or not row.verify_code or not _code_live(row)
            or not hmac.compare_digest(str(code or "").encode("utf-8", "ignore"),
                                       row.verify_code.encode("utf-8", "ignore"))):
        return False
    row.verified = True
    row.verified_at = timezone.now()
    row.verify_code = ""
    row.verify_code_expires_at = None
    row.save(update_fields=["verified", "verified_at", "verify_code",
                            "verify_code_expires_at", "updated_at"])
    return True


def unlink(user, kind: str) -> int:
    from .models import PilotContactChannel

    deleted, _ = PilotContactChannel.objects.filter(user=user, kind=kind).delete()
    return deleted


def telegram_deeplink(code: str) -> str:
    """The ``t.me`` deep link a pilot taps to verify Telegram, or '' if the bot is unset."""
    from django.conf import settings

    bot = getattr(settings, "PINGBOARD_TELEGRAM_BOT_USERNAME", "")
    return f"https://t.me/{bot}?start={code}" if bot and code else ""
