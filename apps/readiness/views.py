"""Readiness dashboard (officer/director) + recompute, findings register & task queue."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from .services import compute_readiness, index_trend


@login_required
@role_required(rbac.ROLE_OFFICER)
def dashboard(request: HttpRequest) -> HttpResponse:
    result = compute_readiness()
    dim_labels = {
        "doctrine": "Doctrine",
        "skill": "Skill",
        "stock": "Stock",
        "logistics": "Logistics",
    }
    dimensions = [
        {"key": k, "label": dim_labels.get(k, k), "score": v}
        for k, v in result["dimensions"].items()
    ]
    # Registered dimensions not in the index (disabled) — offer a drill-down preview
    # so leadership can evaluate them before enabling.
    from .engine import registry

    scored_keys = set(result["dimensions"])
    preview = [
        {"key": p.key, "label": getattr(p, "label", p.key)}
        for p in registry.providers()
        if p.key not in scored_keys
    ]

    # Per-dimension week trend: a mini sparkline + the delta vs the snapshot from ~7
    # days ago, so each card shows movement, not just a point value (doc 10 §1). Reads
    # the snapshot history (cheap, dashboard-only — not on the compute/warm hot path).
    import datetime as _dt

    from django.utils import timezone as _tz

    from .models import ReadinessFinding, ReadinessSnapshot

    snaps = list(reversed(ReadinessSnapshot.objects.order_by("-created_at")[:60]))
    week_ago = _tz.now() - _dt.timedelta(days=7)
    baseline = None
    for s in snaps:
        if s.created_at <= week_ago:
            baseline = s
    for d in dimensions:
        series = [s.dimensions.get(d["key"]) for s in snaps if s.dimensions.get(d["key"]) is not None]
        d["spark"] = series[-12:]
        base_val = baseline.dimensions.get(d["key"]) if baseline else None
        d["delta"] = (d["score"] - base_val) if (d["score"] is not None and base_val is not None) else None

    forecast_count = ReadinessFinding.objects.filter(
        kind=ReadinessFinding.Kind.FORECAST, status=ReadinessFinding.Status.OPEN
    ).count()

    return render(
        request,
        "readiness/dashboard.html",
        {
            "index": result["index"],
            "dimensions": dimensions,
            "preview_dimensions": preview,
            "gaps": result["gaps"],
            "coverage": result["coverage"],
            "trend": index_trend(),
            "forecast_count": forecast_count,
            "is_director": rbac.has_role(request.user, rbac.ROLE_DIRECTOR),
        },
    )


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def recompute(request: HttpRequest) -> HttpResponse:
    # Debounce: the scan is heavy and runs in-request, so a short cache lock stops
    # rapid clicks (or concurrent requests) from each kicking off a full recompute.
    from django.core.cache import cache

    if not cache.add("readiness:recompute:lock", "1", timeout=60):
        messages.info(request, "Readiness is already recomputing — give it a moment.")
        return redirect("readiness:dashboard")
    try:
        result = compute_readiness(persist=True)
    except Exception:
        cache.delete("readiness:recompute:lock")  # release so a failed run can be retried
        raise
    messages.success(request, f"Readiness recomputed: index {result['index']}.")
    return redirect("readiness:dashboard")


def _main_character(user):
    chars = list(user.characters.all())
    return next((c for c in chars if c.is_main), chars[0] if chars else None)


@login_required
@role_required(rbac.ROLE_MEMBER)
def pilot_dashboard(request: HttpRequest) -> HttpResponse:
    """Absorbed into the Command Center (/dashboard/) — redirect old bookmarks.

    The facets + quest log render there now (apps/identity/views.py), keeping
    this view's seed-once/read-only cache contract. The officer coaching view
    (``pilot_view`` below) still renders readiness/me.html for OTHER pilots.
    Stays namespace-gated by the 'readiness' feature via the middleware map.
    """
    return redirect("identity:dashboard")


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def reco_action(request: HttpRequest, pk: int) -> HttpResponse:
    """A pilot marks a quest-log item done, dismisses it, or snoozes it 7 days."""
    import datetime as dt

    from django.utils import timezone

    from .models import PilotRecommendation

    reco = get_object_or_404(PilotRecommendation, pk=pk, user=request.user)
    action = request.POST.get("action")
    if action == "done":
        reco.state = PilotRecommendation.State.DONE
        reco.save(update_fields=["state", "updated_at"])
        messages.success(request, "Nice — marked done. That lifts your readiness.")
    elif action == "dismiss":
        reco.state = PilotRecommendation.State.DISMISSED
        reco.save(update_fields=["state", "updated_at"])
        messages.success(request, "Hidden — it won't come back unless you ask.")
    elif action == "snooze":
        reco.snoozed_until = timezone.now() + dt.timedelta(days=7)
        reco.save(update_fields=["snoozed_until", "updated_at"])
        messages.success(request, "Snoozed for a week.")
    return redirect("identity:dashboard")


@login_required
@role_required(rbac.ROLE_OFFICER)
def pilot_view(request: HttpRequest, character_id: int) -> HttpResponse:
    """Officer's read-only view of another pilot's readiness, for coaching (UI §8).

    Same facets + quest log as the pilot sees, but no action buttons — an officer
    can see what to coach toward without being able to action someone else's quests.
    """
    from django.core.cache import cache
    from django.db.models import Q
    from django.utils import timezone

    from apps.sso.models import EveCharacter

    from .models import PilotRecommendation
    from .pilot import cache_key, compute_pilot

    character = get_object_or_404(EveCharacter, character_id=character_id, is_corp_member=True)
    # Officer access to another member's private readiness is sensitive (doc 14 §3) — audit it.
    audit_log(request.user, "readiness.pilot.view", target_type="character",
              target_id=character.character_id, ip=client_ip(request))
    # Read-only: never persist from an officer's view (the pilot's own warm owns that).
    payload = cache.get(cache_key(character.character_id))
    if payload is None:
        payload = compute_pilot(character, persist=False)
    facets = payload["facets"]
    scored = {k: v for k, v in facets.items() if v is not None}
    lowest = min(scored, key=scored.get) if scored else None

    target_user = getattr(character, "user", None)
    if target_user is not None:
        recos = list(
            PilotRecommendation.objects.filter(
                user=target_user, state=PilotRecommendation.State.OPEN
            ).filter(Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=timezone.now()))
        )
    else:
        # Unlinked character — no DB-backed quest log to read; the facets still coach.
        recos = []
    return render(request, "readiness/me.html", {
        "no_character": False,
        "viewing_other": True,
        "character": character,
        "overall": payload["overall"],
        "facets": [{"key": k, "score": facets.get(k)} for k in
                   ("doctrine", "combat", "logistics", "strategic", "activity", "contribution")],
        "lowest": lowest,
        "recommendations": recos,
        "contributions": payload["contributions"],
    })


def _kpi_status_override(kpi, kpi_cfg: dict) -> None:
    """Re-band a KPI's display status from its configured amber/red, in place."""
    thr = (kpi_cfg.get(kpi.key) or {}).get("thresholds")
    if thr and kpi.score is not None:
        kpi.status = ("green" if kpi.score >= thr["amber"]
                      else "amber" if kpi.score >= thr["red"] else "red")


@login_required
@role_required(rbac.ROLE_OFFICER)
def dimension_detail(request: HttpRequest, key: str) -> HttpResponse:
    """Drill-down for one dimension: why the score is what it is, KPI by KPI."""
    from .config import get as config_get
    from .engine import registry
    from .services import compute_dimension

    provider = registry.get(key)
    if provider is None:
        from django.http import Http404

        raise Http404("Unknown readiness dimension.")
    result = compute_dimension(key)
    dim_cfg = config_get("dimensions").get(key, {})
    # A disabled KPI doesn't contribute to the score, so it's dropped from the "why
    # this score" table; the rest carry their configured status bands.
    if result is not None:
        kpi_cfg = config_get("kpis")
        kept = []
        for k in result.kpis:
            if not (kpi_cfg.get(k.key) or {}).get("enabled", True):
                continue
            _kpi_status_override(k, kpi_cfg)
            kept.append(k)
        result.kpis = kept
    return render(request, "readiness/dimension.html", {
        "key": key,
        "label": getattr(provider, "label", key.title()),
        "data_sources": getattr(provider, "data_sources", []),
        "result": result,
        "enabled": dim_cfg.get("enabled", True),
        "weight": dim_cfg.get("weight", 1.0),
        "is_director": rbac.has_role(request.user, rbac.ROLE_DIRECTOR),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def kpi_detail(request: HttpRequest, key: str) -> HttpResponse:
    """Drill-down for one KPI: its current value, history trend, and open findings.

    The KPI key is dimension-namespaced (``<dimension>.<kpi>``); the trend reads the
    per-KPI history now recorded on each snapshot (FR4), so a KPI's score over time is
    visible without a per-KPI table.
    """
    from django.http import Http404

    from .config import get as config_get
    from .engine import registry
    from .models import ReadinessFinding, ReadinessSnapshot
    from .services import compute_dimension

    dimension_key = key.split(".", 1)[0]
    provider = registry.get(dimension_key)
    if provider is None:
        raise Http404("Unknown readiness dimension.")
    result = compute_dimension(dimension_key)
    kpi = None
    if result is not None:
        kpi = next((k for k in result.kpis if k.key == key), None)
    if kpi is None:
        raise Http404("Unknown KPI for this dimension.")
    kpi_cfg = config_get("kpis")
    _kpi_status_override(kpi, kpi_cfg)
    this_cfg = kpi_cfg.get(key) or {}
    kpi_disabled = not this_cfg.get("enabled", True)
    kpi_weight = this_cfg.get("weight", 1.0)

    # Trend: the KPI's score across the last 30 snapshots (oldest → newest), skipping
    # snapshots predating the per-KPI column (their score is absent).
    rows = list(ReadinessSnapshot.objects.order_by("-created_at")[:30])
    trend = [s.kpis.get(key, {}).get("score") for s in reversed(rows)]
    trend = [t for t in trend if t is not None]

    findings = list(
        ReadinessFinding.objects.filter(
            kpi_key=key,
            status__in=[ReadinessFinding.Status.OPEN, ReadinessFinding.Status.ACKNOWLEDGED],
        ).order_by("-weight")[:10]
    )
    return render(request, "readiness/kpi.html", {
        "key": key,
        "dimension_key": dimension_key,
        "dimension_label": getattr(provider, "label", dimension_key.title()),
        "kpi": kpi,
        "kpi_disabled": kpi_disabled,
        "kpi_weight": kpi_weight,
        "trend": trend,
        "trend_max": max(trend) if trend else 100,
        "findings": findings,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def simulator(request: HttpRequest) -> HttpResponse:
    """Can-we-field-it: qualified roster vs the configured strategic-role targets.

    4.11: an optional **availability-aware** mode (``?available=1``) counts only pilots
    who've logged in recently — a realistic "who'll actually show up" view — instead of
    the theoretical roster maximum. It's an aggregate activity signal, never per-pilot
    presence tracking.
    """
    from apps.admin_audit.models import AppSetting

    from .dimensions.roles import active_member_ids, qualified_count
    from .models import StrategicRoleTarget

    available = request.GET.get("available") == "1"
    try:
        window_days = int(AppSetting.get("readiness.sim_availability_days", 30))
    except (TypeError, ValueError):  # a malformed JSON config value must not 500 the page
        window_days = 30
    window_days = window_days or 30
    active_ids = active_member_ids(window_days) if available else None

    targets = list(StrategicRoleTarget.objects.filter(active=True, desired_count__gt=0))
    rows = []
    any_short = False
    total_desired = total_fieldable = 0
    for target in targets:
        qualified = qualified_count(target, only_char_ids=active_ids)  # None ⇒ not auto-detectable
        if qualified is None:
            rows.append({"role": target.label, "desired": target.desired_count,
                         "qualified": None, "fieldable": None, "short": 0, "status": "unknown"})
            continue
        fieldable = min(qualified, target.desired_count)
        short = max(0, target.desired_count - qualified)
        total_desired += target.desired_count
        total_fieldable += fieldable
        if short:
            any_short = True
        rows.append({
            "role": target.label, "desired": target.desired_count, "qualified": qualified,
            "fieldable": fieldable, "short": short,
            "status": "ready" if short == 0 else ("tight" if short <= 1 else "short"),
            "pct": round(100 * fieldable / target.desired_count) if target.desired_count else 100,
        })

    measurable = [r for r in rows if r["qualified"] is not None]
    if not measurable:
        verdict = "no_data"
    elif not any_short:
        verdict = "ready"
    elif total_fieldable >= total_desired * 0.6:
        verdict = "partial"
    else:
        verdict = "cannot_field"

    return render(request, "readiness/simulator.html", {
        "rows": rows,
        "verdict": verdict,
        "total_desired": total_desired,
        "total_fieldable": total_fieldable,
        "fill_pct": round(100 * total_fieldable / total_desired) if total_desired else 0,
        "has_targets": bool(targets),
        "available": available,
        "window_days": window_days,
        "active_count": len(active_ids) if active_ids is not None else None,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def timeline(request: HttpRequest) -> HttpResponse:
    """Historical evolution of the overall index and each dimension over time."""
    import datetime as dt

    from django.utils import timezone

    from .models import ReadinessSnapshot

    ranges = {"30d": 30, "90d": 90, "180d": 180, "1y": 365}
    rkey = request.GET.get("range", "90d")
    days = ranges.get(rkey, 90)
    since = timezone.now() - dt.timedelta(days=days)

    rows = list(
        ReadinessSnapshot.objects.filter(created_at__gte=since).order_by("created_at")
    )
    # Thin to at most ~60 points so the chart stays legible over long ranges.
    if len(rows) > 60:
        step = len(rows) // 60 + 1
        rows = rows[::step]

    series = [{
        "label": r.created_at.strftime("%d %b"),
        "index": r.index,
        "dimensions": {k: v for k, v in (r.dimensions or {}).items() if v is not None},
    } for r in rows]
    # The dimension keys that appear anywhere in the window (for the chart legend).
    dim_keys = sorted({k for s in series for k in s["dimensions"]})

    # Period summary: biggest mover up/down + net index change over the window.
    best = worst = None
    net = 0
    if len(series) >= 2:
        first, last = series[0], series[-1]
        net = last["index"] - first["index"]
        deltas = []
        for k in dim_keys:
            a, b = first["dimensions"].get(k), last["dimensions"].get(k)
            if isinstance(a, int) and isinstance(b, int):
                deltas.append((k, b - a))
        if deltas:
            deltas.sort(key=lambda d: d[1])
            worst = deltas[0] if deltas[0][1] < 0 else None
            best = deltas[-1] if deltas[-1][1] > 0 else None

    return render(request, "readiness/timeline.html", {
        "chart": {"series": series, "dim_keys": dim_keys},
        "ranges": list(ranges),
        "active_range": rkey,
        "enough": len(series) >= 2,
        "best": best,
        "worst": worst,
        "net": net,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def alerts_log(request: HttpRequest) -> HttpResponse:
    """The fired-alert log + escalation/resolution state, and the report archive."""
    from .models import ExecutiveReport, ReadinessAlert

    alerts = list(ReadinessAlert.objects.all()[:100])
    return render(request, "readiness/alerts.html", {
        "alerts": alerts,
        "open_count": sum(1 for a in alerts if a.is_open),
        "reports": list(ExecutiveReport.objects.all()[:12]),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def weekly_report(request: HttpRequest) -> HttpResponse:
    """The standalone Weekly Executive Report page (doc 10 §5).

    Renders the full body of one archived ``ExecutiveReport`` (latest by default, or the
    ``?period_start=`` one) — index, biggest movers, top risks and top tasks — with a
    rail of prior weeks to switch between. The report itself is composed by the weekly
    beat; this view only reads the archive.
    """
    from .models import ExecutiveReport

    reports = list(ExecutiveReport.objects.all()[:26])
    selected = None
    ps = request.GET.get("period_start")
    if ps:
        selected = next((r for r in reports if r.period_start.isoformat() == ps), None)
    if selected is None:
        selected = reports[0] if reports else None
    return render(request, "readiness/report.html", {
        "report": selected,
        "body": selected.body if selected else None,
        "reports": reports,
        "selected_start": selected.period_start.isoformat() if selected else None,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def findings_register(request: HttpRequest) -> HttpResponse:
    """The risk register: current findings (gaps/risks) with age, and their tasks."""
    from .models import ReadinessFinding

    active = list(
        ReadinessFinding.objects.filter(
            status__in=[ReadinessFinding.Status.OPEN, ReadinessFinding.Status.ACKNOWLEDGED]
        ).select_related("task")
    )
    resolved_count = ReadinessFinding.objects.filter(
        status=ReadinessFinding.Status.RESOLVED
    ).count()
    return render(request, "readiness/findings.html", {
        "findings": active,
        "resolved_count": resolved_count,
        "is_director": rbac.has_role(request.user, rbac.ROLE_DIRECTOR),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def task_queue(request: HttpRequest) -> HttpResponse:
    """A lens over the task board: readiness tasks grouped by owner for triage."""
    import datetime as dt

    from django.utils import timezone

    from apps.tasks.models import Task

    from . import config as config_module
    from .models import ReadinessFinding
    from .tasks_bridge import RELATED_TYPE

    active = list(
        Task.objects.filter(related_type=RELATED_TYPE)
        .exclude(status__in=[Task.Status.DONE, Task.Status.CANCELLED])
        .select_related("assignee")
    )
    findings_by_task = {
        f.task_id: f
        for f in ReadinessFinding.objects.filter(task_id__in=[t.id for t in active])
    }
    owner_labels = {
        tag: entry.get("label", tag)
        for tag, entry in (config_module.get("responsibilities").get("owner_tags") or {}).items()
    }

    groups: dict[str, list] = {}
    unowned: list = []
    for task in active:
        finding = findings_by_task.get(task.id)
        tag = finding.owner_tag if finding else ""
        row = {"task": task, "finding": finding}
        if tag and tag in owner_labels:
            groups.setdefault(owner_labels[tag], []).append(row)
        else:
            unowned.append(row)

    since = timezone.now() - dt.timedelta(days=7)
    recently_closed = list(
        Task.objects.filter(
            related_type=RELATED_TYPE,
            status__in=[Task.Status.DONE, Task.Status.CANCELLED],
            updated_at__gte=since,
        ).select_related("assignee")[:25]
    )
    return render(request, "readiness/tasks.html", {
        "groups": sorted(groups.items()),
        "unowned": unowned,
        "recently_closed": recently_closed,
        "active_count": len(active),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def create_tasks_from_gap(request: HttpRequest) -> HttpResponse:
    """Turn a finding (or a legacy dashboard gap) into a claimable task — idempotent."""
    from apps.tasks.models import Task

    # New path (doc 12 §4.a): the officer clicks "Create task" on a finding row.
    finding_id = (request.POST.get("finding_id") or "").strip()
    if finding_id:
        from .models import ReadinessFinding
        from .tasks_bridge import active_task_exists, task_for_finding

        if not finding_id.isdigit():
            messages.error(request, "Invalid finding.")
            return redirect("readiness:findings")
        finding = get_object_or_404(ReadinessFinding, pk=finding_id)
        if active_task_exists(finding):
            messages.info(request, "A task for that finding is already open.")
            return redirect("readiness:findings")
        task = task_for_finding(finding, user=request.user)
        audit_log(request.user, "readiness.gap_tasked",
                  target_type="readiness", target_id=str(finding.id),
                  metadata={"task_id": task.id}, ip=client_ip(request))
        messages.success(request, "Task created from finding — now open to claim.")
        return redirect("readiness:findings")

    # Legacy path: a gap row on the dashboard (kept for backward compatibility).
    kind = (request.POST.get("kind") or "").strip()
    ref_id = (request.POST.get("ref_id") or "").strip()
    title = (request.POST.get("title") or "").strip()
    task_type = request.POST.get("task_type") or Task.Type.OTHER
    if task_type not in Task.Type.values:
        task_type = Task.Type.OTHER
    if not (kind and ref_id and title):
        messages.error(request, "Incomplete gap.")
        return redirect("readiness:dashboard")

    related_type = f"gap:{kind}"
    existing = Task.objects.filter(
        related_type=related_type, related_id=ref_id,
        status__in=[Task.Status.OPEN, Task.Status.CLAIMED, Task.Status.IN_PROGRESS],
    ).exists()
    if existing:
        messages.info(request, "A task for that gap is already open.")
        return redirect("readiness:dashboard")

    Task.objects.create(
        type=task_type, title=title, is_open=True, status=Task.Status.OPEN,
        priority=10, created_by=request.user,
        related_type=related_type, related_id=ref_id,
    )
    audit_log(
        request.user, "readiness.gap_tasked",
        target_type=related_type, target_id=ref_id,
        ip=client_ip(request),
    )
    messages.success(request, "Task created from gap — now open to claim.")
    return redirect("readiness:dashboard")
