"""Report engine — the generation pipeline (design doc 06).

The Celery job body: resolve/build a snapshot → compute + persist constraints →
estimate candidate impacts → (if enabled) call the LLM for a structured briefing,
validate it (schema + entity grounding) with a bounded repair loop, drop anything
ungrounded → persist the body + COAs → terminal status. If the LLM is disabled or
unavailable, a deterministic ``ready_degraded`` report is produced from the computed
constraints and templated COAs — CI is useful even with the AI down.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from . import coa as coa_mod
from . import config
from . import impact as impact_mod
from . import snapshot as snapshot_mod
from .engine import pipeline
from .models import IntelligenceReport, OperationalConstraint, Severity

logger = logging.getLogger("forca.command_intel")


# --- snapshot resolution -----------------------------------------------------
def resolve_snapshot(*, force_rebuild: bool = False, user=None):
    """Reuse the cached latest snapshot when fresh, else build a new one (doc 06 §2)."""
    from core import freshness

    if not force_rebuild:
        latest = snapshot_mod.latest_snapshot()
        if latest and not freshness.is_stale(latest.created_at, "command_intel"):
            return latest
    return snapshot_mod.build_snapshot(trigger="manual", persist=True, user=user)


# --- constraint persistence --------------------------------------------------
def _persist_constraints(report, constraints) -> dict:
    """Upsert OperationalConstraint rows for the report's snapshot; return {key: row}."""
    by_key: dict = {}
    for c in constraints:
        evidence = [
            {"name": i.name, "value": i.value, "unit": i.unit, **(i.evidence_ref or {})}
            for i in (c.inputs or [])
        ]
        row, _ = OperationalConstraint.objects.update_or_create(
            snapshot=report.snapshot, key=c.key,
            defaults={
                "category": c.category, "label": c.label[:160],
                "binding_metric": c.binding_metric, "unit": c.unit,
                "limiting_factor": (c.limiting_factor or "")[:80], "headroom": c.headroom,
                "score": c.score, "severity": c.severity, "status": c.status,
                "affected_capabilities": list(c.affected_capabilities or []),
                "evidence": evidence, "detail": c.detail,
            },
        )
        by_key[c.key] = row
    return by_key


# --- deterministic body (ready_degraded / baseline) --------------------------
def _deterministic_body(snap, constraints, impacts) -> dict:
    crit = [c for c in constraints if c.severity == Severity.CRITICAL and c.status == "computed"]
    high = [c for c in constraints if c.severity == Severity.HIGH and c.status == "computed"]
    unknown = [c for c in constraints if c.status == "unknown"]
    readiness = (snap.slices.get("readiness") or {}).get("overall_index")
    summary = (
        f"{len(crit)} critical and {len(high)} high operational constraint(s) identified. "
        + (f"Overall readiness index {readiness}. " if readiness is not None else "")
        + "Narrative unavailable (AI offline) — deterministic operational picture below."
    )
    return {
        "executive_summary": summary,
        "operational_picture": {
            "posture_statement": "Deterministic constraint picture (no AI narrative).",
            "overall_readiness": readiness,
            "highlights": [f"{c.label}: {c.binding_metric} {c.unit} ({c.severity})" for c in (crit + high)[:5]],
            "not_assessed": [f"{c.label} — {c.detail}" for c in unknown[:5]],
        },
        "operational_constraints": [
            {"constraint_key": c.key, "interpretation": c.detail, "priority_rank": i + 1}
            for i, c in enumerate(crit + high)
        ],
        "courses_of_action": [],  # persisted as rows from templated_drafts
        "strategic_risks": [
            {"risk": f"{c.label} is binding ({c.binding_metric} {c.unit})",
             "severity": c.severity, "linked_constraint": c.key}
            for c in crit
        ],
        "forecast": "Trend forecasting requires snapshot history (accumulates over time).",
        "annexes": [{"title": "Constraint evidence", "ref": "constraints"}],
        "_degraded": True,
    }


# --- LLM call with repair + grounding ---------------------------------------
def _merge_usage(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in (b or {}).items():
        try:
            out[k] = out.get(k, 0) + v
        except TypeError:
            out[k] = v
    return out


def _call_llm(report, snap, constraints, impacts):
    """Return (body, usage, model, latency_ms). Raises LLMError on irreparable failure."""
    from . import prompts
    from .llm import schema
    from .llm.client import LLMClient, LLMError, LLMRequest

    provider_cfg = config.get("provider")
    templates = config.get("report_templates")
    tmpl = templates["templates"].get(report.template_key) or templates["templates"]["posture"]
    contract = snapshot_mod.to_contract(snap)
    cons_dicts = [c.as_dict() for c in constraints]
    index = schema.build_index(contract, cons_dicts)

    client = LLMClient()
    req = prompts.build_request(
        snapshot_contract=contract, constraints=cons_dicts, candidate_impacts=impacts,
        untrusted_text=None, template=tmpl, provider_cfg=provider_cfg,
        prompts_cfg=config.get("prompts"), max_coas=config.get("coa_rules")["max_coas_per_report"],
    )
    repair_attempts = int(provider_cfg.get("repair_attempts", 2))
    usage: dict = {}
    model, latency = "", 0
    obj, violations = None, ["no response"]

    for attempt in range(repair_attempts + 1):
        res = client.generate(req)
        model, latency = res.model, res.latency_ms
        usage = _merge_usage(usage, res.usage)
        obj = res.obj
        violations = ["response was not valid JSON"] if obj is None else schema.validate(obj, contract, cons_dicts)
        if not violations:
            report.repair_attempts_used = attempt
            return obj, usage, model, latency
        if attempt < repair_attempts:
            req = LLMRequest(
                system=req.system, user_blocks=[*req.user_blocks, schema.repair_hint(violations)],
                schema=req.schema, max_output_tokens=req.max_output_tokens,
                temperature=req.temperature, model=req.model,
            )

    # Exhausted: if structurally valid, drop ungrounded items and accept; else fail to degraded.
    if obj is not None and not schema.validate_structure(obj):
        report.grounding_violations_dropped = schema.drop_ungrounded(obj, index)
        report.repair_attempts_used = repair_attempts
        return obj, usage, model, latency
    raise LLMError("irreparable LLM output: " + "; ".join(violations[:3]))


# --- the pipeline ------------------------------------------------------------
def run_generation(report: IntelligenceReport, *, force_rebuild: bool = False) -> IntelligenceReport:
    """Run the full generation pipeline for a queued report. Idempotent-safe."""
    from .llm.client import LLMError

    try:
        report.status = IntelligenceReport.Status.BUILDING_SNAPSHOT
        report.save(update_fields=["status", "updated_at"])
        snap = resolve_snapshot(force_rebuild=force_rebuild, user=report.requested_by)
        report.snapshot = snap
        report.config_version = snap.config_version
        report.save(update_fields=["snapshot", "config_version", "updated_at"])

        report.status = IntelligenceReport.Status.COMPUTING_CONSTRAINTS
        report.save(update_fields=["status", "updated_at"])
        constraints = pipeline.compute_constraints({"sources": snap.slices}, config.get("constraints"))
        constraint_rows = _persist_constraints(report, constraints)
        impacts = impact_mod.candidate_impacts(constraints, {"sources": snap.slices})
        impacts_by_key = {i["constraint_key"]: i for i in impacts if i.get("constraint_key")}

        degraded, error, body = False, "", None
        if settings.COMMAND_INTEL_ENABLED:
            report.status = IntelligenceReport.Status.CALLING_LLM
            report.save(update_fields=["status", "updated_at"])
            try:
                body, usage, model, latency = _call_llm(report, snap, constraints, impacts)
                report.status = IntelligenceReport.Status.VALIDATING
                report.save(update_fields=["status", "updated_at"])
                report.token_usage, report.model_name, report.latency_ms = usage, model, latency
            except LLMError as exc:
                degraded, error = True, f"LLM unavailable: {exc}"[:1000]
                logger.warning("command_intel report %s degraded: %s", report.pk, error)
        else:
            degraded, error = True, "LLM disabled (no LLM_API_KEY)"

        if degraded:
            body = _deterministic_body(snap, constraints, impacts)
            drafts = coa_mod.templated_drafts(constraints, impacts_by_key)
        else:
            drafts = body.get("courses_of_action", []) or []

        coa_mod.persist_coas(report, drafts, constraint_rows, impacts_by_key, user=report.requested_by)

        report.body = body
        report.summary = (body.get("executive_summary", "") or "")[:1000]
        report.title = report.title or f"Command Intelligence Report — {timezone.now():%Y-%m-%d}"
        report.prompt_version = int(config.get("prompts").get("active_version", 1))
        report.error = error
        report.generated_at = timezone.now()
        report.status = (
            IntelligenceReport.Status.READY_DEGRADED if degraded else IntelligenceReport.Status.READY
        )
        report.save()
    except Exception as exc:  # noqa: BLE001 - any pre-LLM failure is a real failure
        logger.exception("command_intel report %s failed", report.pk)
        report.status = IntelligenceReport.Status.FAILED
        report.error = str(exc)[:1000]
        report.save(update_fields=["status", "error", "updated_at"])
    return report
