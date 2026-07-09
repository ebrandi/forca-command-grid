"""Raffle Contest admin console (/ops/admin/raffle/…).

The complete leadership surface: contest list + CRUD, prize/source/eligibility
config, the manual-grant desk, the searchable ticket ledger, the ineligible-activity
(adoption) report, the draw manager (freeze → commit → draw → transparency/redraw),
the statistics report and archive management. Officer-gated for day-to-day work;
Director-gated for irreversible / ISK-adjacent actions (execute draw, redraw,
emergency override config, cancel). Every mutation is audited.

Follows the console conventions: thin views, ModelForm PRG, audit_log + client_ip,
data-confirm (never inline JS), CSV export where useful.
"""
from __future__ import annotations

import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.raffle import boosters, contest_templates, engine, integrity, metrics, services, stats
from apps.raffle.draw import verify_draw
from apps.raffle.forms import (
    RaffleConfigForm,
    RaffleContestForm,
    RaffleManualGrantForm,
    RafflePrizeForm,
    RaffleSourceConfigForm,
)
from apps.raffle.models import (
    RaffleContest,
    RaffleDraw,
    RaffleDrawResult,
    RaffleExclusion,
    RaffleIneligibleActivity,
    RafflePrize,
    RaffleSuspiciousActivityFlag,
    RaffleTicketLedgerEntry,
    RaffleTicketSourceConfig,
)
from apps.raffle.sources import all_sources, get_source
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

_STATUS_ORDER = [
    RaffleContest.Status.ACTIVE, RaffleContest.Status.CLOSED,
    RaffleContest.Status.SCHEDULED, RaffleContest.Status.DRAFT,
    RaffleContest.Status.COMPLETED, RaffleContest.Status.ARCHIVED,
    RaffleContest.Status.CANCELLED,
]

# Schedule + eligibility + draw-policy fields that must NOT change once a contest
# is live or frozen (editing them mid-contest would rewrite the deal or the draw).
_PROTECTED_EDIT_FIELDS = (
    "start_at", "end_at", "draw_at",
    "require_enrolled", "require_valid_token", "include_alliance", "required_scopes",
    "retroactive_enabled", "one_prize_per_pilot", "auto_draw",
    "booster_multiplier", "booster_start_at", "booster_end_at",
)


def _audit(request, action, **kw):
    audit_log(request.user, action, ip=client_ip(request), **kw)


def _contest(pk):
    return get_object_or_404(RaffleContest, pk=pk)


# --------------------------------------------------------------------------- #
#  Hub / list
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_OFFICER)
def raffle_hub(request):
    contests = list(RaffleContest.objects.all())
    order = {s: i for i, s in enumerate(_STATUS_ORDER)}
    contests.sort(key=lambda c: (order.get(c.status, 99), -(c.start_at.timestamp() if c.start_at else 0)))
    groups = {s: [] for s in _STATUS_ORDER}
    for c in contests:
        groups.setdefault(c.status, []).append(c)
    return render(request, "admin_audit/console/raffle_hub.html", {
        "groups": [(RaffleContest.Status(s).label, groups.get(s, [])) for s in _STATUS_ORDER],
        "templates": contest_templates.BUILTIN,
        "adoption": stats.adoption_metrics(),
        "is_director": rbac.has_role(request.user, rbac.ROLE_DIRECTOR),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def raffle_create(request):
    if request.method == "POST":
        form = RaffleContestForm(request.POST)
        if form.is_valid():
            contest = form.save(commit=False)
            contest.created_by = request.user
            contest.save()
            services.seed_source_configs(contest)
            template_key = request.POST.get("template_key", "")
            if template_key:
                contest_templates.apply_template(contest, template_key, overwrite_prizes=True)
            _audit(request, "raffle.create", target_type="raffle_contest",
                   target_id=str(contest.pk), metadata={"template": template_key})
            messages.success(request, f"Contest “{contest.name}” created as a draft.")
            return redirect("admin_audit:raffle_detail", pk=contest.pk)
        messages.error(request, "Please correct the errors below.")
    else:
        form = RaffleContestForm()
    return render(request, "admin_audit/console/raffle_form.html", {
        "form": form, "creating": True, "templates": contest_templates.BUILTIN,
        "kpi": metrics.kpi_panel(), "metrics": metrics.METRICS,
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def raffle_config(request):
    cfg = services.active_config()
    if request.method == "POST":
        form = RaffleConfigForm(request.POST, instance=cfg)
        if form.is_valid():
            form.save()
            _audit(request, "raffle.config", target_type="raffle_config", target_id=str(cfg.pk),
                   metadata={"override": cfg.allow_manual_override})
            messages.success(request, "Raffle settings saved.")
            return redirect("admin_audit:raffle_config")
    else:
        form = RaffleConfigForm(instance=cfg)
    return render(request, "admin_audit/console/raffle_config.html",
                  {"form": form, "config": cfg, "budget": services.budget_status()})


# --------------------------------------------------------------------------- #
#  Detail / edit
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_OFFICER)
def raffle_detail(request, pk):
    contest = _contest(pk)
    stat = stats.contest_statistics(contest, use_cache=False)
    source_rows = []
    for cfg in contest.source_configs.all().order_by("source_key"):
        src = get_source(cfg.source_key)
        source_rows.append({"config": cfg, "source": src,
                            "tickets": stat["by_source"].get(cfg.source_key, 0)})
    recent = list(RaffleTicketLedgerEntry.objects.filter(contest=contest).order_by("-created_at")[:20])
    flags = contest.suspicious_flags.filter(status=RaffleSuspiciousActivityFlag.Status.OPEN).count()
    draw = contest.draws.filter(status=RaffleDraw.Status.COMPLETED, superseded_by__isnull=True).first()
    return render(request, "admin_audit/console/raffle_detail.html", {
        "contest": contest, "stats": stat, "adoption": stat["adoption"],
        "source_rows": source_rows, "prizes": contest.prizes.order_by("rank"),
        "recent": recent, "open_flags": flags, "draw": draw,
        "next_statuses": [s for s in RaffleContest.Status if services.can_transition(contest, s)],
        "is_director": rbac.has_role(request.user, rbac.ROLE_DIRECTOR),
        "recommendations": stats.recommendations(contest) if contest.status in (
            RaffleContest.Status.COMPLETED, RaffleContest.Status.ARCHIVED) else [],
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def raffle_edit(request, pk):
    contest = _contest(pk)
    # Capture the editability on the ORIGINAL state — form.save(commit=False)
    # mutates the instance in place, so is_editable (which reads start_at) must be
    # evaluated before that, or a start-date change could flip the lock off.
    editable = contest.is_editable
    frozen = contest.is_frozen
    if request.method == "POST":
        form = RaffleContestForm(request.POST, instance=contest)
        if form.is_valid():
            obj = form.save(commit=False)
            if not editable:
                # A contest that has started accruing (or is closed) locks its
                # schedule + rules; silently preserve them and save only the
                # cosmetic/display fields, so an officer can't change eligibility,
                # the draw time or draw policy mid-contest (or after freeze).
                fresh = RaffleContest.objects.get(pk=contest.pk)
                for f in _PROTECTED_EDIT_FIELDS:
                    setattr(obj, f, getattr(fresh, f))
                messages.info(request,
                              "This contest has started accruing tickets (or is closed) — its "
                              "schedule and rules are locked; only the text/display fields were saved.")
            obj.save()
            _audit(request, "raffle.edit", target_type="raffle_contest", target_id=str(contest.pk),
                   metadata={"frozen_edit": not editable})
            messages.success(request, "Contest updated.")
            return redirect("admin_audit:raffle_detail", pk=contest.pk)
        messages.error(request, "Please correct the errors below.")
    else:
        form = RaffleContestForm(instance=contest)
    return render(request, "admin_audit/console/raffle_form.html", {
        "form": form, "contest": contest, "creating": False,
        "warn_live": not editable and not frozen,
        "kpi": metrics.kpi_panel(contest), "metrics": metrics.METRICS,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def raffle_status(request, pk):
    contest = _contest(pk)
    new_status = request.POST.get("status", "")
    if new_status == RaffleContest.Status.CANCELLED and not rbac.has_role(request.user, rbac.ROLE_DIRECTOR):
        messages.error(request, "Only a Director can cancel a contest.")
        return redirect("admin_audit:raffle_detail", pk=pk)
    if contest.status == RaffleContest.Status.DRAFT and new_status in (
        RaffleContest.Status.SCHEDULED, RaffleContest.Status.ACTIVE
    ):
        block = services.budget_block_reason(contest)
        if block:
            messages.error(request, block)
            return redirect("admin_audit:raffle_detail", pk=pk)
    if services.set_status(contest, new_status, request.user, reason=request.POST.get("reason", "")):
        messages.success(request, f"Contest moved to {new_status}.")
    else:
        messages.error(request, "That status change isn't allowed from the current state.")
    return redirect("admin_audit:raffle_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def raffle_apply_template(request, pk):
    contest = _contest(pk)
    if not contest.is_editable:
        messages.error(request, "Templates can only be applied to a draft/scheduled contest.")
        return redirect("admin_audit:raffle_detail", pk=pk)
    key = request.POST.get("template_key", "")
    if contest_templates.apply_template(contest, key, overwrite_prizes=True):
        _audit(request, "raffle.template", target_type="raffle_contest", target_id=str(pk),
               metadata={"template": key})
        messages.success(request, f"Applied the “{key}” template.")
    else:
        messages.error(request, "Unknown template.")
    return redirect("admin_audit:raffle_detail", pk=pk)


# --------------------------------------------------------------------------- #
#  Prizes
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_OFFICER)
def raffle_prizes(request, pk):
    contest = _contest(pk)
    return render(request, "admin_audit/console/raffle_prizes.html", {
        "contest": contest, "prizes": contest.prizes.order_by("rank"),
        "form": RafflePrizeForm(initial={"rank": contest.prizes.count() + 1}),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def raffle_prize_save(request, pk, prize_id=None):
    contest = _contest(pk)
    instance = get_object_or_404(RafflePrize, pk=prize_id, contest=contest) if prize_id else None
    form = RafflePrizeForm(request.POST, instance=instance)
    if form.is_valid():
        prize = form.save(commit=False)
        prize.contest = contest
        prize.save()
        _audit(request, "raffle.prize.save", target_type="raffle_prize", target_id=str(prize.pk))
        messages.success(request, "Prize saved.")
    else:
        messages.error(request, "; ".join(f"{k}: {v.as_text()}" for k, v in form.errors.items()))
    return redirect("admin_audit:raffle_prizes", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def raffle_prize_delete(request, pk, prize_id):
    contest = _contest(pk)
    prize = get_object_or_404(RafflePrize, pk=prize_id, contest=contest)
    prize.delete()
    _audit(request, "raffle.prize.delete", target_type="raffle_prize", target_id=str(prize_id))
    messages.success(request, "Prize removed.")
    return redirect("admin_audit:raffle_prizes", pk=pk)


# --------------------------------------------------------------------------- #
#  Sources
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_OFFICER)
def raffle_sources(request, pk):
    contest = _contest(pk)
    services.seed_source_configs(contest)
    rows = []
    for cfg in contest.source_configs.all().order_by("source_key"):
        rows.append({"config": cfg, "source": get_source(cfg.source_key),
                     "form": RaffleSourceConfigForm(instance=cfg, prefix=f"s{cfg.pk}")})
    return render(request, "admin_audit/console/raffle_sources.html", {
        "contest": contest, "rows": rows, "all_sources": all_sources(),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def raffle_source_save(request, pk, source_pk):
    contest = _contest(pk)
    cfg = get_object_or_404(RaffleTicketSourceConfig, pk=source_pk, contest=contest)
    form = RaffleSourceConfigForm(request.POST, instance=cfg, prefix=f"s{cfg.pk}")
    if form.is_valid():
        form.save()
        _audit(request, "raffle.source.save", target_type="raffle_source", target_id=str(cfg.pk),
               metadata={"source": cfg.source_key, "enabled": cfg.enabled})
        messages.success(request, f"“{cfg.source_key}” source saved.")
    else:
        messages.error(request, "; ".join(f"{k}: {v.as_text()}" for k, v in form.errors.items()))
    return redirect("admin_audit:raffle_sources", pk=pk)


# --------------------------------------------------------------------------- #
#  Manual grants
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_OFFICER)
def raffle_grant(request, pk):
    contest = _contest(pk)
    if request.method == "POST":
        form = RaffleManualGrantForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                grant = services.grant_manual_tickets(
                    contest, request.user, character_id=cd["character_id"], amount=cd["amount"],
                    reason=cd["reason"], category=cd["category"], internal_notes=cd["internal_notes"],
                    override=cd["override"],
                )
                messages.success(
                    request,
                    f"Granted {grant.amount} tickets to "
                    f"{grant.character_name or cd['character_id']}.",
                )
                return redirect("admin_audit:raffle_grant", pk=pk)
            except services.GrantBlocked as e:
                messages.error(request, str(e))
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = RaffleManualGrantForm()
    recent = list(contest.manual_grants.select_related("granted_by").order_by("-created_at")[:25])
    return render(request, "admin_audit/console/raffle_grant.html", {
        "contest": contest, "form": form, "recent": recent,
        "override_enabled": services.active_config().allow_manual_override,
        "is_director": rbac.has_role(request.user, rbac.ROLE_DIRECTOR),
    })


# --------------------------------------------------------------------------- #
#  Ledger
# --------------------------------------------------------------------------- #
def _ledger_qs(contest, request):
    qs = RaffleTicketLedgerEntry.objects.filter(contest=contest)
    src = request.GET.get("source")
    status = request.GET.get("status")
    q = request.GET.get("q")
    if src:
        qs = qs.filter(source_key=src)
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(character_name__icontains=q) | Q(source_ref__icontains=q))
    return qs.order_by("-created_at")


@login_required
@role_required(rbac.ROLE_OFFICER)
def raffle_ledger(request, pk):
    contest = _contest(pk)
    qs = _ledger_qs(contest, request)
    if request.GET.get("export") == "csv":
        return _csv_response(
            f"raffle-{contest.slug}-ledger.csv",
            ["created", "character", "character_id", "source", "ref", "amount", "status", "esi", "reason"],
            ([e.created_at.isoformat(), e.character_name, e.character_id, e.source_key,
              e.source_ref, e.amount, e.status, e.esi_status, e.reason]
             for e in qs.iterator(chunk_size=1000)),
        )
    entries = list(qs[:500])
    return render(request, "admin_audit/console/raffle_ledger.html", {
        "contest": contest, "entries": entries, "total": qs.count(),
        "sources": all_sources(),
        "statuses": RaffleTicketLedgerEntry.Status.choices,
        "cur": {"source": request.GET.get("source", ""), "status": request.GET.get("status", ""),
                "q": request.GET.get("q", "")},
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def raffle_ledger_action(request, pk, entry_id):
    contest = _contest(pk)
    entry = get_object_or_404(RaffleTicketLedgerEntry, pk=entry_id, contest=contest)
    action = request.POST.get("action")
    reason = request.POST.get("reason", "")
    if action == "reverse":
        services.reverse_entry(entry, request.user, reason=reason or "correction")
        messages.success(request, "Entry reversed (a correcting row was appended).")
    elif action in ("approved", "excluded", "disqualified"):
        if services.set_entry_status(entry, request.user, action, reason=reason):
            messages.success(request, f"Entry marked {action}.")
        else:
            messages.error(request, "Couldn't change that entry.")
    return redirect("admin_audit:raffle_ledger", pk=pk)


# --------------------------------------------------------------------------- #
#  Ineligible activity report
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_OFFICER)
def raffle_ineligible(request, pk):
    contest = _contest(pk)
    qs = RaffleIneligibleActivity.objects.filter(contest=contest).order_by("-detected_at")
    if request.GET.get("export") == "csv":
        return _csv_response(
            f"raffle-{contest.slug}-ineligible.csv",
            ["detected", "character_id", "character", "source", "ref", "reason",
             "would_be_tickets", "later_enrolled"],
            ([r.detected_at.isoformat(), r.character_id, r.character_name, r.source_key,
              r.source_ref, r.reason, r.would_be_tickets, r.later_enrolled]
             for r in qs.iterator(chunk_size=1000)),
        )
    by_char = list(
        qs.values("character_id", "character_name", "reason")
        .annotate(events=Count("id"), tickets=Sum("would_be_tickets"))
        .order_by("-tickets")[:200]
    )
    by_reason = {r["reason"]: r["c"] for r in qs.order_by().values("reason").annotate(c=Count("id"))}
    return render(request, "admin_audit/console/raffle_ineligible.html", {
        "contest": contest, "by_char": by_char, "by_reason": by_reason,
        "total": qs.count(), "adoption": stats.adoption_metrics(contest),
        "outreach_sent": contest.enrolment_outreach.count(),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def raffle_send_outreach(request, pk):
    """RAF-3 (3.9): nudge the ranked active-but-unenrolled pilots to enrol (one-click)."""
    contest = _contest(pk)
    result = services.send_enrolment_outreach(contest, actor=request.user)
    capped = " Only the top 200 pilots by tickets were considered — run again for more." \
        if result.get("capped") else ""
    if result.get("reason") == "event_disabled":
        messages.error(
            request, "Enrolment outreach is turned off in the notification console.")
    elif result["sent"]:
        messages.success(
            request,
            f"Sent {result['sent']} enrolment nudge(s); skipped {result['skipped']} "
            f"(already nudged, opted out, now enrolled, or no linked account).{capped}")
    else:
        messages.info(
            request, f"No new pilots to nudge (skipped {result['skipped']}).{capped}")
    return redirect("admin_audit:raffle_ineligible", pk=pk)


# --------------------------------------------------------------------------- #
#  Draw manager
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_OFFICER)
def raffle_draw(request, pk):
    contest = _contest(pk)
    committed = contest.draws.filter(status=RaffleDraw.Status.COMMITTED).order_by("-created_at").first()
    completed = contest.draws.filter(status=RaffleDraw.Status.COMPLETED, superseded_by__isnull=True).first()
    approved_tickets = RaffleTicketLedgerEntry.objects.filter(
        contest=contest, status=RaffleTicketLedgerEntry.Status.APPROVED, amount__gt=0
    ).aggregate(n=Sum("amount"))["n"] or 0
    readiness = {
        "closed": contest.status in (RaffleContest.Status.CLOSED, RaffleContest.Status.COMPLETED),
        "has_prizes": contest.prizes.exists(),
        "has_tickets": approved_tickets > 0,
        "draw_time_passed": contest.draw_at <= timezone.now(),
        "open_flags": contest.suspicious_flags.filter(status=RaffleSuspiciousActivityFlag.Status.OPEN).count(),
    }
    verification = verify_draw(completed) if completed else None
    activity = boosters.min_activity_status(contest)
    booster = boosters.prize_booster_status(contest)
    prize_preview = [
        {"prize": p,
         "effective": boosters.effective_prize_value(p, contest, achieved=booster["achieved"])}
        for p in contest.prizes.order_by("rank")
    ]
    return render(request, "admin_audit/console/raffle_draw.html", {
        "contest": contest, "committed": committed, "completed": completed,
        "approved_tickets": approved_tickets, "readiness": readiness,
        "verification": verification, "activity": activity, "booster": booster,
        "prize_preview": prize_preview,
        "results": list(completed.results.select_related("prize").order_by("draw_order")) if completed else [],
        "is_director": rbac.has_role(request.user, rbac.ROLE_DIRECTOR),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def raffle_draw_action(request, pk):
    contest = _contest(pk)
    action = request.POST.get("action")
    entropy = request.POST.get("external_entropy", "").strip()
    director = rbac.has_role(request.user, rbac.ROLE_DIRECTOR)

    if action == "close":
        if services.set_status(contest, RaffleContest.Status.CLOSED, request.user, reason="freeze for draw"):
            messages.success(request, "Contest closed — the ledger is frozen.")
        else:
            messages.error(request, "Can't close from the current state.")
    elif action == "prepare":
        draw = services.prepare_draw(contest, request.user, external_entropy=entropy)
        messages.success(request, f"Seed committed: {draw.seed_commitment[:16]}… Ready to draw.")
    elif action == "execute":
        if not director:
            messages.error(request, "Only a Director can execute the draw.")
        else:
            force = request.POST.get("override") == "1"  # the "draw anyway" button
            try:
                draw = services.run_draw(contest, request.user, external_entropy=entropy, force=force)
                if draw is None:
                    messages.info(request, "A draw is already running.")
                elif force and draw.forced_below_minimum:
                    messages.success(request, f"Drawn by override (below minimum activity) — "
                                              f"{draw.results.count()} winners.")
                else:
                    messages.success(request, f"Draw complete — {draw.results.count()} winners.")
            except services.ActivityNotMet as e:
                messages.error(request, str(e))
            except services.GrantBlocked as e:
                messages.error(request, str(e))
    elif action == "redraw":
        if not director:
            messages.error(request, "Only a Director can redraw.")
        else:
            draw = services.redraw(contest, request.user,
                                   reason=request.POST.get("reason", "manual redraw"),
                                   external_entropy=entropy)
            messages.success(request, f"Redraw complete — {draw.results.count()} winners.")
    return redirect("admin_audit:raffle_draw", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def raffle_fulfil(request, pk, result_id):
    contest = _contest(pk)
    draw = contest.draws.filter(status=RaffleDraw.Status.COMPLETED, superseded_by__isnull=True).first()
    result = get_object_or_404(RaffleDrawResult, pk=result_id, draw=draw)
    services.set_fulfilment(result, request.user, status=request.POST.get("status", "pending"),
                            notes=request.POST.get("notes", ""))
    messages.success(request, "Fulfilment updated.")
    return redirect("admin_audit:raffle_draw", pk=pk)


# --------------------------------------------------------------------------- #
#  Statistics
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_OFFICER)
def raffle_stats(request, pk):
    contest = _contest(pk)
    stat = stats.contest_statistics(contest, use_cache=False)
    if request.GET.get("export") == "json":
        import json
        return HttpResponse(json.dumps(stat, indent=2, default=str),
                            content_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="raffle-{contest.slug}-stats.json"'})
    return render(request, "admin_audit/console/raffle_stats.html", {
        "contest": contest, "stats": stat, "adoption": stat["adoption"],
        "recommendations": stats.recommendations(contest),
    })


# --------------------------------------------------------------------------- #
#  Exclusions + suspicious flags
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def raffle_exclude(request, pk):
    contest = _contest(pk)
    try:
        services.exclude_pilot(
            contest, request.user, character_id=request.POST.get("character_id") or None,
            character_name=request.POST.get("character_name", ""),
            reason=request.POST.get("reason", ""),
        )
        messages.success(request, "Pilot excluded from the contest.")
    except ValidationError as e:
        messages.error(request, "; ".join(e.messages))
    return redirect("admin_audit:raffle_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def raffle_exclude_remove(request, pk, exclusion_id):
    contest = _contest(pk)
    excl = get_object_or_404(RaffleExclusion, pk=exclusion_id, contest=contest)
    services.remove_exclusion(excl, request.user)
    messages.success(request, "Exclusion lifted.")
    return redirect("admin_audit:raffle_detail", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
def raffle_flags(request, pk):
    contest = _contest(pk)
    flags = list(contest.suspicious_flags.select_related("ledger_entry").order_by("status", "-created_at"))
    return render(request, "admin_audit/console/raffle_flags.html", {"contest": contest, "flags": flags})


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def raffle_flag_resolve(request, pk, flag_id):
    contest = _contest(pk)
    flag = get_object_or_404(RaffleSuspiciousActivityFlag, pk=flag_id, contest=contest)
    integrity.resolve_flag(flag, request.user, uphold=request.POST.get("action") == "uphold",
                           resolution=request.POST.get("resolution", ""))
    messages.success(request, "Flag resolved.")
    return redirect("admin_audit:raffle_flags", pk=pk)


# --------------------------------------------------------------------------- #
#  Preview / simulator
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_OFFICER)
def raffle_preview(request, pk):
    contest = _contest(pk)
    source_key = request.GET.get("source", "pvp")
    lookback = int(request.GET.get("days", 30))
    preview = engine.preview_source(contest, source_key, lookback_days=lookback)
    return render(request, "admin_audit/console/raffle_preview.html", {
        "contest": contest, "preview": preview, "source_key": source_key,
        "lookback": lookback, "sources": [s for s in all_sources() if not s.manual_only],
    })


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
def _csv_response(filename, header, rows):
    # Rows carry attacker-influenced text (character names, free-text reasons), and the
    # export is opened in a director's spreadsheet — neutralise formula injection.
    from core.exporting import csv_safe_row

    resp = HttpResponse(content_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    writer = csv.writer(resp)
    writer.writerow(header)
    for row in rows:
        writer.writerow(csv_safe_row(row))
    return resp
