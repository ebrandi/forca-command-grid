"""Alert dispatch: in-app alerts and optional Discord webhooks."""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import requests
from django.utils import timezone, translation

from .models import Alert, Recommendation

log = logging.getLogger("forca.notify")

# Only ever POST to genuine Discord webhook hosts (SSRF guard).
_ALLOWED_WEBHOOK_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
_MAX_DISCORD_CONTENT = 2000  # Discord's hard per-message limit


def _post_discord(url: str, content: str) -> None:
    if not url:
        return
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_WEBHOOK_HOSTS
        or not parsed.path.startswith("/api/webhooks/")
    ):
        log.warning("Refusing non-Discord webhook URL")
        return
    payload = {
        "content": (content or "")[:_MAX_DISCORD_CONTENT],
        # Message text is built from member/officer-supplied fields (op names,
        # timer notes); never let it mass-ping the server via @everyone/@here or
        # role/user mentions.
        "allowed_mentions": {"parse": []},
    }
    try:
        # Don't follow redirects: the host allowlist is checked on the initial URL
        # only, so a 3xx could otherwise bounce this POST (and its body) to an
        # attacker-chosen host.
        requests.post(url, json=payload, timeout=10, allow_redirects=False)
    except requests.RequestException as exc:
        log.warning("Discord webhook failed: %s", exc)


def broadcast_discord(content: str, *, classification: str | None = None) -> int:
    """Post a plain message to every armed broadcast channel.

    Despite the historical name, this fans out across ALL enabled Pingboard chat
    channels — Discord, Slack, Telegram and WhatsApp — via the Pingboard
    ``ChannelProvider`` registry, so every caller (fleet pings, readiness/command-intel
    digests, mail relay) reaches whatever the corp has armed. Returns how many channels
    were posted to (0 = nothing delivered; callers treat >0 as delivered). Each post is
    best-effort and SSRF-guarded. ``classification`` (a Command-Intelligence tier) skips
    any channel whose ``max_classification`` ceiling is below it.
    """
    from apps.pingboard.services import broadcast_text

    return broadcast_text(content, classification=classification)


def _canonical_type_label(rec: Recommendation) -> str:
    """``rec``'s type label in canonical English — the value that is safe to PERSIST.

    ``rec.get_type_display()`` returns the ``gettext_lazy`` proxy behind ``Type.choices``. That
    proxy is correct *at display time*, but ``Alert.title`` is a stored column: resolving it here
    freezes whatever locale happens to be active in the Celery worker that ran the dispatch beat
    into a row that officers of every other locale then read. The row would show the WORKER's
    language, not the reader's — and it would change meaning depending on which worker won.

    So the write side stores the stable English label, and the *display* side stays free to
    translate: ``Alert.recommendation`` is a live FK, so any renderer can call
    ``alert.recommendation.get_type_display()`` and get the reader's locale. The i18n boundary
    belongs there, at read time — never frozen into the row.
    """
    with translation.override("en"):
        try:
            return str(Recommendation.Type(rec.type).label)
        except ValueError:  # a type not in the enum (legacy row) — store it verbatim
            return str(rec.type)


def dispatch_alerts(min_severity: int | None = None) -> int:
    """Create in-app alerts (and fan out to armed chat channels) for high-severity recs.

    Recommendations are **officer/leadership** content. The in-app record always stands
    (it powers the officer dashboard, which is already role-gated), but the *chat
    broadcast* is governed by the ``recommendations.officer_digest`` notification event:
    it fires only when leadership has left the event enabled, and it carries the event's
    audience-derived classification (``high_command`` by default), so it is dropped by
    every chat channel that has not been designated a leadership channel. The severity
    floor also comes from the event policy unless a caller pins one explicitly.
    """
    from apps.pingboard import notifications

    policy = notifications.resolve("recommendations.officer_digest")
    floor = min_severity if min_severity is not None else (policy.get("min_severity") or 50)
    broadcast_enabled = policy["enabled"]
    classification = policy["classification"]

    sent = 0
    for rec in Recommendation.objects.filter(
        state=Recommendation.State.NEW, severity__gte=floor
    ):
        if rec.alerts.exists():
            continue
        Alert.objects.create(
            recommendation=rec,
            title=_canonical_type_label(rec),
            body=rec.message,
            severity=rec.severity,
            channel=Alert.Channel.IN_APP,
            dispatched_at=timezone.now(),
        )
        sent += 1
        # Fan the rec out to armed chat channels — but only to leadership-cleared ones,
        # never a mass corp channel (classification gate at the sink).
        if broadcast_enabled:
            broadcast_discord(rec.message, classification=classification)
    return sent
