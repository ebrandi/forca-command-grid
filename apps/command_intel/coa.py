"""Course-of-Action lifecycle (design doc 07).

Persisting COAs from LLM drafts (or deterministic templates), deduped by a stable
slug; accepting one (capture baseline, convert to tasks.Task via the shared
create_task() factory); dismissing one. Impact/confidence/owner are CODE-filled, not
model-filled — the model frames the action; the platform quantifies and assigns it.
"""
from __future__ import annotations

from django.utils import timezone
from django.utils.text import slugify

from . import config, outcomes
from .models import CourseOfAction, Severity

# Constraint category → officer-responsibility tag (reused/extended from readiness).
_OWNER_TAG_BY_CATEGORY = {
    "combat": "combat_officer",
    "logistics": "logistics_officer",
    "financial": "finance_director",
    "industry": "industry_director",
    "infrastructure": "infrastructure_officer",
    "manpower": "recruitment_officer",
    "recruitment": "recruitment_officer",
    "strategic": "strategic_director",
}

# limiting_factor → (action template, task type) for the deterministic ready_degraded path.
_ACTION_TEMPLATE = {
    "hulls_in_stock": ("Stage {n} more {label}", "buy"),
    "pilots_qualified": ("Train {n} more pilots for {label}", "train"),
    "logi_cap": ("Recruit/train logistics pilots for {label}", "train"),
    "fuel_days_left": ("Refuel low-fuel structures", "deliver"),
    "srp_budget": ("Top up the SRP budget", "other"),
    "balance_isk": ("Raise corp income or cut burn", "other"),
}

_SEVERITY_PRIORITY = {
    Severity.CRITICAL: 90, Severity.HIGH: 70, Severity.WATCH: 45, Severity.INFO: 20,
}


def _confidence_label(conf: float, cfg: dict) -> str:
    labels = cfg.get("confidence_labels", {"high": 0.66, "medium": 0.4})
    if conf >= labels.get("high", 0.66):
        return CourseOfAction.ConfidenceLabel.HIGH
    if conf >= labels.get("medium", 0.4):
        return CourseOfAction.ConfidenceLabel.MEDIUM
    return CourseOfAction.ConfidenceLabel.LOW


def resolve_owner_user(owner_tag: str):
    """First user mapped to an owner tag in command_intel.responsibilities, or None."""
    if not owner_tag:
        return None
    mapping = (config.get("responsibilities").get("owner_tags") or {}).get(owner_tag)
    users = mapping.get("users") if isinstance(mapping, dict) else mapping
    if not users:
        return None
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=users[0]).first()


def _slug_for(constraint_key: str, objective: str) -> str:
    return f"{constraint_key or 'general'}/{slugify(objective)[:48]}"


def templated_drafts(constraints: list, impacts_by_key: dict) -> list[dict]:
    """Deterministic COA drafts from open constraints (the ready_degraded path, doc 07 §2)."""
    drafts: list[dict] = []
    for c in constraints:
        if c.status != "computed" or c.severity == Severity.INFO:
            continue
        tmpl, task_type = _ACTION_TEMPLATE.get(c.limiting_factor or "", (f"Relieve: {c.label}", "other"))
        n = abs(int(c.headroom)) if c.headroom is not None and c.headroom < 0 else 1
        label = (c.affected_capabilities[0] if c.affected_capabilities else c.label)
        objective = tmpl.format(n=n, label=label)
        drafts.append({
            "constraint_key": c.key,
            "objective": objective,
            "reasoning": c.detail,
            "risk_if_ignored": f"{c.label} stays binding at {c.binding_metric} {c.unit}.",
            "severity_if_ignored": c.severity,
            "effort": CourseOfAction.Effort.MEDIUM,
            "priority": _SEVERITY_PRIORITY.get(c.severity, 30),
            "task_type": task_type,
            "_templated": True,
        })
    return drafts


def persist_coas(report, drafts: list[dict], constraints_by_key: dict, impacts_by_key: dict,
                 *, user=None) -> list[CourseOfAction]:
    """Upsert COAs from drafts; code-fill impact/confidence/owner; dedupe by slug (doc 07 §4)."""
    cfg = config.get("coa_rules")
    min_priority = cfg.get("min_priority_to_surface", 30)
    out: list[CourseOfAction] = []
    for d in drafts:
        ck = d.get("constraint_key") or ""
        constraint = constraints_by_key.get(ck)
        priority = int(d.get("priority", 0) or 0)
        if priority < min_priority:
            continue
        impact = impacts_by_key.get(ck, {})
        confidence = float(impact.get("confidence", 0.5))
        # Apply the learned calibration for this action family (doc 10 §4): scale
        # confidence by the family's tightness factor and bias-correct the predicted
        # delta. With no/thin history this is a no-op (factor 1.0, bias 0.0).
        cal = outcomes.calibration_for(outcomes.family_of(ck))
        confidence = round(min(1.0, confidence * cal["factor"]), 2)
        raw_delta = _impact_delta(impact)
        delta = round(raw_delta - cal["bias"], 2) if (raw_delta is not None and cal["n"]) else raw_delta
        category = getattr(constraint, "category", "") if constraint else ""
        owner_tag = _OWNER_TAG_BY_CATEGORY.get(category, "")
        slug = _slug_for(ck, d.get("objective", ""))

        coa, created = CourseOfAction.objects.get_or_create(
            slug=slug,
            defaults={
                "report": report,
                "constraint": constraint,
                "objective": d.get("objective", "")[:2000],
                "reasoning": d.get("reasoning", ""),
                "expected_impact": impact.get("expected_delta", {}) or {},
                "readiness_delta": delta,
                "effort": d.get("effort") if d.get("effort") in CourseOfAction.Effort.values else "medium",
                "priority": priority,
                "confidence": confidence,
                "confidence_label": _confidence_label(confidence, cfg),
                "owner_tag": owner_tag,
                "responsible_user": resolve_owner_user(owner_tag) if cfg.get("auto_resolve_owner", True) else None,
                "risk_if_ignored": d.get("risk_if_ignored", ""),
                "severity_if_ignored": d.get("severity_if_ignored")
                    if d.get("severity_if_ignored") in Severity.values else Severity.WATCH,
                "provenance": {"impact": impact, "templated": d.get("_templated", False)},
                "state": CourseOfAction.State.PROPOSED,
            },
        )
        if not created and coa.state == CourseOfAction.State.PROPOSED:
            # Refresh a still-proposed COA with the newest reasoning/impact/priority.
            coa.report = report
            coa.constraint = constraint
            coa.reasoning = d.get("reasoning", "")
            coa.expected_impact = impact.get("expected_delta", {}) or {}
            coa.readiness_delta = delta
            coa.priority = priority
            coa.confidence = confidence
            coa.confidence_label = _confidence_label(confidence, cfg)
            coa.risk_if_ignored = d.get("risk_if_ignored", "")
            coa.save()
        out.append(coa)
    return out


def _impact_delta(impact: dict):
    ed = (impact or {}).get("expected_delta") or {}
    if isinstance(ed, dict):
        for v in ed.values():
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                continue
    return None


def accept_coa(coa: CourseOfAction, user, *, baseline_snapshot=None):
    """Accept a COA: capture baseline, convert to task(s), move to in_progress (doc 07 §5/§6)."""
    from apps.tasks.services import create_task

    from .models import IntelligenceReport  # noqa: F401  (kept for symmetry/typing)

    coa.state = CourseOfAction.State.ACCEPTED
    coa.decided_by = user
    coa.decided_at = timezone.now()
    coa.baseline_snapshot = baseline_snapshot or (coa.report.snapshot if coa.report else None)
    coa.save()

    task_type = (coa.provenance or {}).get("task_type", "other")
    create_task(
        task_type=task_type,
        title=coa.objective,
        description=coa.reasoning,
        priority=coa.priority,
        assignee=coa.responsible_user,
        related_type=CourseOfAction.RELATED_TYPE,
        related_id=coa.pk,
        created_by=user,
    )
    coa.state = CourseOfAction.State.IN_PROGRESS
    coa.save(update_fields=["state", "updated_at"])
    return coa


def dismiss_coa(coa: CourseOfAction, user, note: str = ""):
    """Dismiss a COA with a recorded reason (kept for decision history, doc 10 §2)."""
    coa.state = CourseOfAction.State.DISMISSED
    coa.decided_by = user
    coa.decided_at = timezone.now()
    coa.decision_note = note or ""
    coa.save()
    return coa
