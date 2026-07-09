"""Operational Campaign planner (design doc 08).

Composes accepted COAs into a sequenced plan toward a target metric, computes the
expected readiness trajectory (accumulated COA impact deltas, damped for shared
metrics), a transparent success probability, and tracks milestone completion. The
measured trajectory is sampled from snapshot history and overlaid on the plan.
"""
from __future__ import annotations

from django.utils import timezone

from . import config
from .engine import pipeline
from .models import Campaign, CampaignMilestone, CourseOfAction, IntelligenceSnapshot


# --- metric helpers ----------------------------------------------------------
def baseline_value(target_metric: str, snapshot) -> float | None:
    """Read the target metric from a snapshot (readiness index or a constraint binding)."""
    if snapshot is None:
        return None
    if target_metric == "readiness.overall":
        v = (snapshot.slices.get("readiness") or {}).get("overall_index")
        return float(v) if v is not None else None
    cons = pipeline.compute_constraints({"sources": snapshot.slices}, config.get("constraints"))
    for c in cons:
        if c.key == target_metric and c.binding_metric is not None:
            return float(c.binding_metric)
    return None


def _coa_metric_delta(coa: CourseOfAction | None, target_metric: str) -> float:
    """The COA's projected contribution to the target metric."""
    if coa is None:
        return 0.0
    if target_metric == "readiness.overall":
        return float(coa.readiness_delta or 0)
    deltas = (coa.expected_impact or {}).get("constraints") or {}
    try:
        return float(deltas.get(target_metric, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


# --- composition -------------------------------------------------------------
def order_coas(coas) -> list[CourseOfAction]:
    """Dependency-aware ordering: fewer unmet deps (within the set) first, then priority."""
    coas = list(coas)
    ids = {c.pk for c in coas}

    def _key(c):
        deps_in_set = c.dependencies.filter(pk__in=ids).count()
        return (deps_in_set, -c.priority)

    return sorted(coas, key=_key)


def compose_campaign(*, objective: str, target_metric: str = "readiness.overall",
                     target_value=None, coa_ids: list[int], user, report=None) -> Campaign:
    """Create a DRAFT campaign from selected COAs; capture the baseline; plan the curve."""
    from .snapshot import latest_snapshot

    snap = latest_snapshot()
    campaign = Campaign.objects.create(
        objective=objective,
        target_metric=target_metric,
        baseline_value=baseline_value(target_metric, snap),
        target_value=target_value,
        status=Campaign.Status.DRAFT,
        owner=user,
        created_from_report=report,
    )
    # Never compose an above-clearance COA into a viewable campaign milestone
    # (milestone.title is copied from coa.objective). Choke point behind the view.
    from . import access
    coas = order_coas(access.visible_coas(user, CourseOfAction.objects.filter(pk__in=coa_ids)))
    for i, coa in enumerate(coas, start=1):
        CampaignMilestone.objects.create(
            campaign=campaign, order=i, title=coa.objective[:200], coa=coa,
            owner_tag=coa.owner_tag, responsible_user=coa.responsible_user,
            status=CampaignMilestone.Status.PENDING,
        )
        coa.campaign = campaign
        coa.save(update_fields=["campaign", "updated_at"])
    compute_trajectory(campaign)
    compute_success_probability(campaign)
    return campaign


# --- trajectory --------------------------------------------------------------
def compute_trajectory(campaign: Campaign) -> list[dict]:
    """The planned curve: accumulate each milestone's COA delta, damped for repeats (doc 08 §3)."""
    policy = config.get("campaign_policy")
    damping = float(policy.get("interaction_damping", 0.7))
    metric = campaign.target_metric
    value = float(campaign.baseline_value or 0)
    points = [{"milestone": None, "value": round(value, 1)}]
    hits_on_metric = 0
    for ms in campaign.milestones.order_by("order"):
        delta = _coa_metric_delta(ms.coa, metric)
        if delta:
            value += delta * (damping ** hits_on_metric)
            hits_on_metric += 1
        ms.expected_value = round(value, 2)
        ms.save(update_fields=["expected_value", "updated_at"])
        points.append({"milestone": ms.pk, "value": round(value, 1)})
    campaign.expected_trajectory = points
    campaign.save(update_fields=["expected_trajectory", "updated_at"])
    return points


def measured_trajectory(campaign: Campaign) -> list[dict]:
    """The actual metric sampled from snapshots since launch, overlaid on the plan (doc 08 §3)."""
    if not campaign.start_at:
        return []
    snaps = IntelligenceSnapshot.objects.filter(
        created_at__gte=campaign.start_at
    ).order_by("created_at")[:40]
    out: list[dict] = []
    for s in snaps:
        v = baseline_value(campaign.target_metric, s)
        if v is not None:
            out.append({"at": s.created_at.isoformat(), "value": round(float(v), 1)})
    return out


# --- success probability (transparent heuristic, doc 08 §5) ------------------
def _completion_rate(milestone: CampaignMilestone, default: float, min_samples: int) -> float:
    user = milestone.responsible_user or (milestone.coa.responsible_user if milestone.coa_id else None)
    if user is None:
        return default
    from apps.tasks.models import Task

    resolved = Task.objects.filter(
        assignee=user, status__in=[Task.Status.DONE, Task.Status.CANCELLED]
    )
    total = resolved.count()
    if total < min_samples:
        return default
    return resolved.filter(status=Task.Status.DONE).count() / total


def _headroom_factor(campaign: Campaign) -> float:
    if not campaign.expected_trajectory or campaign.target_value is None:
        return 1.0
    end = campaign.expected_trajectory[-1]["value"]
    base = float(campaign.baseline_value or 0)
    tgt = float(campaign.target_value)
    if tgt == base:
        return 1.0
    progress = (end - base) / (tgt - base)
    if progress >= 1.2:
        return 1.0
    if progress >= 1.0:
        return 0.9
    return max(0.4, 0.9 * progress)


def compute_success_probability(campaign: Campaign) -> float:
    """Π milestone(completion × confidence) × headroom — factors visible, not a black box."""
    policy = config.get("campaign_policy")
    sp = policy.get("success_probability", {})
    default_rate = float(sp.get("default_completion_rate", 0.6))
    min_samples = int(sp.get("min_completion_samples", 5))
    p = 1.0
    for ms in campaign.milestones.all():
        completion = _completion_rate(ms, default_rate, min_samples)
        conf = float(ms.coa.confidence) if ms.coa_id else 0.6
        p *= max(0.05, completion * (0.5 + 0.5 * conf))
    p = max(0.0, min(1.0, p * _headroom_factor(campaign)))
    campaign.success_probability = round(p, 2)
    campaign.save(update_fields=["success_probability", "updated_at"])
    return campaign.success_probability


# --- lifecycle ---------------------------------------------------------------
def launch_campaign(campaign: Campaign, user) -> Campaign:
    """DRAFT → ACTIVE: anchor the baseline to the live snapshot at launch (doc 08 §7)."""
    from .snapshot import latest_snapshot

    policy = config.get("campaign_policy")
    snap = latest_snapshot()
    campaign.baseline_value = baseline_value(campaign.target_metric, snap)
    campaign.start_at = timezone.now()
    campaign.due_at = campaign.due_at or (
        campaign.start_at + timezone.timedelta(days=int(policy.get("default_window_days", 21)))
    )
    campaign.status = Campaign.Status.ACTIVE
    campaign.save()
    compute_trajectory(campaign)
    compute_success_probability(campaign)
    return campaign


def abandon_campaign(campaign: Campaign, user, note: str = "") -> Campaign:
    campaign.status = Campaign.Status.ABANDONED
    campaign.save(update_fields=["status", "updated_at"])
    return campaign


def on_coa_completed(coa: CourseOfAction) -> None:
    """Called from the task-done signal: mark the COA's milestone done; roll up the campaign."""
    if not coa.campaign_id:
        return
    coa.milestones.filter(status__in=[
        CampaignMilestone.Status.PENDING, CampaignMilestone.Status.IN_PROGRESS,
    ]).update(status=CampaignMilestone.Status.DONE)
    campaign = coa.campaign
    if campaign.status != Campaign.Status.ACTIVE:
        return
    policy = config.get("campaign_policy")
    all_done = not campaign.milestones.exclude(status=CampaignMilestone.Status.DONE).exists()
    target_reached = _target_reached(campaign)
    complete_on = policy.get("complete_on", "target_or_all_milestones")
    if all_done or (complete_on == "target_or_all_milestones" and target_reached):
        campaign.status = Campaign.Status.COMPLETED
        campaign.save(update_fields=["status", "updated_at"])


def _target_reached(campaign: Campaign) -> bool:
    if campaign.target_value is None:
        return False
    from .snapshot import latest_snapshot

    current = baseline_value(campaign.target_metric, latest_snapshot())
    return current is not None and current >= float(campaign.target_value)
