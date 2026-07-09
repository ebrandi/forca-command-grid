"""Relay corp/alliance mailing-list mail to Discord.

Reads the mail *headers* (never bodies) of the character that granted the mail
scope, keeps the broadcast mails (sent to a corporation / alliance / mailing
list — not personal DMs), and posts new ones to Discord. De-duplicated by mail id;
a freshness window keeps the first sync from dumping the whole backlog.
"""
from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

MAIL_SCOPE = "esi-mail.read_mail.v1"
# Recipient types that make a mail a broadcast worth relaying (skip character DMs).
_BROADCAST_TYPES = {"corporation", "alliance", "mailing_list"}
_FRESH = dt.timedelta(hours=6)  # don't spam the backlog on first sync


def _token_character(corp_id: int):
    # REC-1 (2.10): use the leadership-designated relay character (deterministic,
    # authoritative), falling back to the first valid token only when none is set.
    from .relay import relay_character

    return relay_character(MAIL_SCOPE)


def _is_broadcast(mail: dict) -> bool:
    return any(
        r.get("recipient_type") in _BROADCAST_TYPES for r in (mail.get("recipients") or [])
    )


def sync_corp_mail(corp_id: int | None = None, client=None) -> dict:
    """Pull mail headers, store + Discord-relay new broadcast mails."""
    from core.esi.client import ESIClient, ESIError

    from .models import RelayedMail
    from .notify import broadcast_discord

    corp_id = corp_id or getattr(settings, "FORCA_HOME_CORP_ID", 0)
    character = _token_character(corp_id)
    if character is None:
        return {"status": "no_token", "new": 0}

    from apps.sso.token_service import get_valid_access_token

    token = get_valid_access_token(character, [MAIL_SCOPE])
    client = client or ESIClient()
    try:
        rows = client.get(f"/characters/{character.character_id}/mail/", token=token).data or []
    except ESIError:
        return {"status": "error", "new": 0}

    have = set(RelayedMail.objects.values_list("mail_id", flat=True))
    broadcasts = [m for m in rows if m.get("mail_id") and _is_broadcast(m)]

    # Resolve sender names best-effort (corp/list/char ids).
    from apps.corporation.models import EveName

    from_ids = {m.get("from") for m in broadcasts if m.get("from")}
    names = dict(EveName.objects.filter(entity_id__in=from_ids).values_list("entity_id", "name"))

    now = timezone.now()
    new = 0
    to_post: list[str] = []
    for m in broadcasts:
        mid = m["mail_id"]
        if mid in have:
            continue
        ts = parse_datetime(m.get("timestamp") or "") or now
        subject = (m.get("subject") or "")[:255]
        sender = names.get(m.get("from"), "")
        RelayedMail.objects.create(
            mail_id=mid, subject=subject, from_id=m.get("from"),
            from_name=sender, sent_at=ts,
        )
        have.add(mid)
        new += 1
        if (now - ts) <= _FRESH:
            who = f" — {sender}" if sender else ""
            to_post.append(f"📧 **{subject or 'New mail'}**{who}")

    from apps.pingboard import notifications

    relayed = 0
    if notifications.is_enabled("mail_relay.corp_mail"):
        for msg in to_post:
            broadcast_discord(msg)
            relayed += 1
    return {"status": "ok", "new": new, "relayed": relayed}
