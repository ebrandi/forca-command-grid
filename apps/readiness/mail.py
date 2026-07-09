"""Readiness EVE-mail outbound (design doc 13 §4).

Sends readiness alert e-mails in-game from a leadership-chosen *sender* character
(``readiness.notifications.eve_mail_sender_character_id``) that has granted the
``esi-mail.send_mail.v1`` scope. Best-effort: with no sender configured, no valid send
token, or no recipient it degrades to a no-op — never blocking the alert record, exactly
like the Discord primitive. Recipients are the responsible officer's character(s), so a
gap reaches the desk that owns it.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SEND_SCOPE = "esi-mail.send_mail.v1"


def configured_sender_id():
    from . import config as config_module

    return (config_module.get("notifications") or {}).get("eve_mail_sender_character_id")


def eve_mail_sender():
    """The configured sender :class:`EveCharacter`, or ``None`` if unset/unknown."""
    from apps.sso.models import EveCharacter

    cid = configured_sender_id()
    if not cid:
        return None
    return EveCharacter.objects.filter(character_id=cid).first()


def _has_send_scope(character) -> bool:
    from apps.sso.models import AuthToken

    return any(
        SEND_SCOPE in (t.scopes or [])
        for t in AuthToken.objects.filter(character=character, revoked_at__isnull=True)
    )


def eligible_senders() -> list[dict]:
    """Director-owned corp characters that can act as the sender, with grant status.

    Rows ``{character_id, name, granted}`` for every home-corp character whose user holds
    the Director role — so leadership can pick one and see whether it has granted the send
    scope yet.
    """
    from apps.sso.models import EveCharacter
    from core import rbac

    rows = []
    for ch in (EveCharacter.objects.filter(is_corp_member=True, user__isnull=False)
               .select_related("user").order_by("name")):
        if not rbac.has_role(ch.user, rbac.ROLE_DIRECTOR):
            continue
        rows.append({
            "character_id": ch.character_id,
            "name": ch.name,
            "granted": _has_send_scope(ch),
        })
    return rows


def owner_recipient_ids(owner_tag: str, responsibilities: dict) -> list[int]:
    """Character ids of the users mapped to an owner desk (each user's main, else any)."""
    if not owner_tag:
        return []
    users = ((responsibilities.get("owner_tags") or {}).get(owner_tag) or {}).get("users") or []
    if not users:
        return []
    from apps.sso.models import EveCharacter

    ids = []
    for uid in users:
        ch = (EveCharacter.objects.filter(user_id=uid, is_main=True).first()
              or EveCharacter.objects.filter(user_id=uid).first())
        if ch:
            ids.append(ch.character_id)
    return ids


def send_mail(subject: str, body: str, recipient_ids) -> bool:
    """Send one EVE-mail from the configured sender. ``True`` if delivered, else ``False``.

    Never raises: a missing sender / token / recipient, or any ESI error, degrades to
    ``False`` so the alert record is still written.
    """
    recipient_ids = [int(c) for c in (recipient_ids or []) if c]
    if not recipient_ids:
        return False
    sender = eve_mail_sender()
    if sender is None:
        return False

    # Delivery goes through Pingboard's unified EVE-mail provider; the sender + its
    # config namespace stay here (ADR-0001 isolation). Best-effort: never raises.
    from apps.pingboard.compat import send_eve_mail

    return send_eve_mail(subject, body, recipient_ids, sender.character_id)
