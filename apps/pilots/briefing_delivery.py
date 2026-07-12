"""Deliver the daily leadership briefing out of the app — to Discord and email.

The in-app briefing (``leadership_briefing``) is the source of truth; this only
*formats and ships* it on a schedule so officers see the corp's state without
logging in. It adds no new data. Both channels are opt-in and degrade to no-ops:
Discord posts only if a webhook channel exists, email sends only if recipients
are configured.
"""
from __future__ import annotations

import logging

from django.conf import settings

log = logging.getLogger("forca.briefing")


def _isk(n) -> str:
    try:
        return f"{float(n):,.0f}"
    except (TypeError, ValueError):
        return "0"


def format_leadership_digest(briefing: dict | None = None) -> str:
    """Render the leadership briefing as a compact plain-text digest.

    Plain text doubles as Discord markdown — no per-channel formatting needed.
    """
    from .briefing import leadership_briefing

    b = briefing if briefing is not None else leadership_briefing()
    corp = getattr(settings, "FORCA_CORP_NAME", "Corp")
    lines = [
        f"**{corp} — daily briefing**",
        f"• Readiness index: {b.get('index', 0)}%",
        f"• Losses (24h): {b.get('losses_24h', 0)}",
        f"• Open tasks: {b.get('open_tasks', 0)} · open hauls: {b.get('open_hauls', 0)}",
        f"• Stock shortfalls: {b.get('stock_shortfalls', 0)}",
        f"• SRP exposure: {_isk(b.get('srp_exposure', 0))} ISK",
    ]
    gaps = b.get("top_gaps") or []
    if gaps:
        labels = [g.get("label") or g.get("name") or str(g) for g in gaps[:3]]
        lines.append("• Top readiness gaps: " + ", ".join(labels))
    leaders = b.get("leaderboard") or []
    if leaders:
        top = leaders[0]
        # points_leaderboard() rows expose {"user", "points"} — the user's
        # display_name (main character's name) is prefetched, so no N+1. The old
        # code read "name"/"character" keys that row never emits, so this line
        # silently rendered "—". Keep those as defensive fallbacks.
        user = top.get("user")
        name = getattr(user, "display_name", "") or top.get("name") or top.get("character") or "—"
        lines.append(f"• Top contributor: {name} ({top.get('points', 0)} pts)")
    return "\n".join(lines)


def deliver_leadership_briefing() -> dict:
    """Compose the leadership digest and ship it to Discord + email. Idempotent-safe."""
    from apps.pingboard import notifications
    from apps.recommendations.notify import broadcast_discord

    digest = format_leadership_digest()
    # This is, by name, leadership content — governed by the pilots.leadership_briefing
    # event and classification-gated so it never lands on a mass corp channel. Email goes
    # to the configured FORCA_BRIEFING_EMAILS list (already a leadership distribution).
    policy = notifications.resolve("pilots.leadership_briefing")
    discord_sent = (
        broadcast_discord(digest, classification=policy["classification"])
        if policy["enabled"] else 0
    )

    recipients = [a for a in getattr(settings, "FORCA_BRIEFING_EMAILS", []) if a]
    emailed = 0
    if recipients:
        from django.core.mail import send_mail
        from django.utils import translation
        from django.utils.translation import gettext as _

        from core.i18n import broadcast_locale

        corp = getattr(settings, "FORCA_CORP_NAME", "Corp")
        # Email body is plain text; strip the Discord bold markers. The digest is
        # corp/leadership DATA and is not re-translated; only the subject chrome localises,
        # in the corp default broadcast locale (no single recipient to key off — doc 08 §12).
        body = digest.replace("**", "")
        try:
            with translation.override(broadcast_locale()):
                subject = _("%(corp)s — daily briefing") % {"corp": corp}
            emailed = send_mail(
                subject=subject,
                message=body,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=recipients,
                fail_silently=False,
            )
        except Exception as exc:  # noqa: BLE001 - never let delivery break the beat
            log.warning("Briefing email failed: %s", exc)

    log.info("leadership briefing delivered: %s discord, %s email", discord_sent, emailed)
    return {"discord": discord_sent, "email": emailed}
