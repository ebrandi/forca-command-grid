"""CMD-1 (roadmap 2.11) — auto-AAR on notable battles.

Battle AARs are the flagship combat feature but on-demand only (click Analyze), so a
notable fight gets no write-up unless an officer remembers. This beat scans recent
killboard ``BattleReport``s and auto-queues an AAR when a fight crosses a leadership-set
threshold (ISK swing, our-loss count, logi lost, off-doctrine losses).

Ships **OFF** (``battle.auto_aar_enabled`` kill switch). Cost-safe: one AAR per battle
(skips any battle that already has an analysis), a per-run cap, and it rides the existing
LLM rate caps in ``request_battle_analysis`` — if the rate cap trips it stops for the run.
"""
from __future__ import annotations

from datetime import timedelta

# Bound how many candidate battles the scan computes facts for in a single run. battle_facts
# prefetches up to 500 killmails per report, so an unsliced scan over a wide lookback could
# be heavy DB load. Newest-first ordering means the recent notable fights are still reached.
_MAX_SCAN = 150


def _crosses_threshold(facts: dict, cfg: dict) -> bool:
    """True if the battle meets any *positive* configured threshold. A threshold of 0
    means "don't trigger on this metric" — never "match everything"."""
    t = facts.get("totals", {}) or {}
    checks = [
        (int(cfg.get("auto_aar_min_isk_swing", 0) or 0), abs(t.get("isk_swing", 0) or 0)),
        (int(cfg.get("auto_aar_min_our_losses", 0) or 0), t.get("our_losses", 0) or 0),
        (int(cfg.get("auto_aar_min_logi_lost", 0) or 0), t.get("logi_lost", 0) or 0),
        (int(cfg.get("auto_aar_min_off_doctrine", 0) or 0), t.get("off_doctrine_losses", 0) or 0),
    ]
    return any(threshold > 0 and value >= threshold for threshold, value in checks)


def scan_and_queue_aars() -> dict:
    """Queue AARs for recent notable battles that don't have one yet. No-op when the kill
    switch is off. Returns a summary."""
    from django.utils import timezone

    from apps.killboard.models import BattleReport

    from . import config
    from .battle import battle_facts
    from .models import BattleAnalysis
    from .services import request_battle_analysis

    cfg = config.get("battle")
    if not cfg.get("auto_aar_enabled", False):
        return {"status": "disabled"}

    lookback = max(1, int(cfg.get("auto_aar_lookback_hours", 6) or 6))
    max_per_run = max(1, int(cfg.get("auto_aar_max_per_run", 3) or 3))
    since = timezone.now() - timedelta(hours=lookback)

    # Recent battles with NO analysis yet (one AAR per battle — request_battle_analysis
    # only dedups in-flight, so we exclude any prior analysis here regardless of status).
    reports = (
        BattleReport.objects.filter(start_time__gte=since)
        .exclude(pk__in=BattleAnalysis.objects.values("battle_report_id"))
        .order_by("-start_time")[:_MAX_SCAN]
    )

    queued = 0
    scanned = 0
    for report in reports:
        if queued >= max_per_run:
            break
        scanned += 1
        if not _crosses_threshold(battle_facts(report), cfg):
            continue
        result = request_battle_analysis(user=None, battle_report_id=report.pk)
        # A FAILED result means the LLM rate cap tripped — stop queuing more this run.
        if result is not None and result.status == BattleAnalysis.Status.FAILED:
            break
        queued += 1

    return {"status": "ok", "scanned": scanned, "queued": queued}
