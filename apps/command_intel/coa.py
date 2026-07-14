"""Course-of-Action lifecycle (design doc 07).

Persisting COAs from LLM drafts (or deterministic templates), deduped by a stable
slug; accepting one (capture baseline, convert to tasks.Task via the shared
create_task() factory); dismissing one. Impact/confidence/owner are CODE-filled, not
model-filled — the model frames the action; the platform quantifies and assigns it.
"""
from __future__ import annotations

from django.utils import timezone
from django.utils.text import slugify

from . import config, messages, outcomes
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

# limiting_factor → (objective scaffold key, task type) for the deterministic ready_degraded path.
# Seam B (``.messages``): the objective/reasoning/risk sentences below are written by the Celery
# worker and read by every officer under their own locale, so each is persisted as a scaffold key +
# JSON-safe params next to its English column. An LLM-drafted COA is model free text: it gets no key
# and renders verbatim, exactly like a pingboard ``custom_message``.
_ACTION_TEMPLATE = {
    "hulls_in_stock": ("coa.objective.stage_hulls", "buy"),
    "pilots_qualified": ("coa.objective.train_pilots", "train"),
    "logi_cap": ("coa.objective.recruit_logi", "train"),
    "fuel_days_left": ("coa.objective.refuel", "deliver"),
    "srp_budget": ("coa.objective.top_up_srp", "other"),
    "balance_isk": ("coa.objective.raise_income", "other"),
}
_OBJECTIVE_FALLBACK = "coa.objective.relieve"
_RISK = "coa.risk_if_ignored"

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
        obj_key, task_type = _ACTION_TEMPLATE.get(
            c.limiting_factor or "", (_OBJECTIVE_FALLBACK, "other")
        )
        n = abs(int(c.headroom)) if c.headroom is not None and c.headroom < 0 else 1
        # A capability id ("doctrine:vanguard") is an identifier and stays raw; the constraint's own
        # label is itself a scaffolded sentence, so it is embedded as a composable ref. NB the
        # generic "Relieve: …" fallback has always quoted the LABEL (never the capability) — keep it.
        label_ref = messages.ref(c.label_key, c.label_params) if c.label_key else c.label
        label = (
            label_ref if obj_key == _OBJECTIVE_FALLBACK
            else (c.affected_capabilities[0] if c.affected_capabilities else label_ref)
        )
        obj_params = {"n": n, "label": label}
        risk_params = {"label": label_ref, "metric": c.binding_metric, "unit": c.unit}
        drafts.append({
            "constraint_key": c.key,
            # The English column: derived from the msgid itself, so the two cannot drift. The slug
            # is built from THIS English string and must never come from a translated one.
            "objective": messages.english(obj_key, obj_params),
            "objective_key": obj_key,
            "objective_params": obj_params,
            "reasoning": c.detail,
            "reasoning_key": c.detail_key,
            "reasoning_params": c.detail_params or {},
            "risk_if_ignored": messages.english(_RISK, risk_params),
            "risk_if_ignored_key": _RISK,
            "risk_if_ignored_params": risk_params,
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
                # Seam B: empty for an LLM draft (free text → verbatim), set for a templated one.
                "objective_key": d.get("objective_key", ""),
                "objective_params": d.get("objective_params") or {},
                "reasoning_key": d.get("reasoning_key", ""),
                "reasoning_params": d.get("reasoning_params") or {},
                "risk_if_ignored_key": d.get("risk_if_ignored_key", ""),
                "risk_if_ignored_params": d.get("risk_if_ignored_params") or {},
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
            # Refresh a still-proposed COA with the newest reasoning/impact/priority — the scaffold
            # key + params move in lockstep with the prose they describe, or the row would render a
            # stale sentence to a non-English reader while English readers saw the fresh one.
            coa.report = report
            coa.constraint = constraint
            coa.reasoning = d.get("reasoning", "")
            coa.reasoning_key = d.get("reasoning_key", "")
            coa.reasoning_params = d.get("reasoning_params") or {}
            coa.expected_impact = impact.get("expected_delta", {}) or {}
            coa.readiness_delta = delta
            coa.priority = priority
            coa.confidence = confidence
            coa.confidence_label = _confidence_label(confidence, cfg)
            coa.risk_if_ignored = d.get("risk_if_ignored", "")
            coa.risk_if_ignored_key = d.get("risk_if_ignored_key", "")
            coa.risk_if_ignored_params = d.get("risk_if_ignored_params") or {}
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
