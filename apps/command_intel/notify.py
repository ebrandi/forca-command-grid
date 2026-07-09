"""Scheduled-report delivery — classification-aware Discord + EVE-mail (design doc 14 §6).

A ready scheduled report is announced to the channels leadership has ARMED, but only to
channels whose allowed-classification list includes the report's tier: a
``director_eyes_only`` report is never broadcast to a corp-wide Discord channel (the
unsafe-pairing rule, enforced both here and by the ``notifications`` config validator).
Delivery is best-effort and **deliver-once** — each channel is attempted at most once per
report (tracked on ``report.delivered_channels``). It ships **disarmed**: the
``deliver_discord`` / ``deliver_evemail`` flags default off, so nothing leaves the server
until a director configures a webhook / sender. Discord reuses the shared SSRF-guarded
``recommendations.notify.broadcast_discord``; EVE-mail uses the same SSO token + ESI client
primitives as the readiness mailer, reading this app's own config (ADR-0001 isolation).
"""
from __future__ import annotations

import logging

from . import config
from .engine.base import SEVERITY_ORDER

logger = logging.getLogger("forca.command_intel")


def _site_base() -> str:
    from django.conf import settings

    base = getattr(settings, "FORCA_SITE_URL", "") or getattr(settings, "SITE_URL", "")
    return (base or "").rstrip("/")


def _report_link(report) -> str:
    return f"{_site_base()}/command/reports/{report.pk}/"


def _binding_constraints(report) -> list:
    """Computed, non-``info`` constraints for the report's snapshot, most-binding first."""
    from .models import OperationalConstraint

    if report.snapshot_id is None:
        return []
    rows = [
        c for c in OperationalConstraint.objects.filter(snapshot_id=report.snapshot_id)
        if c.status == "computed" and c.severity != "info"
    ]
    return sorted(rows, key=lambda c: SEVERITY_ORDER.get(c.severity, 0), reverse=True)


def _passes_min_severity(report, cfg: dict) -> bool:
    """Only announce a report that actually has a binding constraint worth the noise."""
    floor = SEVERITY_ORDER.get(cfg.get("min_severity_to_deliver", "watch"), 1)
    return any(SEVERITY_ORDER.get(c.severity, 0) >= floor for c in _binding_constraints(report))


def _summary_text(report) -> str:
    """A compact, classification-safe digest: title, narrative gist, top binding constraints."""
    lines = [f"**{report.title or 'Command Intelligence Report'}**"]
    if report.summary:
        lines.append(report.summary[:400])
    for c in _binding_constraints(report)[:3]:
        metric = f"{c.binding_metric:g} {c.unit}".strip() if c.binding_metric is not None else "—"
        hr = f" (headroom {c.headroom:+g})" if c.headroom is not None else ""
        lines.append(f"• [{c.severity.upper()}] {c.label}: {metric}{hr}")
    link = _report_link(report)
    if link.startswith("http"):
        lines.append(link)
    return "\n".join(lines)


def deliver_report(report) -> dict:
    """Announce a ready report to every ARMED, classification-cleared channel.

    Returns the per-channel delivered counts (merged into ``report.delivered_channels``).
    Best-effort: a channel failure is logged, never raised. Deliver-once per channel, so a
    re-run never double-posts.
    """
    cfg = config.get("notifications")
    delivered = dict(report.delivered_channels or {})
    if report.status not in ("ready", "ready_degraded"):
        return delivered
    # Master off-switch: the notifications console can silence this event corp-wide,
    # on top of command_intel's own arming (deliver_discord / deliver_evemail flags).
    from apps.pingboard import notifications

    if not notifications.is_enabled("command_intel.report"):
        return delivered
    if not _passes_min_severity(report, cfg):
        return delivered

    # Defense in depth: the broadcast-forbidden tiers (director/alliance) can NEVER be
    # posted to a corp-wide Discord channel, whatever the stored config says — the
    # guarantee holds at the sink, not only at config-write time (doc 14 §6).
    discord_allowed = set(cfg.get("discord_classifications") or []) - config._BROADCAST_FORBIDDEN
    if (cfg.get("deliver_discord") and "discord" not in delivered
            and report.classification in discord_allowed):
        n = _deliver_discord(report)
        if n:  # only record a real send, so a down/unconfigured channel stays retriable
            delivered["discord"] = n

    if (cfg.get("deliver_evemail") and "evemail" not in delivered
            and report.classification in (cfg.get("evemail_classifications") or [])):
        n = _deliver_evemail(report, cfg)
        if n:
            delivered["evemail"] = n

    if delivered != (report.delivered_channels or {}):
        report.delivered_channels = delivered
        report.save(update_fields=["delivered_channels", "updated_at"])
    return delivered


def _deliver_discord(report) -> int:
    from apps.recommendations.notify import broadcast_discord

    try:
        # Pass the report's classification so the broadcast honours each channel's
        # ``max_classification`` ceiling — the guard now holds across every armed chat
        # channel (Discord/Slack/Telegram/WhatsApp), not just the Discord webhook.
        return broadcast_discord(_summary_text(report), classification=report.classification)
    except Exception:  # noqa: BLE001 - delivery is best-effort, never fail the run
        logger.exception("command_intel discord delivery failed for report %s", report.pk)
        return 0


def _deliver_evemail(report, cfg: dict) -> int:
    recipients = _recipient_ids(cfg)
    if not recipients:
        return 0
    subject = (report.title or "Command Intelligence Report")[:1000]
    body = _summary_text(report).replace("**", "")[:9500]
    return 1 if _send_mail(subject, body, recipients, cfg) else 0


def _recipient_ids(cfg: dict) -> list[int]:
    """Character ids of the mains behind the configured leadership owner tags."""
    from apps.sso.models import EveCharacter

    resp = config.get("responsibilities").get("owner_tags") or {}
    user_ids: set = set()
    for tag in (cfg.get("evemail_owner_tags") or []):
        mapping = resp.get(tag)
        users = mapping.get("users") if isinstance(mapping, dict) else mapping
        for uid in (users or []):
            user_ids.add(uid)
    if not user_ids:
        return []
    ids: list[int] = []
    for user_id in user_ids:
        chars = list(EveCharacter.objects.filter(user_id=user_id))
        main = next((c for c in chars if getattr(c, "is_main", False)), chars[0] if chars else None)
        if main is not None:
            ids.append(main.character_id)
    return ids


def _send_mail(subject: str, body: str, recipient_ids: list[int], cfg: dict) -> bool:
    """Send one in-game mail from the configured sender; never raises (returns False).

    Delivery goes through Pingboard's unified EVE-mail provider; command_intel keeps its
    own sender config (``evemail_sender_character_id``) per its ADR-0001 isolation.
    """
    sender_id = cfg.get("evemail_sender_character_id")
    if not sender_id:
        return False
    from apps.pingboard.compat import send_eve_mail

    return send_eve_mail(subject, body, recipient_ids, sender_id)
