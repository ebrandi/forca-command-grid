"""Command Intelligence views (officer-gated, classification-filtered).

The leadership web surface (design doc 16): a Command Overview posture read, the
Operational Constraints board, the classification-filtered Reports list, and the
report rendered as a staff briefing with an async (htmx-polled) generation flow plus
the Course-of-Action accept/dismiss decisions. The web tier only ever calls the
public service API (``services`` / ``coa``) and the classification gate (``access``);
it never builds a snapshot or calls the LLM itself.
"""
from __future__ import annotations

import datetime as dt

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core.features import feature_required
from core.rbac import ROLE_MEMBER, ROLE_OFFICER, role_required

from . import access, forecast, services, simulation
from .campaign import measured_trajectory
from .coa import accept_coa, dismiss_coa
from .models import (
    BattleAnalysis,
    Campaign,
    ConversationTurn,
    CourseOfAction,
    IntelligenceReport,
    OperationalConstraint,
    PilotDirective,
)
from .snapshot import latest_snapshot

# Severity ordering for the board (the model stores the enum string, which does not
# sort by urgency on its own).
_SEV_RANK = {"critical": 4, "high": 3, "watch": 2, "info": 1}


def _sorted_constraints(snapshot) -> list[OperationalConstraint]:
    """All constraints for a snapshot, most-binding first (severity, then score)."""
    if snapshot is None:
        return []
    rows = OperationalConstraint.objects.filter(snapshot=snapshot)
    return sorted(
        rows,
        key=lambda c: (_SEV_RANK.get(c.severity, 0), c.score or 0),
        reverse=True,
    )


def _posture_snapshot(user):
    """The snapshot backing the posture read: the latest viewable report's, else the
    freshest snapshot on file (so the overview is useful before the first report)."""
    report = (
        access.visible_reports(user)
        .select_related("snapshot")
        .order_by("-created_at")
        .first()
    )
    snapshot = report.snapshot if (report and report.snapshot_id) else latest_snapshot()
    return report, snapshot


def _readiness_index(snapshot) -> int | None:
    if snapshot is None:
        return None
    return (snapshot.slices.get("readiness") or {}).get("overall_index")


def _back(request, default_url: str):
    """Redirect to the POSTed ``next`` (same-origin only) or a safe default."""
    nxt = request.POST.get("next") or request.headers.get("Referer", "")
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(nxt)
    return redirect(default_url)


@login_required
@role_required(ROLE_OFFICER)
def overview(request):
    """Command Overview — posture hero, top binding constraints, latest briefing."""
    report, snapshot = _posture_snapshot(request.user)
    rows = _sorted_constraints(snapshot)
    top = [c for c in rows if c.severity != "info"][:4] or rows[:4]
    return render(
        request,
        "command_intel/overview.html",
        {
            "posture": _readiness_index(snapshot),
            "snapshot": snapshot,
            "top_constraints": top,
            "latest_report": report,
            "ai_enabled": settings.COMMAND_INTEL_ENABLED,
        },
    )


@login_required
@role_required(ROLE_OFFICER)
def constraints(request):
    """The Operational Constraints board from the latest snapshot."""
    snapshot = latest_snapshot()
    rows = _sorted_constraints(snapshot)
    computed = sum(1 for c in rows if c.status == "computed")
    unknown = sum(1 for c in rows if c.status != "computed")
    return render(
        request,
        "command_intel/constraints.html",
        {
            "snapshot": snapshot,
            "constraints": rows,
            "computed_count": computed,
            "unknown_count": unknown,
        },
    )


@login_required
@role_required(ROLE_OFFICER)
def reports(request):
    """Classification-filtered report list, newest first."""
    qs = access.visible_reports(request.user).order_by("-created_at")
    return render(request, "command_intel/reports.html", {"reports": qs})


@login_required
@role_required(ROLE_OFFICER)
def report_detail(request, pk: int):
    """A report rendered as a staff briefing — or the generating page while building."""
    report = get_object_or_404(IntelligenceReport, pk=pk)
    if not access.can_view_report(request.user, report):
        raise PermissionDenied("Above your clearance.")
    return render(request, "command_intel/report_detail.html", {"report": report})


@login_required
@role_required(ROLE_OFFICER)
def report_status(request, pk: int):
    """htmx poll target: the briefing fragment once terminal, else the lifecycle stepper.

    A non-htmx (direct) GET falls back to the full report page, so a refresh during
    generation is always safe.
    """
    report = get_object_or_404(IntelligenceReport, pk=pk)
    if not access.can_view_report(request.user, report):
        raise PermissionDenied("Above your clearance.")
    if not request.headers.get("HX-Request"):
        return redirect("command_intel:report_detail", pk=report.pk)
    template = (
        "command_intel/_briefing.html"
        if report.is_terminal
        else "command_intel/_generating.html"
    )
    return render(request, template, {"report": report})


@login_required
@role_required(ROLE_OFFICER)
@require_POST
def generate(request):
    """Queue a new report and send the requester to its (polling) detail page."""
    report = services.request_report(user=request.user)
    return redirect("command_intel:report_detail", pk=report.pk)


@login_required
@role_required(ROLE_OFFICER)
@require_POST
def coa_accept(request, pk: int):
    """Accept a Course of Action — converts it to a tracked task."""
    coa = get_object_or_404(CourseOfAction, pk=pk)
    if not access.can_view_coa(request.user, coa):
        raise PermissionDenied("Above your clearance.")
    accept_coa(coa, request.user)
    default = (
        reverse("command_intel:report_detail", args=[coa.report_id])
        if coa.report_id
        else reverse("command_intel:overview")
    )
    return _back(request, default)


@login_required
@role_required(ROLE_OFFICER)
@require_POST
def coa_dismiss(request, pk: int):
    """Dismiss a Course of Action with a recorded reason."""
    coa = get_object_or_404(CourseOfAction, pk=pk)
    if not access.can_view_coa(request.user, coa):
        raise PermissionDenied("Above your clearance.")
    dismiss_coa(coa, request.user, note=request.POST.get("note", ""))
    default = (
        reverse("command_intel:report_detail", args=[coa.report_id])
        if coa.report_id
        else reverse("command_intel:overview")
    )
    return _back(request, default)


# --- Strategic campaigns (the operational campaign planner, doc 16 §2.6) ------
@login_required
@role_required(ROLE_OFFICER)
def campaigns(request):
    """Strategic campaigns — every planned arc, newest first."""
    return render(
        request,
        "command_intel/campaigns.html",
        {"campaigns": Campaign.objects.order_by("-created_at")},
    )


@login_required
@role_required(ROLE_OFFICER)
def campaign_detail(request, pk: int):
    """A campaign's milestone timeline + expected-vs-measured trajectory chart."""
    campaign = get_object_or_404(Campaign, pk=pk)
    # Hide milestone rows whose COA is above the viewer's clearance — a milestone
    # title is copied from coa.objective, so an above-clearance COA composed into a
    # campaign must not leak its objective here (select_related the report to avoid N+1).
    milestones = [
        m for m in campaign.milestones.select_related("coa__report", "responsible_user").order_by("order")
        if m.coa_id is None or access.can_view_coa(request.user, m.coa)
    ]
    expected = campaign.expected_trajectory or []
    measured = measured_trajectory(campaign)
    chart = {
        "labels": ["Baseline"] + [f"M{i}" for i in range(1, len(expected))],
        "expected": [p.get("value") for p in expected],
        "measured": [m.get("value") for m in measured],
        "target": float(campaign.target_value) if campaign.target_value is not None else None,
    }
    current = (
        measured[-1]["value"]
        if measured
        else (float(campaign.baseline_value) if campaign.baseline_value is not None else None)
    )
    return render(
        request,
        "command_intel/campaign_detail.html",
        {"campaign": campaign, "milestones": milestones, "chart": chart, "current": current},
    )


@login_required
@role_required(ROLE_OFFICER)
def campaign_new(request):
    """Compose form — pick composable COAs and a target metric for a new campaign."""
    # Only COAs the officer is cleared to read — a director-tier report's COA must
    # not leak its objective into the picker (rendered as {{ coa.objective }}).
    coas = access.visible_coas(
        request.user,
        CourseOfAction.objects.filter(campaign__isnull=True).exclude(
            state__in=[CourseOfAction.State.DISMISSED, CourseOfAction.State.SUPERSEDED]
        ),
    ).order_by("-priority", "-created_at")
    snapshot = latest_snapshot()
    metric_keys: list[str] = []
    if snapshot is not None:
        metric_keys = list(
            OperationalConstraint.objects.filter(
                snapshot=snapshot, status=OperationalConstraint.Status.COMPUTED
            )
            .order_by("key")
            .values_list("key", flat=True)
            .distinct()
        )
    return render(
        request,
        "command_intel/campaign_new.html",
        {"coas": coas, "metric_keys": metric_keys},
    )


@login_required
@role_required(ROLE_OFFICER)
@require_POST
def campaign_compose(request):
    """Create a DRAFT campaign from the selected COAs and send the user to its planner."""
    objective = (request.POST.get("objective") or "").strip()
    target_metric = (request.POST.get("target_metric") or "readiness.overall").strip()
    target_value = (request.POST.get("target_value") or "").strip() or None
    coa_ids = request.POST.getlist("coa_ids")
    if not objective or not coa_ids:
        return _back(request, reverse("command_intel:campaign_new"))
    campaign = services.compose_campaign(
        objective=objective,
        target_metric=target_metric,
        target_value=target_value,
        coa_ids=coa_ids,
        user=request.user,
    )
    return redirect("command_intel:campaign_detail", pk=campaign.pk)


@login_required
@role_required(ROLE_OFFICER)
@require_POST
def campaign_launch(request, pk: int):
    """DRAFT → ACTIVE: anchor the baseline and start the clock."""
    campaign = get_object_or_404(Campaign, pk=pk)
    services.launch_campaign(campaign, request.user)
    return _back(request, reverse("command_intel:campaign_detail", args=[campaign.pk]))


@login_required
@role_required(ROLE_OFFICER)
@require_POST
def campaign_abandon(request, pk: int):
    """Abandon a campaign with a recorded reason."""
    campaign = get_object_or_404(Campaign, pk=pk)
    services.abandon_campaign(campaign, request.user, note=request.POST.get("note", ""))
    return _back(request, reverse("command_intel:campaign_detail", args=[campaign.pk]))


# --- Pilot Intelligence (member self-service quest log, doc 16 §7) ------------
# The quest log's page moved into the merged Daily Briefing (pilots:briefing);
# the list helper lives in pilot.open_directives. This app keeps the directive
# state endpoint (the model is ours) and a gated redirect for old bookmarks.


@login_required
@role_required(ROLE_MEMBER)
@feature_required("command_intel_pilot")
def me(request):
    """Absorbed into the Command Center (/dashboard/) — redirect old bookmarks.

    Stays gated by ``command_intel_pilot`` so the old URL keeps 404-ing when the
    pilot slice is disabled (the merged page hides its orders section instead).
    """
    return redirect("identity:dashboard")


def _credit_directive(directive) -> None:
    """CMD-2 (3.6): recognition credit for completing a directive — a ledger entry + feed
    shout, magnitude = the directive's points. Future-only, idempotent per directive
    (ref_id), never moves ISK. The raffle 'directive' source applies the enrolled+valid-ESI
    gate for tickets separately."""
    from apps.pilots.models import ContributionEvent
    from apps.pilots.services import record_contribution

    record_contribution(
        directive.user, kind=ContributionEvent.Kind.DIRECTIVE,
        magnitude=1, unit="directives",
        points=max(1, directive.points),  # the directive's own points are the recognition score
        description=directive.title, ref_type="directive", ref_id=str(directive.pk),
        occurred_at=directive.completed_at or timezone.now(),
    )


@login_required
@role_required(ROLE_MEMBER)
@feature_required("command_intel_pilot")
@require_POST
def directive_action(request, pk: int):
    """Act on one of the member's OWN directives (done / snooze 7d / dismiss). IDOR-safe."""
    from django.contrib import messages

    directive = get_object_or_404(PilotDirective, pk=pk, user=request.user)
    action = request.POST.get("action")
    if action == "done":
        newly_done = directive.state != PilotDirective.State.DONE
        directive.state = PilotDirective.State.DONE
        if newly_done and directive.completed_at is None:
            directive.completed_at = timezone.now()
            directive.save(update_fields=["state", "completed_at", "updated_at"])
            _credit_directive(directive)  # CMD-2 (3.6): recognition on completion
        else:
            directive.save(update_fields=["state", "updated_at"])
        messages.success(request, "Order complete — o7.")
    elif action == "dismiss":
        directive.state = PilotDirective.State.DISMISSED
        directive.save(update_fields=["state", "updated_at"])
        messages.info(request, "Dismissed — this order won't be suggested again.")
    elif action == "snooze":
        directive.snoozed_until = timezone.now() + dt.timedelta(days=7)
        directive.save(update_fields=["snoozed_until", "updated_at"])
        messages.info(request, "Snoozed — it returns in 7 days.")
    return redirect("identity:dashboard")


# --- Conversational intelligence (officer Q&A over the archive, doc 17 §3) -----
@login_required
@role_required(ROLE_OFFICER)
def ask(request):
    """The officer's conversational Q&A over the archive — their own recent turns."""
    turns = list(ConversationTurn.objects.filter(user=request.user).order_by("-created_at")[:20])
    return render(
        request,
        "command_intel/ask.html",
        {"turns": turns, "ai_enabled": settings.COMMAND_INTEL_ENABLED},
    )


@login_required
@role_required(ROLE_OFFICER)
@require_POST
def ask_submit(request):
    """Queue a question and send the officer back to the (polling) conversation."""
    question = (request.POST.get("question") or "").strip()
    if question:
        services.request_answer(user=request.user, question=question)
    return redirect("command_intel:ask")


@login_required
@role_required(ROLE_OFFICER)
def ask_status(request, pk: int):
    """htmx poll target for one turn card — self-only (a turn belongs to its asker)."""
    turn = get_object_or_404(ConversationTurn, pk=pk, user=request.user)
    if not request.headers.get("HX-Request"):
        return redirect("command_intel:ask")
    return render(request, "command_intel/_ask_turn.html", {"turn": turn})


# --- Combat Intelligence: battle after-action reviews (officer) ---------------
@login_required
@role_required(ROLE_OFFICER)
def battles(request):
    """Recent killboard battles with their AI after-action-review status."""
    from apps.killboard.models import BattleReport

    reports = list(BattleReport.objects.order_by("-start_time")[:30])
    latest: dict[int, BattleAnalysis] = {}
    for a in (
        BattleAnalysis.objects.filter(battle_report_id__in=[r.pk for r in reports])
        .order_by("battle_report_id", "-created_at")
    ):
        latest.setdefault(a.battle_report_id, a)  # newest per battle (─created_at within battle)
    # Hide even the existence/status of an AAR above the viewer's clearance (the list must
    # not leak metadata the detail/poll/retrieval surfaces already gate).
    rows = [
        {"battle": r, "analysis": a if (a := latest.get(r.pk)) and access.can_view_report(request.user, a) else None}
        for r in reports
    ]
    return render(
        request,
        "command_intel/battles.html",
        {"rows": rows, "ai_enabled": settings.COMMAND_INTEL_ENABLED},
    )


@login_required
@role_required(ROLE_OFFICER)
def battle_detail(request, battle_id: int):
    """A battle + its after-action review (generate when absent, poll while building)."""
    from apps.killboard.models import BattleReport

    battle_report = get_object_or_404(BattleReport, pk=battle_id)
    analysis = (
        BattleAnalysis.objects.filter(battle_report_id=battle_id).order_by("-created_at").first()
    )
    if analysis is not None and not access.can_view_report(request.user, analysis):
        analysis = None  # AAR above the viewer's clearance — still show the battle itself
    return render(
        request,
        "command_intel/battle_detail.html",
        {"battle": battle_report, "analysis": analysis, "ai_enabled": settings.COMMAND_INTEL_ENABLED},
    )


@login_required
@role_required(ROLE_OFFICER)
@require_POST
def battle_generate(request, battle_id: int):
    """Queue an after-action review for a battle and return to its (polling) detail page."""
    from apps.killboard.models import BattleReport

    get_object_or_404(BattleReport, pk=battle_id)
    services.request_battle_analysis(user=request.user, battle_report_id=battle_id)
    return redirect("command_intel:battle_detail", battle_id=battle_id)


@login_required
@role_required(ROLE_OFFICER)
def battle_status(request, pk: int):
    """htmx poll target for a battle AAR fragment — classification-gated."""
    analysis = get_object_or_404(BattleAnalysis, pk=pk)
    if not access.can_view_report(request.user, analysis):
        raise PermissionDenied("Above your clearance.")
    if not request.headers.get("HX-Request"):
        return redirect("command_intel:battle_detail", battle_id=analysis.battle_report_id)
    return render(request, "command_intel/_battle_aar.html", {"analysis": analysis})


# --- Simulation / Readiness Digital Twin (officer what-if, doc 17 §1) ---------
# Bounds on the shared saved-scenario library (4.18): cap live re-runs per compare page,
# and cap total library size so unbounded creation can't feed a slow compare.
MAX_COMPARE = 25
MAX_SAVED_SCENARIOS = 100


@login_required
@role_required(ROLE_OFFICER)
def simulator(request):
    """A deterministic what-if: perturb the latest snapshot, recompute, diff constraints."""
    from .models import SavedSimScenario

    scenario = request.GET.get("scenario") or "pilot_attrition"
    magnitude = request.GET.get("magnitude")
    result = simulation.simulate(scenario, magnitude)
    scenarios = simulation.scenario_list(scenario, magnitude)
    selected_scenario = next((s for s in scenarios if s["selected"]), scenarios[0])
    return render(
        request,
        "command_intel/sim.html",
        {
            "scenarios": scenarios,
            "selected_scenario": selected_scenario,
            "result": result,
            "forecasts": forecast.forecast_findings(),
            "snapshot": result.get("snapshot"),
            "saved": list(SavedSimScenario.objects.all()),  # 4.18 shared library
        },
    )


@login_required
@role_required(ROLE_OFFICER)
@require_POST
def simulator_save(request):
    """4.18: save the current what-if as a named, re-runnable scenario (shared library)."""
    from django.contrib import messages

    from core.audit import audit_log, client_ip

    from .models import SavedSimScenario

    name = (request.POST.get("name") or "").strip()[:120]
    if not name:
        messages.error(request, "Give the scenario a name before saving.")
        return redirect("command_intel:simulator")
    if SavedSimScenario.objects.count() >= MAX_SAVED_SCENARIOS:
        messages.error(request, f"The saved-scenario library is full ({MAX_SAVED_SCENARIOS}). "
                                "Delete an old scenario before adding another.")
        return redirect("command_intel:simulator")
    key, mag = simulation.validate_scenario(request.POST.get("scenario"), request.POST.get("magnitude"))
    scenario = SavedSimScenario.objects.create(
        name=name, scenario_key=key, magnitude=mag,
        notes=(request.POST.get("notes") or "").strip()[:280], created_by=request.user,
    )
    audit_log(request.user, "command_intel.sim.save", target_type="saved_sim_scenario",
              target_id=str(scenario.pk), ip=client_ip(request),
              metadata={"name": name, "scenario": key, "magnitude": mag})
    messages.success(request, f"Saved scenario “{name}”.")
    return redirect(f"{reverse('command_intel:simulator')}?scenario={key}&magnitude={mag}")


@login_required
@role_required(ROLE_OFFICER)
@require_POST
def simulator_delete(request, pk: int):
    """4.18: remove a saved scenario from the shared library."""
    from django.contrib import messages

    from core.audit import audit_log, client_ip

    from .models import SavedSimScenario

    scenario = get_object_or_404(SavedSimScenario, pk=pk)
    name = scenario.name
    scenario.delete()
    audit_log(request.user, "command_intel.sim.delete", target_type="saved_sim_scenario",
              target_id=str(pk), ip=client_ip(request), metadata={"name": name})
    messages.success(request, f"Removed scenario “{name}”.")
    return redirect("command_intel:simulator")


@login_required
@role_required(ROLE_OFFICER)
def simulator_compare(request):
    """4.18: run several saved scenarios against the latest snapshot and lay them out
    side-by-side for contingency planning (which stressor hurts most). ``?id=`` may be
    repeated to compare a subset; with none, the whole saved library is compared."""
    from .models import SavedSimScenario

    ids = [int(i) for i in request.GET.getlist("id") if i.isdigit()]
    qs = SavedSimScenario.objects.filter(pk__in=ids) if ids else SavedSimScenario.objects.all()
    # Bound the number of live re-runs one page can trigger (each is a deepcopy + a
    # constraint recompute) so a large library can't block a worker — review MED fix.
    saved = list(qs[:MAX_COMPARE])
    comparison = simulation.compare(saved)
    return render(request, "command_intel/sim_compare.html", {
        "comparison": comparison, "count": len(comparison),
        "truncated": qs.count() > len(saved),
        "max_compare": MAX_COMPARE,
        "snapshot_missing": bool(comparison) and not comparison[0]["available"],
    })
