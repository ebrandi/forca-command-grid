"""Public service API for the web tier (design doc 06 §1).

The web tier calls ONLY this module — it requests a report (enqueues a Celery job)
and reads finished, persisted reports; it never builds a snapshot or calls the LLM
itself (sync-vs-serve, ADR-0008). COA decisions are thin wrappers over ``coa``.
"""
from __future__ import annotations

from django.db import transaction

from . import config
from .campaign import (  # noqa: F401 - re-exported public API
    abandon_campaign,
    compose_campaign,
    launch_campaign,
)
from .coa import accept_coa, dismiss_coa  # noqa: F401 - re-exported public API
from .models import IntelligenceReport

_IN_FLIGHT = [
    IntelligenceReport.Status.QUEUED,
    IntelligenceReport.Status.BUILDING_SNAPSHOT,
    IntelligenceReport.Status.COMPUTING_CONSTRAINTS,
    IntelligenceReport.Status.CALLING_LLM,
    IntelligenceReport.Status.VALIDATING,
]


def _llm_rate_exceeded(user, bucket: str) -> str | None:
    """Enforce the provider per-hour AND per-day LLM caps for ``bucket``, per user.

    Returns an error message if the user is over a cap (so no LLM call should be
    enqueued), else None. Shared by report / battle / answer generation so a runaway
    loop can't drive unbounded external-LLM token spend from the web tier — the report
    and battle-AAR paths previously enforced only in-flight dedup, and
    ``rate_limit_per_day`` was defined but wired nowhere.
    """
    from django.core.cache import cache

    provider = config.get("provider")
    uid = getattr(user, "pk", None) or "anon"
    for span, secs in (("hour", 3600), ("day", 86400)):
        limit = int(provider.get(f"rate_limit_per_{span}", 0) or 0)
        if limit <= 0:
            continue
        key = f"command_intel:llm:{bucket}:{span}:{uid}"
        cache.add(key, 0, secs)
        try:
            used = cache.incr(key)
        except ValueError:  # key expired between add and incr
            cache.set(key, 1, secs)
            used = 1
        if used > limit:
            return f"Rate limit reached ({limit}/{span}). Please wait before trying again."
    return None


def request_report(*, template_key: str | None = None, classification: str | None = None,
                   user=None, force_rebuild: bool = False) -> IntelligenceReport:
    """Create a queued report and enqueue generation; dedupe an in-flight one (doc 06 §7)."""
    templates = config.get("report_templates")
    template_key = template_key or templates.get("default", "posture")
    tmpl = templates["templates"].get(template_key) or {}
    classification = (
        classification
        or tmpl.get("default_classification")
        or config.get("classification")["default"]
    )

    existing = IntelligenceReport.objects.filter(
        template_key=template_key, status__in=_IN_FLIGHT
    ).order_by("-created_at").first()
    if existing:
        return existing

    over = _llm_rate_exceeded(user, "report")
    if over:
        return IntelligenceReport.objects.create(
            template_key=template_key, classification=classification,
            status=IntelligenceReport.Status.FAILED, requested_by=user, error=over,
        )

    report = IntelligenceReport.objects.create(
        template_key=template_key,
        classification=classification,
        status=IntelligenceReport.Status.QUEUED,
        requested_by=user,
    )

    def _enqueue():
        from .tasks import generate_report

        generate_report.delay(report.pk, force_rebuild)

    transaction.on_commit(_enqueue)
    return report


def run_report_now(report: IntelligenceReport, *, force_rebuild: bool = False) -> IntelligenceReport:
    """Synchronous generation (used by tests / management commands, not the web tier)."""
    from .report import run_generation

    return run_generation(report, force_rebuild=force_rebuild)


def request_answer(*, user, question: str):
    """Create a queued conversational turn and enqueue its worker-side answer (P7, ADR-0008).

    Enforces per-user hourly AND daily LLM rate limits (``provider.rate_limit_per_hour`` /
    ``rate_limit_per_day``) so a runaway loop can't drive unbounded LLM spend from the web
    tier. Over a cap, a turn is returned already failed with a clear message rather than
    enqueued (no LLM call is made).
    """
    from django.utils import timezone

    from .models import ConversationTurn

    over = _llm_rate_exceeded(user, "ask")
    if over:
        return ConversationTurn.objects.create(
            user=user, question=question[:2000],
            status=ConversationTurn.Status.FAILED, error=over,
            answered_at=timezone.now(),
        )

    turn = ConversationTurn.objects.create(
        user=user, question=question[:2000], status=ConversationTurn.Status.PENDING,
    )

    def _enqueue():
        from .tasks import answer_question

        answer_question.delay(turn.pk)

    transaction.on_commit(_enqueue)
    return turn


def request_battle_analysis(*, user, battle_report_id: int):
    """Create a queued battle AAR and enqueue it (Combat Intelligence, ADR-0008).

    Dedupes against an in-flight analysis for the same battle so a double-click can't spawn
    two LLM runs.
    """
    from .models import BattleAnalysis

    classification = (
        config.get("battle").get("analysis_classification")
        or config.get("classification")["default"]
    )
    in_flight = [
        BattleAnalysis.Status.PENDING,
        BattleAnalysis.Status.BUILDING_FACTS,
        BattleAnalysis.Status.CALLING_LLM,
    ]
    existing = (
        BattleAnalysis.objects.filter(battle_report_id=battle_report_id, status__in=in_flight)
        .order_by("-created_at")
        .first()
    )
    if existing:
        return existing

    over = _llm_rate_exceeded(user, "battle")
    if over:
        return BattleAnalysis.objects.create(
            battle_report_id=battle_report_id, classification=classification,
            status=BattleAnalysis.Status.FAILED, requested_by=user, error=over,
        )

    analysis = BattleAnalysis.objects.create(
        battle_report_id=battle_report_id, classification=classification,
        status=BattleAnalysis.Status.PENDING, requested_by=user,
    )

    def _enqueue():
        from .tasks import analyze_battle

        analyze_battle.delay(analysis.pk)

    transaction.on_commit(_enqueue)
    return analysis
