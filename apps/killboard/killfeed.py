"""Configurable kill-feed: post sizeable corp kills/losses to Discord.

Officers tune the value thresholds; this scans recent home-corp killmails and
posts the ones that clear the bar, de-duplicated by killmail id. A short freshness
window keeps the first run from dumping history. Reuses the shared Discord poster.
"""
from __future__ import annotations

import datetime as dt

from django.utils import timezone

# Only consider killmails from the last few hours so enabling the feed (or a
# restart) never replays the whole board.
_FRESH = dt.timedelta(hours=6)
_ZKILL = "https://zkillboard.com/kill/{}/"


def _isk(value) -> str:
    v = float(value)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:.0f}"


def run_kill_feed(client_post=None) -> dict:
    """Post new qualifying corp killmails to Discord. ``client_post`` is injectable."""
    from apps.sde.models import SdeType

    from .models import KillFeedConfig, KillFeedPing, Killmail

    cfg = KillFeedConfig.load()
    if not cfg.enabled:
        return {"status": "disabled", "posted": 0}

    # Also honour the notification-console toggle for this event (leadership's one-stop
    # switchboard), on top of the kill-feed's own config.
    from apps.pingboard import notifications

    if not notifications.is_enabled("killboard.killfeed"):
        return {"status": "disabled", "posted": 0}

    post = client_post
    if post is None:
        from apps.recommendations.notify import broadcast_discord
        post = broadcast_discord

    from django.db.models import Count, Q

    from . import killfeed_rules

    since = timezone.now() - _FRESH
    already = set(KillFeedPing.objects.values_list("killmail_id", flat=True))
    candidates = list(
        Killmail.objects.filter(involves_home_corp=True, killmail_time__gte=since)
        .exclude(killmail_id__in=already)
        .annotate(attacker_count=Count("participants", filter=Q(participants__role="attacker")))
        .order_by("killmail_time")
    )
    hull_ids = {k.victim_ship_type_id for k in candidates}
    ship_names = dict(
        SdeType.objects.filter(type_id__in=hull_ids).values_list("type_id", "name")
    )
    # KB-24: batch the rule inputs once, then evaluate each mail against the require/exclude
    # clauses. All-default rules reduce this to the original ISK-threshold check.
    ship_classes = killfeed_rules.ship_classes_for(hull_ids)
    staging_distance = killfeed_rules.staging_distances(cfg)

    posted = 0
    for km in candidates:
        if not killfeed_rules.evaluate(
            km, cfg,
            attacker_count=km.attacker_count,
            ship_class=ship_classes.get(km.victim_ship_type_id, "Other"),
            staging_distance=staging_distance,
        ):
            continue  # blocked by a rule — leave unmarked, the window bounds re-checks
        is_loss = km.home_corp_role == Killmail.HomeRole.VICTIM
        ship = ship_names.get(km.victim_ship_type_id, f"Type {km.victim_ship_type_id}")
        verb = "💥 **Loss**" if is_loss else "🔫 **Kill**"
        post(f"{verb}: {ship} ({_isk(km.total_value)} ISK) — {_ZKILL.format(km.killmail_id)}")
        KillFeedPing.objects.get_or_create(killmail_id=km.killmail_id)
        posted += 1
    return {"status": "ok", "posted": posted}
