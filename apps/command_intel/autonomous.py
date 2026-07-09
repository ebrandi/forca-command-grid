"""Autonomous COA proposal — the guard-railed proposer (P7, doc 17 §5).

The system may open COAs unattended, but ONLY as ``proposed`` — a human still accepts each
one; it never auto-accepts, creates a task, or spends ISK ("the AI may propose anything; only
a human may commit the corporation to it"). Ships behind the ``autonomous.enabled`` kill
switch (default OFF): the beat fires but proposes nothing until a director arms it. Every
proposal is:

- **calibration-gated** — only for action families whose measured-outcome history is trusted
  (enough samples, tight error spread), so autonomy is only as bold as its track record;
- **entity-grounded** — deterministic templated drafts computed over the real snapshot (no
  unattended LLM call ⇒ no unattended token spend and no unattended hallucination);
- **audited** (``command_intel.autonomous.propose``) and **reversible** (dismiss like any COA).
"""
from __future__ import annotations

import logging

from . import coa as coa_mod
from . import config, impact, outcomes
from . import snapshot as snapshot_mod
from .engine import pipeline
from .models import IntelligenceReport, Trigger

logger = logging.getLogger("forca.command_intel")


def _trusted_families(constraints) -> set[str]:
    """Action families whose calibration is good enough to auto-propose for (doc 10 §4)."""
    cfg = config.get("autonomous")
    min_n = int(cfg.get("min_calibration_samples", 5))
    max_spread = float(cfg.get("max_calibration_spread", 3.0))
    trusted: set[str] = set()
    for c in constraints:
        fam = outcomes.family_of(c.key)
        if fam in trusted:
            continue
        cal = outcomes.calibration_for(fam)
        if cal["n"] >= min_n and cal["spread"] <= max_spread:
            trusted.add(fam)
    return trusted


def run_autonomous_proposals() -> dict:
    """Propose calibration-trusted COAs unattended.

    Returns ``{status, proposed, ...}``. Inert (proposes nothing) unless
    ``autonomous.enabled`` — the provable kill switch. Proposes nothing until calibration
    history exists, so a young corp is never auto-proposed to.
    """
    cfg = config.get("autonomous")
    if not cfg.get("enabled", False):
        return {"status": "disabled", "proposed": 0}

    snap = snapshot_mod.build_snapshot(trigger="autonomous", persist=True)
    constraints = pipeline.compute_constraints({"sources": snap.slices}, config.get("constraints"))
    trusted = _trusted_families(constraints)
    if not trusted:
        return {"status": "no_trusted_families", "proposed": 0}

    # A deterministic report row homes the proposals and gives them provenance/audit.
    report = IntelligenceReport.objects.create(
        snapshot=snap, trigger=Trigger.AUTONOMOUS,
        status=IntelligenceReport.Status.READY_DEGRADED,
        classification=config.get("classification")["default"],
        title=f"Autonomous proposals — {snap.created_at:%Y-%m-%d}",
        summary="Autonomously proposed courses of action (calibration-trusted families only).",
        config_version=snap.config_version,
    )

    from .report import _persist_constraints

    constraint_rows = _persist_constraints(report, constraints)
    impacts = impact.candidate_impacts(constraints, {"sources": snap.slices})
    impacts_by_key = {i["constraint_key"]: i for i in impacts if i.get("constraint_key")}

    drafts = [
        d for d in coa_mod.templated_drafts(constraints, impacts_by_key)
        if outcomes.family_of(d.get("constraint_key", "")) in trusted
    ]
    drafts.sort(key=lambda d: d.get("priority", 0), reverse=True)
    drafts = drafts[: int(cfg.get("max_proposals_per_run", 5))]

    coas = coa_mod.persist_coas(report, drafts, constraint_rows, impacts_by_key, user=None)
    _audit_proposals(coas)
    return {"status": "ok", "proposed": len(coas), "report": report.pk, "families": sorted(trusted)}


def _audit_proposals(coas) -> None:
    from core.audit import audit_log

    for c in coas:
        audit_log(
            None, "command_intel.autonomous.propose",
            target_type="command_intel_coa", target_id=str(c.pk),
            metadata={"slug": c.slug, "constraint": c.constraint_id, "priority": c.priority},
        )
