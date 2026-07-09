"""Compatibility shim — route the legacy EVE-mail primitive through Pingboard's
unified provider while preserving its exact signature and semantics.

Pingboard is the single implementation of EVE-mail sending (one mailer); each caller
keeps its own sender config (ADR-0001 isolation) and only the send mechanism is unified
here. Discord broadcasting now goes directly through ``pingboard.services.broadcast_text``
(the legacy ``NotificationChannel`` registry is retired), so no Discord shim remains.
"""
from __future__ import annotations


def send_eve_mail(subject: str, body: str, recipient_ids, sender_character_id) -> bool:
    """Send one in-game mail via Pingboard's unified EVE-mail provider.

    Returns ``True`` on delivery, ``False`` on any missing sender/token/recipient or ESI
    error (never raises). The caller resolves the sender — each app keeps its own config
    namespace (ADR-0001 isolation); only the send mechanism is unified here.
    """
    if not sender_character_id:
        return False
    ids = [c for c in (recipient_ids or []) if c]
    if not ids:
        return False
    from .models import ChannelProvider
    from .providers.base import Recipient
    from .providers.evemail import EveMailProvider

    row = ChannelProvider(kind="eve_mail", routing={"sender_character_id": sender_character_id})
    recipients = [Recipient("eve_mail", "character", str(c)) for c in ids]
    return EveMailProvider(row).send(subject=subject, body=body, recipients=recipients).ok
