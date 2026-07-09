"""Battle after-action review generation (Combat Intelligence) — worker-side (ADR-0008).

Two layers, as everywhere in this subsystem: ``battle.battle_facts`` computes the deterministic
fact set from the killboard, and the LLM only *narrates* it into an After-Action Review
(what happened / what went wrong / what to improve), grounded because the fenced facts are the
model's only input. Degrades to a facts-only body when the LLM is disabled or unreachable, so
an officer always gets the numbers even with the AI down.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from . import battle, config
from .models import BattleAnalysis

logger = logging.getLogger("forca.command_intel")


def _isk(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "0"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"{v / div:.1f}{unit}"
    return f"{v:.0f}"


def _normalize_body(obj: dict) -> dict:
    def _lst(key):
        return [str(x)[:600] for x in (obj.get(key) or []) if x][:12]

    return {
        "summary": str(obj.get("summary") or "")[:2000],
        "what_happened": str(obj.get("what_happened") or "")[:4000],
        "what_went_wrong": _lst("what_went_wrong"),
        "what_to_improve": _lst("what_to_improve"),
        "key_losses": _lst("key_losses"),
    }


def _deterministic_body(facts: dict) -> dict:
    t = facts.get("totals", {})
    systems = ", ".join(facts.get("systems") or []) or "the field"
    wrong = []
    if t.get("doctrine_losses"):
        wrong.append(f"{t.get('off_doctrine_losses', 0)} of {t['doctrine_losses']} doctrine losses were off-doctrine.")
    if t.get("logi_lost"):
        wrong.append(f"{t['logi_lost']} logistics ship(s) lost.")
    return {
        "summary": (
            f"{facts.get('outcome', 'even').title()} engagement in {systems}: lost "
            f"{t.get('our_losses', 0)} ships ({_isk(t.get('isk_lost'))} ISK), killed "
            f"{t.get('our_kills', 0)} ({_isk(t.get('isk_destroyed'))} ISK); ISK swing "
            f"{_isk(t.get('isk_swing'))}. Narrative unavailable (AI offline) — facts below."
        ),
        "what_happened": "Deterministic facts only (AI narrative unavailable). See the panels below.",
        "what_went_wrong": wrong,
        "what_to_improve": [],
        "key_losses": [f"{loss['ship']} ({loss['pilot']})" for loss in facts.get("our_losses_detail", [])[:5]],
        "_degraded": True,
    }


def _call_llm(facts: dict):
    """Return (body, usage, model, latency_ms). Raises LLMError on unusable output."""
    from . import prompts
    from .llm.client import LLMClient, LLMError

    client = LLMClient()
    req = prompts.build_battle_request(
        facts=facts, provider_cfg=config.get("provider"), prompts_cfg=config.get("prompts"),
    )
    res = client.generate(req)
    obj = res.obj if isinstance(res.obj, dict) else None
    if not obj or not (isinstance(obj.get("summary"), str) and obj["summary"].strip()):
        raise LLMError("battle AAR output missing a usable summary")
    return _normalize_body(obj), res.usage, res.model, res.latency_ms


def run_battle_analysis(analysis: BattleAnalysis) -> BattleAnalysis:
    """Build the facts, then (if enabled) narrate the AAR. Idempotent-safe; never raises out."""
    from apps.killboard.models import BattleReport

    from .llm.client import LLMError

    report = BattleReport.objects.filter(pk=analysis.battle_report_id).first()
    if report is None:
        analysis.status = BattleAnalysis.Status.FAILED
        analysis.error = "battle report not found"
        analysis.generated_at = timezone.now()
        analysis.save(update_fields=["status", "error", "generated_at", "updated_at"])
        return analysis

    try:
        analysis.status = BattleAnalysis.Status.BUILDING_FACTS
        analysis.save(update_fields=["status", "updated_at"])
        facts = battle.battle_facts(report)
        analysis.facts = facts
        analysis.title = analysis.title or f"After-action — {facts.get('title') or 'Battle'}"
        analysis.save(update_fields=["facts", "title", "updated_at"])

        degraded, error, body = False, "", None
        if settings.COMMAND_INTEL_ENABLED:
            analysis.status = BattleAnalysis.Status.CALLING_LLM
            analysis.save(update_fields=["status", "updated_at"])
            try:
                body, usage, model, latency = _call_llm(facts)
                analysis.token_usage, analysis.model_name, analysis.latency_ms = usage, model, latency
            except LLMError as exc:
                degraded, error = True, f"LLM unavailable: {exc}"[:1000]
                logger.warning("command_intel battle analysis %s degraded: %s", analysis.pk, error)
        else:
            degraded, error = True, "LLM disabled (no LLM_API_KEY)"

        if degraded:
            body = _deterministic_body(facts)
        analysis.body = body
        analysis.error = error
        analysis.generated_at = timezone.now()
        analysis.status = (
            BattleAnalysis.Status.READY_DEGRADED if degraded else BattleAnalysis.Status.READY
        )
        analysis.save()
    except Exception as exc:  # noqa: BLE001 - any failure is a real failure, not a crash
        logger.exception("command_intel battle analysis %s failed", analysis.pk)
        analysis.status = BattleAnalysis.Status.FAILED
        analysis.error = str(exc)[:1000]
        analysis.generated_at = timezone.now()
        analysis.save(update_fields=["status", "error", "generated_at", "updated_at"])
    return analysis
