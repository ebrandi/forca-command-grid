"""Relay interesting ESI in-game notifications (structure attacks, wars, sov, moons).

Pulled from a Director's character token (``esi-characters.read_notifications.v1``),
de-duplicated by ESI notification id. Fresh high-priority items are echoed to Discord;
the rest are stored for the on-site board. The notification ``text`` is left as the raw
ESI YAML — we surface the type with a human label rather than parsing every variant.
"""
from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

NOTIF_SCOPE = "esi-characters.read_notifications.v1"

# ESI notification type -> (human label, alert-to-Discord?)
INTERESTING: dict[str, tuple[str, bool]] = {
    "StructureUnderAttack": ("Structure under attack", True),
    "StructureLostShields": ("Structure lost shields", True),
    "StructureLostArmor": ("Structure lost armor", True),
    "StructureDestroyed": ("Structure destroyed", True),
    "StructureFuelAlert": ("Structure low on fuel", True),
    "StructureWentLowPower": ("Structure went low power", True),
    "StructureWentHighPower": ("Structure restored to high power", False),
    "StructureOnline": ("Structure onlined", False),
    "StructureAnchoring": ("Structure anchoring", False),
    "StructuresReinforcementChanged": ("Structure reinforcement window changed", False),
    "OwnershipTransferred": ("Structure ownership transferred", False),
    "WarDeclared": ("War declared", True),
    "CorpWarDeclaredV2": ("War declared", True),
    "AllWarDeclaredMsg": ("Alliance war declared", True),
    "CorpWarSurrenderMsg": ("War surrender", False),
    "WarRetractedByConcord": ("War retracted", False),
    "WarInvalid": ("War invalidated", False),
    "SovStructureReinforced": ("Sov structure reinforced", True),
    "SovCommandNodeEventStarted": ("Sov command nodes spawned", True),
    "SovStructureDestroyed": ("Sov structure destroyed", True),
    "EntosisCaptureStarted": ("Entosis capture started", True),
    "MoonminingExtractionStarted": ("Moon extraction started", False),
    "MoonminingExtractionFinished": ("Moon extraction ready to fire", False),
    "MoonminingAutomaticFracture": ("Moon auto-fractured", False),
    "MoonminingLaserFired": ("Moon laser fired", False),
}

_ALERT_FRESH = dt.timedelta(hours=2)  # don't spam the corp with the backlog on first sync

# ESI notification type → (Pingboard alert category, priority) for a fresh corp alert.
# Every ``alert=True`` type above is routed here; anything else falls back to home defence.
_ALERT_ROUTE: dict[str, tuple[str, str]] = {
    "StructureUnderAttack": ("home_defence", "urgent"),
    "StructureLostShields": ("home_defence", "urgent"),
    "StructureLostArmor": ("home_defence", "urgent"),
    "StructureDestroyed": ("home_defence", "urgent"),
    "StructureFuelAlert": ("structure_timer", "high"),
    "StructureWentLowPower": ("structure_timer", "high"),
    "WarDeclared": ("home_defence", "urgent"),
    "CorpWarDeclaredV2": ("home_defence", "urgent"),
    "AllWarDeclaredMsg": ("home_defence", "urgent"),
    "SovStructureReinforced": ("home_defence", "urgent"),
    "SovCommandNodeEventStarted": ("home_defence", "urgent"),
    "SovStructureDestroyed": ("home_defence", "urgent"),
    "EntosisCaptureStarted": ("home_defence", "urgent"),
}


def label_for(ntype: str) -> str:
    return INTERESTING.get(ntype, (ntype, False))[0]


def _emit_corp_alert(ntype: str, label: str, nid, ts) -> bool:
    """Fire a fresh ESI notification as a Pingboard corp alert across every armed channel
    (in-app + EVE-mail + Discord + Telegram + WhatsApp + Slack) with history + retry.

    Best-effort and idempotent on the ESI notification id, so a re-sync never double-alerts.
    Returns whether an alert was created (``False`` when disabled/suppressed/failed).
    """
    category, priority = _ALERT_ROUTE.get(ntype, ("home_defence", "high"))
    try:
        from apps.pingboard import notifications
        from apps.pingboard import services as pingboard

        if not notifications.is_enabled("esi.corp_alert"):
            return False
        alert = pingboard.emit_broadcast(
            category=category, priority=priority,
            title=label, body=f"🚨 {label} — {ts:%a %d %b %H:%M} EVE",
            # Only the chrome around the ESI event localises; the event label is a CCP/EVE term
            # and the timestamp is data, so both stay raw slots. ``body`` is the English audit column.
            template="recommendations.esi_corp_alert",
            context={"event_label": label, "event_time": f"{ts:%a %d %b %H:%M}"},
            source_service="recommendations", source_object_id=f"esi-notif:{nid}",
            idempotency_key=f"esi-notif:{nid}",
            reason="Automated ESI corp alert",
        )
        return alert is not None
    except Exception:  # noqa: BLE001 - an alert must never break the ESI sync
        import logging

        logging.getLogger("forca.notify").exception("corp alert emit failed for %s", nid)
        return False


def _token_character(corp_id: int):
    # REC-1 (2.10): use the leadership-designated relay character (deterministic,
    # authoritative), falling back to the first valid token only when none is set.
    from .relay import relay_character

    return relay_character(NOTIF_SCOPE)


def sync_corp_notifications(corp_id: int | None = None, client=None) -> dict:
    """Pull notifications from a Director token; store + Discord-alert new ones."""
    from core.esi.client import ESIClient, ESIError

    from .models import CorpNotification

    corp_id = corp_id or getattr(settings, "FORCA_HOME_CORP_ID", 0)
    character = _token_character(corp_id)
    if character is None:
        return {"status": "no_token", "new": 0}

    from apps.sso.token_service import get_valid_access_token

    token = get_valid_access_token(character, [NOTIF_SCOPE])
    client = client or ESIClient()
    try:
        rows = client.get(
            f"/characters/{character.character_id}/notifications/", token=token,
        ).data or []
    except ESIError:
        return {"status": "error", "new": 0}

    have = set(CorpNotification.objects.values_list("notification_id", flat=True))
    now = timezone.now()
    new = 0
    alerted = 0
    for r in rows:
        ntype = r.get("type")
        nid = r.get("notification_id")
        if ntype not in INTERESTING or not nid or nid in have:
            continue
        ts = parse_datetime(r.get("timestamp") or "") or now
        CorpNotification.objects.create(
            notification_id=nid, type=ntype, sender_id=r.get("sender_id"),
            sender_type=r.get("sender_type", ""), timestamp=ts, text=r.get("text", ""),
        )
        have.add(nid)
        new += 1
        label, alert = INTERESTING[ntype]
        if alert and (now - ts) <= _ALERT_FRESH and _emit_corp_alert(ntype, label, nid, ts):
            alerted += 1

    return {"status": "ok", "new": new, "alerted": alerted}
