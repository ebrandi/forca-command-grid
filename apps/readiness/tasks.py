"""Readiness beat tasks: keep the corp index warm + auto-generate prep tasks."""
from __future__ import annotations

from celery import shared_task


@shared_task(name="readiness.warm")
def warm_readiness() -> int:
    """Recompute + cache the readiness index so no leadership page pays it cold.

    The ``refresh`` recompute also upserts the durable ``ReadinessFinding`` register
    (current gaps/risks), which the risk register, task queue and (later) alerts read.
    """
    from .services import compute_readiness

    return compute_readiness(use_cache=True, refresh=True)["index"]


@shared_task(name="readiness.snapshot")
def snapshot_readiness() -> int:
    """Persist a durable ``ReadinessSnapshot`` on a fixed cadence so the timeline,
    week-over-week deltas, forecast and weekly-report movers actually populate.

    ``readiness.warm`` recomputes + caches every 10 min but deliberately does **not**
    write history (that would be ~144 rows/day). This dedicated task calls the same
    authoritative compute with ``persist=True``: it upserts findings, runs the
    forecast pass and writes exactly one snapshot row per run. It runs 6-hourly
    (4 rows/day) — the dashboard week-delta reads the most-recent 60 snapshots and
    needs one that is ~7 days old, so the 60-row window must span well over a week;
    at 4/day it spans ~15 days. The forecast needs ≥5 points, reached in ~30h.
    """
    from .services import compute_readiness

    return compute_readiness(persist=True)["index"]


@shared_task(name="readiness.warm_pilots")
def warm_pilots() -> int:
    """Score each active corp pilot (their main) and warm the per-pilot cache.

    Populates ``PilotReadinessSnapshot`` + upserts ``PilotRecommendation`` (state
    preserved). Inactive pilots are computed on demand on first view, not here.
    """
    import logging

    from apps.sso.models import EveCharacter

    from .pilot import compute_pilot

    scored = 0
    mains = EveCharacter.objects.filter(
        is_corp_member=True, is_main=True, user__isnull=False
    ).select_related("user")
    for character in mains:
        try:
            compute_pilot(character, persist=True)
            scored += 1
        except Exception:  # noqa: BLE001 - one pilot must not break the batch
            logging.getLogger(__name__).exception(
                "readiness.warm_pilots failed for %s", character.character_id
            )
    return scored


def _first_matching_rule(finding, rules):
    """First alert rule whose ``match`` targets this finding (doc 04 §8 / doc 12 §4b).

    Phase-2 matching keys on dimension/kpi/kind only (no KPI scores exist yet);
    score/status conditions are honoured from Phase 3 when KPIs are computed. With
    no rules configured (the shipped default) nothing matches → the beat is inert.
    """
    for rule in rules:
        match = rule.get("match") or {}
        if "dimension" in match and match["dimension"] != finding.dimension_key:
            continue
        if "kpi" in match and match["kpi"] != finding.kpi_key:
            continue
        if "kind" in match and match["kind"] != finding.kind:
            continue
        return rule
    return None


@shared_task(name="readiness.evaluate_alerts")
def evaluate_alerts() -> int:
    """Evaluate alert rules over current findings (fire/escalate/resolve). Inert until
    leadership configures ``readiness.alerts`` rules."""
    from .alerts import evaluate_alerts as _evaluate

    return _evaluate()


@shared_task(name="readiness.weekly_report")
def weekly_report() -> dict:
    """Build, archive and deliver the weekly executive report."""
    from .report import weekly_report as _weekly

    return _weekly()


@shared_task(name="readiness.housekeeping")
def housekeeping() -> dict:
    """Retention pruning for the readiness history/output tables (doc 03 §6).

    Convergent: pruning by age, so a missed night is caught up the next run.
    """
    import datetime as dt

    from django.utils import timezone

    from .models import (
        ExecutiveReport,
        PilotReadinessSnapshot,
        PilotRecommendation,
        ReadinessAlert,
        ReadinessFinding,
        ReadinessSnapshot,
    )

    now = timezone.now()
    counts: dict[str, int] = {}

    # resolved findings pruned after 90 days
    counts["findings"] = ReadinessFinding.objects.filter(
        status=ReadinessFinding.Status.RESOLVED, last_seen__lt=now - dt.timedelta(days=90)
    ).delete()[0]
    # resolved alerts pruned after 180 days (a still-open alert is kept, else deleting
    # it would make evaluate_alerts see no open alert and re-fire a long-standing issue)
    counts["alerts"] = ReadinessAlert.objects.filter(
        resolved_at__isnull=False, created_at__lt=now - dt.timedelta(days=180)
    ).delete()[0]
    # done/dismissed recommendations pruned after 60 days
    counts["recommendations"] = PilotRecommendation.objects.filter(
        state__in=[PilotRecommendation.State.DONE, PilotRecommendation.State.DISMISSED],
        updated_at__lt=now - dt.timedelta(days=60),
    ).delete()[0]
    # corp snapshots kept 365 days; pilot snapshots kept 180 days
    counts["snapshots"] = ReadinessSnapshot.objects.filter(
        created_at__lt=now - dt.timedelta(days=365)
    ).delete()[0]
    counts["pilot_snapshots"] = PilotReadinessSnapshot.objects.filter(
        created_at__lt=now - dt.timedelta(days=180)
    ).delete()[0]
    # executive reports kept a year
    counts["reports"] = ExecutiveReport.objects.filter(
        created_at__lt=now - dt.timedelta(days=365)
    ).delete()[0]
    return counts


@shared_task(name="readiness.generate_tasks")
def generate_tasks() -> int:
    """Auto-create prep tasks from findings whose alert rule opts in, and cancel the
    tasks of findings that have cleared (untouched only). Idempotent; ships inert
    until leadership configures alert rules with ``generate_task: true``."""
    from apps.tasks.models import Task, TaskEvent

    from . import config as config_module
    from .models import ReadinessFinding
    from .tasks_bridge import SEVERITY_RANK, active_task_exists, task_for_finding

    scoring = config_module.get("scoring")
    rules = config_module.get("alerts").get("rules", [])
    min_weight = float(scoring.get("min_task_weight", 5.0))
    floor = SEVERITY_RANK.get(scoring.get("task_severity_floor", "warn"), 1)
    max_per_run = int(scoring.get("max_tasks_per_run", 25))

    created = 0
    eligible = (
        ReadinessFinding.objects.filter(status=ReadinessFinding.Status.OPEN, task__isnull=True)
        .order_by("-weight")
    )
    for finding in eligible:
        if created >= max_per_run:
            break  # the worst gaps go first; the rest wait for the next run
        rule = _first_matching_rule(finding, rules)
        if rule is None or not rule.get("generate_task"):
            continue
        if finding.weight < min_weight or SEVERITY_RANK.get(finding.severity, 1) < floor:
            continue
        if active_task_exists(finding):
            continue
        task_for_finding(finding, user=None)
        created += 1

    # Reconcile closures: a resolved finding whose task is still open AND untouched
    # (no assignee, no events) is auto-cancelled. A claimed/started task is left alone.
    for finding in ReadinessFinding.objects.filter(
        status=ReadinessFinding.Status.RESOLVED, task__isnull=False
    ).select_related("task"):
        task = finding.task
        if task and task.status == Task.Status.OPEN and task.assignee_id is None and not task.events.exists():
            task.status = Task.Status.CANCELLED
            task.save(update_fields=["status", "updated_at"])
            TaskEvent.objects.create(task=task, from_status="open", to_status="cancelled", actor=None)

    return created
