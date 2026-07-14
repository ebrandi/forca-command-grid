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

from . import battle, config, messages
from .models import BattleAnalysis

logger = logging.getLogger("forca.command_intel")

# Seam B (``.messages``): this job runs in a Celery worker (no user, no locale) and the AAR it
# writes is read by every officer under their own locale. The deterministic (degraded) prose is
# therefore persisted as a scaffold key + JSON-safe params next to its English column. The LLM
# narrative is model free text: no key, rendered verbatim. ``facts`` is NOT a prose sink — it is
# numbers, ISO timestamps and EVE names (ship/pilot/system), which stay English by policy.
_TITLE = "battle.title"
_OUTCOME_KEYS = {
    "favorable": "battle.outcome.favorable",
    "unfavorable": "battle.outcome.unfavorable",
    "even": "battle.outcome.even",
}


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


def _deterministic_template(facts: dict) -> dict:
    """The degraded AAR body as a *document template* — same shape, prose leaves are scaffold refs.

    Persisted in ``BattleAnalysis.body_params``; the English ``body`` column is derived from it with
    ``messages.english_doc`` so the two cannot drift, and ``body_i18n`` re-renders it per reader. A
    ``gettext_lazy`` proxy could not live here at all: inside a JSONField it is a TypeError on save.
    """
    t = facts.get("totals", {})
    # EVE solar-system names stay English by policy; only the empty-case wording is prose.
    systems = ", ".join(facts.get("systems") or []) or messages.ref("battle.systems.default")
    outcome = facts.get("outcome", "even")
    outcome_key = _OUTCOME_KEYS.get(outcome)
    # An unrecognised outcome degrades to the raw, title-cased code — never blank.
    outcome_val = messages.ref(outcome_key) if outcome_key else str(outcome).title()

    wrong = []
    if t.get("doctrine_losses"):
        wrong.append(messages.ref("battle.wrong.off_doctrine", {
            "off": t.get("off_doctrine_losses", 0), "total": t["doctrine_losses"],
        }))
    if t.get("logi_lost"):
        wrong.append(messages.ref("battle.wrong.logi_lost", {"count": t["logi_lost"]}))
    return {
        "summary": messages.ref("battle.summary.degraded", {
            "outcome": outcome_val, "systems": systems,
            "our_losses": t.get("our_losses", 0), "isk_lost": _isk(t.get("isk_lost")),
            "our_kills": t.get("our_kills", 0), "isk_destroyed": _isk(t.get("isk_destroyed")),
            "isk_swing": _isk(t.get("isk_swing")),
        }),
        "what_happened": messages.ref("battle.what_happened.degraded"),
        "what_went_wrong": wrong,
        "what_to_improve": [],
        # Pure EVE game data (hull + pilot name) — no prose, nothing to translate.
        "key_losses": [
            f"{loss['ship']} ({loss['pilot']})"
            for loss in facts.get("our_losses_detail", [])[:5]
        ],
        "_degraded": True,
    }


def _deterministic_body(facts: dict) -> dict:
    """The English degraded body — byte-identical to what this job has always written."""
    return messages.english_doc(_deterministic_template(facts))


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
        if not analysis.title:
            # The battle's own title is killboard/EVE data and stays raw; only the "Battle"
            # fallback and the "After-action — …" frame are prose.
            battle_name = facts.get("title") or messages.ref("battle.title.default")
            title_params = {"battle": battle_name}
            analysis.title = messages.english(_TITLE, title_params)
            analysis.title_key, analysis.title_params = _TITLE, title_params
        analysis.save(update_fields=[
            "facts", "title", "title_key", "title_params", "updated_at",
        ])

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
            # Seam B: only the deterministic body is scaffolded. The LLM narrative above is model
            # free text with no msgid — it carries no key and renders verbatim to every reader.
            template = _deterministic_template(facts)
            body = messages.english_doc(template)
            analysis.body_key = "battle.body.degraded"
            analysis.body_params = template
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
