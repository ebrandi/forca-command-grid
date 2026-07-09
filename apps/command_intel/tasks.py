"""Celery tasks for Command Intelligence (design doc 06, ADR-0008).

Thin wrappers with lazy imports (the codebase task convention). All LLM I/O and
snapshot building happens here, in a worker — never in a web request.
"""
from __future__ import annotations

from celery import shared_task


@shared_task(name="command_intel.generate_report")
def generate_report(report_id: int, force_rebuild: bool = False) -> str:
    """Run the full report-generation pipeline for a queued report row."""
    from .models import IntelligenceReport
    from .report import run_generation

    report = IntelligenceReport.objects.filter(pk=report_id).first()
    if report is None:
        return "missing"
    run_generation(report, force_rebuild=force_rebuild)
    return report.status


@shared_task(name="command_intel.build_snapshot")
def build_snapshot(trigger: str = "scheduled") -> int:
    """Build + persist a fresh Intelligence Snapshot (warms the cached latest)."""
    from .snapshot import build_snapshot as _build

    snap = _build(trigger=trigger, persist=True)
    return snap.pk


@shared_task(name="command_intel.measure_outcome")
def measure_outcome(coa_id: int) -> str:
    """Measure a completed COA's predicted-vs-actual effect (the calibration loop)."""
    from .models import CourseOfAction
    from .outcomes import measure_outcome as _measure

    coa = CourseOfAction.objects.filter(pk=coa_id).first()
    if coa is None:
        return "missing"
    outcome = _measure(coa)
    return str(outcome.pk) if outcome else "skipped"


@shared_task(name="command_intel.scheduled_report")
def scheduled_report() -> str:
    """Generate + deliver the weekly unattended briefing (P5).

    Inert until ``notifications.scheduled_enabled``; deduped against a recent scheduled
    run so a retried beat never double-spends tokens. Delivery is classification-aware.
    """
    from .scheduled import run_scheduled_report

    return run_scheduled_report()


@shared_task(name="command_intel.housekeeping")
def housekeeping() -> dict:
    """Retention pruning for CI's churn tables (P5, doc 03 §8).

    Age-based ⇒ convergent: a missed night is caught up the next run. Institutional
    records (reports, COAs, outcomes) are kept — only resolved directives and orphan
    snapshots are pruned. A snapshot referenced by a report is held by its PROTECT FK
    and never touched.
    """
    import datetime as dt

    from django.utils import timezone

    from .models import IntelligenceSnapshot, PilotDirective

    now = timezone.now()
    counts: dict[str, int] = {}
    counts["directives"] = PilotDirective.objects.filter(
        state__in=[PilotDirective.State.DONE, PilotDirective.State.DISMISSED],
        updated_at__lt=now - dt.timedelta(days=60),
    ).delete()[0]
    orphan_snapshots = (
        IntelligenceSnapshot.objects.filter(created_at__lt=now - dt.timedelta(days=180))
        .exclude(reports__isnull=False)
        .exclude(baseline_for_coas__isnull=False)
        .exclude(outcome_baseline_for__isnull=False)
        .exclude(outcome_result_for__isnull=False)
    )
    counts["snapshots"] = orphan_snapshots.delete()[0]
    return counts


@shared_task(name="command_intel.answer_question")
def answer_question(turn_id: int) -> str:
    """Answer one conversational turn in the worker (P7; ADR-0008: no LLM in a web request)."""
    from .ask import answer_question as _answer
    from .models import ConversationTurn

    turn = ConversationTurn.objects.filter(pk=turn_id).first()
    if turn is None:
        return "missing"
    _answer(turn)
    return turn.status


@shared_task(name="command_intel.autonomous_propose")
def autonomous_propose() -> dict:
    """Guard-railed autonomous COA proposal (P7). Inert unless armed (the kill switch)."""
    from .autonomous import run_autonomous_proposals

    return run_autonomous_proposals()


@shared_task(name="command_intel.analyze_battle")
def analyze_battle(analysis_id: int) -> str:
    """Generate a battle after-action review in the worker (Combat Intelligence; ADR-0008)."""
    from .battle_analysis import run_battle_analysis
    from .models import BattleAnalysis

    analysis = BattleAnalysis.objects.filter(pk=analysis_id).first()
    if analysis is None:
        return "missing"
    run_battle_analysis(analysis)
    return analysis.status


@shared_task(name="command_intel.auto_aar")
def auto_aar() -> dict:
    """CMD-1 (2.11): scan recent battles and queue an AAR for any that cross a threshold.
    No-op when the kill switch is off; cost-safe (one per battle, per-run cap, rate caps)."""
    from .auto_aar import scan_and_queue_aars

    return scan_and_queue_aars()
