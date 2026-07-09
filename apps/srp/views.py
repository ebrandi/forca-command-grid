"""SRP views: a pilot's eligible losses & claims, the manager queue, and the
leadership programme settings (payout policy + rules)."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.killboard.models import Killmail
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from . import services
from .forms import SrpProgramForm, SrpRuleForm
from .models import SrpBudget, SrpClaim, SrpRule


def _parse_isk(raw: str | None) -> Decimal | None:
    """Parse an officer-entered ISK amount; None when blank/invalid."""
    if not raw or not raw.strip():
        return None
    try:
        value = Decimal(raw.strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return value if value >= 0 else None


@login_required
@role_required(rbac.ROLE_MEMBER)
def my_srp(request: HttpRequest) -> HttpResponse:
    from apps.killboard.models import FitDeviation

    char_ids = list(request.user.characters.values_list("character_id", flat=True))
    # The pilot's own recent doctrine losses that deviated from the canonical fit
    # ("you keep dying with the wrong rigs"). Only their own — never peers'.
    my_deviations = [
        dev for dev in FitDeviation.objects.select_related("killmail", "doctrine_fit")
        .filter(killmail__victim_character_id__in=char_ids)
        .order_by("-killmail__killmail_time")[:25]
        if dev.missing
    ][:6]
    return render(
        request,
        "srp/my_srp.html",
        {
            "program": services.active_program(),
            "eligible": services.eligible_losses_for(char_ids),
            "claims": SrpClaim.objects.filter(claimant=request.user).select_related("killmail"),
            "is_officer": rbac.has_role(request.user, rbac.ROLE_OFFICER),
            "my_deviations": my_deviations,
        },
    )


@login_required
@role_required(rbac.ROLE_MEMBER)
@require_POST
def submit_claim(request: HttpRequest) -> HttpResponse:
    char_ids = list(request.user.characters.values_list("character_id", flat=True))
    killmail = get_object_or_404(Killmail, pk=request.POST.get("killmail_id"))
    claim = services.submit_claim(request.user, killmail, char_ids)
    if claim:
        messages.success(request, f"SRP claim submitted: {claim.computed_payout:,.0f} ISK pending review.")
    else:
        messages.error(request, "That loss isn't eligible, or a claim already exists.")
    return redirect("srp:mine")


@login_required
@role_required(rbac.ROLE_OFFICER)
def queue(request: HttpRequest) -> HttpResponse:
    from django.core.paginator import Paginator

    pending_qs = SrpClaim.objects.filter(status=SrpClaim.Status.SUBMITTED).select_related(
        "killmail", "claimant", "doctrine"
    ).order_by("-created_at")
    approved_qs = SrpClaim.objects.filter(status=SrpClaim.Status.APPROVED).select_related(
        "killmail", "claimant"
    ).order_by("-decided_at")
    recent = SrpClaim.objects.filter(
        status__in=[SrpClaim.Status.DENIED, SrpClaim.Status.PAID]
    ).select_related("killmail", "claimant").order_by("-decided_at")[:20]
    return render(
        request,
        "srp/queue.html",
        {
            "program": services.active_program(),
            # SRP-4 (3.17): paginate the two potentially-large actionable lists (a fleet wipe
            # produces dozens of claims); the batch actions still operate on the full set.
            "pending": Paginator(pending_qs, 50).get_page(request.GET.get("ppage")),
            "approved": Paginator(approved_qs, 50).get_page(request.GET.get("apage")),
            "pending_count": pending_qs.count(),
            "approved_count": approved_qs.count(),
            "recent": recent,
            "exposure": services.exposure(),
        },
    )


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def batch_approve(request: HttpRequest) -> HttpResponse:
    """SRP-4 (3.17): approve every submitted claim in one action (skips the officer's own)."""
    result = services.batch_approve(request.user)
    audit_log(request.user, "srp.batch_approve", target_type="srp_batch",
              metadata=result, ip=client_ip(request))
    if result["approved"]:
        msg = f"Approved {result['approved']} claim(s)."
        if result["skipped"]:
            msg += f" Skipped {result['skipped']} of your own — another officer must review those."
        messages.success(request, msg)
    else:
        messages.info(request, "Nothing to approve"
                      + (f" — {result['skipped']} were your own claims." if result["skipped"] else "."))
    return redirect("srp:queue")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def batch_pay(request: HttpRequest) -> HttpResponse:
    """SRP-4 (3.17): settle every approved claim against one reference (skips the officer's own)."""
    reference = (request.POST.get("payment_reference") or "").strip()[:200]
    result = services.batch_pay(request.user, reference=reference)
    audit_log(request.user, "srp.batch_pay", target_type="srp_batch",
              metadata={**result, "reference": reference}, ip=client_ip(request))
    if result["paid"]:
        msg = f"Settled {result['paid']} claim(s)"
        msg += f" against “{reference}”." if reference else "."
        if result["skipped"]:
            msg += f" Skipped {result['skipped']} of your own."
        messages.success(request, msg)
    else:
        messages.info(request, "No approved claims to settle"
                      + (f" — {result['skipped']} were your own." if result["skipped"] else "."))
    return redirect("srp:queue")


@login_required
@role_required(rbac.ROLE_OFFICER)
def budget(request: HttpRequest) -> HttpResponse:
    """SRP-6: budget vs spend vs open exposure (solvency). Spend is derived live
    from PAID claims; SrpBudget stores only the allocation."""
    now = timezone.now()
    # Last 6 month keys, newest first.
    periods = []
    y, m = now.year, now.month
    for _ in range(6):
        periods.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    budgets = {b.period: b for b in SrpBudget.objects.filter(period__in=periods)}
    rows = []
    for period in periods:
        allocated = budgets[period].allocated if period in budgets else Decimal("0")
        spent = services.spent_for_period(period)
        rows.append({
            "period": period, "allocated": allocated, "spent": spent,
            "remaining": allocated - spent,
        })
    open_exposure = services.exposure()
    current = rows[0]
    return render(request, "srp/budget.html", {
        "program": services.active_program(),
        "rows": rows,
        "current_period": current["period"],
        "current_allocated": current["allocated"],
        "current_spent": current["spent"],
        "open_exposure": open_exposure,
        # Can this month's allocation cover what's already paid plus every open claim?
        "solvency": current["allocated"] - current["spent"] - open_exposure,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def budget_save(request: HttpRequest) -> HttpResponse:
    """Officer sets/edits a period's SRP allocation (replaces the admin-only path)."""
    period = (request.POST.get("period") or "").strip()
    allocated = _parse_isk(request.POST.get("allocated"))
    valid_period = (
        len(period) == 7 and period[4] == "-"
        and period[:4].isdigit() and period[5:7].isdigit() and "01" <= period[5:7] <= "12"
    )
    if not valid_period or allocated is None:
        messages.error(request, "Enter a valid period (YYYY-MM) and a non-negative amount.")
        return redirect("srp:budget")
    SrpBudget.objects.update_or_create(period=period, defaults={"allocated": allocated})
    audit_log(
        request.user, "srp.budget_update", target_type="srp_budget", target_id=period,
        metadata={"allocated": str(allocated)}, ip=client_ip(request),
    )
    messages.success(request, f"Budget for {period} set to {allocated:,.0f} ISK.")
    return redirect("srp:budget")


@login_required
@role_required(rbac.ROLE_OFFICER)
def loss_impact(request: HttpRequest) -> HttpResponse:
    """SRP-7: corp-wide loss-impact board — losses by doctrine, the most commonly
    missing fittings, and pilots with repeated fitting mistakes. Officer-gated;
    individual pilot deviations stay on the pilot's own SRP page."""
    from apps.killboard.analytics import loss_impact_summary

    return render(request, "srp/loss_impact.html", {"summary": loss_impact_summary()})


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def decide(request: HttpRequest, pk: int) -> HttpResponse:
    claim = get_object_or_404(SrpClaim, pk=pk)
    approve = request.POST.get("decision") == "approve"
    reason = (request.POST.get("reason") or "").strip()
    # Optional officer override of the payout (approvals only).
    adjusted = _parse_isk(request.POST.get("approved_payout")) if approve else None
    try:
        changed = services.decide(claim, request.user, approve, reason, approved_payout=adjusted)
    except PermissionDenied:
        audit_log(
            request.user,
            "srp.decide.denied_self",
            target_type="srp_claim",
            target_id=str(claim.pk),
            ip=client_ip(request),
        )
        messages.error(request, "You can't decide your own SRP claim — another officer must review it.")
        return redirect("srp:queue")
    if not changed:
        messages.error(request, "That claim is no longer awaiting a decision.")
        return redirect("srp:queue")
    audit_log(
        request.user,
        "srp.decided",
        target_type="srp_claim",
        target_id=str(claim.pk),
        metadata={"approve": approve, "payout": str(claim.computed_payout)},
        ip=client_ip(request),
    )
    messages.success(request, f"Claim {'approved' if approve else 'denied'}.")
    return redirect("srp:queue")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def pay(request: HttpRequest, pk: int) -> HttpResponse:
    claim = get_object_or_404(SrpClaim, pk=pk)
    reference = (request.POST.get("payment_reference") or "").strip()[:200]
    try:
        paid = services.mark_paid(claim, request.user, reference=reference)
    except PermissionDenied:
        audit_log(
            request.user,
            "srp.pay.denied_self",
            target_type="srp_claim",
            target_id=str(claim.pk),
            ip=client_ip(request),
        )
        messages.error(request, "You can't pay out your own SRP claim — another officer must.")
        return redirect("srp:queue")
    if not paid:
        messages.error(request, "That claim isn't approved and awaiting payment.")
        return redirect("srp:queue")
    audit_log(
        request.user,
        "srp.paid",
        target_type="srp_claim",
        target_id=str(claim.pk),
        metadata={"payout": str(claim.payout), "reference": reference},
        ip=client_ip(request),
    )
    messages.success(request, "Marked paid.")
    return redirect("srp:queue")


@login_required
@role_required(rbac.ROLE_OFFICER)
def settings_view(request: HttpRequest) -> HttpResponse:
    """Leadership: tune the SRP programme (payout policy) and per-doctrine rules."""
    program = services.active_program()
    if request.method == "POST":
        was_auto_draft = program.auto_draft_enabled
        form = SrpProgramForm(request.POST, instance=program)
        if form.is_valid():
            saved = form.save(commit=False)
            # 4.6: stamp the future-only baseline the moment auto-draft is first armed, so it
            # never back-drafts historical losses. Cleared when disarmed so a later re-arm
            # sets a fresh baseline.
            if saved.auto_draft_enabled and not was_auto_draft:
                from django.utils import timezone
                saved.auto_draft_since = timezone.now()
            elif not saved.auto_draft_enabled:
                saved.auto_draft_since = None
            saved.save()
            audit_log(request.user, "srp.program_update", target_type="srp_program",
                      target_id=str(program.pk), ip=client_ip(request))
            messages.success(request, "SRP programme updated.")
            return redirect("srp:settings")
    else:
        form = SrpProgramForm(instance=program)
    return render(
        request,
        "srp/settings.html",
        {
            "form": form,
            "program": program,
            "rules": SrpRule.objects.select_related("doctrine").all(),
            "rule_form": SrpRuleForm(),
        },
    )


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def rule_add(request: HttpRequest) -> HttpResponse:
    form = SrpRuleForm(request.POST)
    if form.is_valid():
        rule = form.save()
        audit_log(request.user, "srp.rule_add", target_type="srp_rule",
                  target_id=str(rule.pk), ip=client_ip(request))
        messages.success(request, "SRP rule added.")
    else:
        messages.error(request, "Couldn't add that rule — check the values.")
    return redirect("srp:settings")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def rule_delete(request: HttpRequest, pk: int) -> HttpResponse:
    rule = get_object_or_404(SrpRule, pk=pk)
    rule.delete()
    audit_log(request.user, "srp.rule_delete", target_type="srp_rule",
              target_id=str(pk), ip=client_ip(request))
    messages.success(request, "SRP rule removed.")
    return redirect("srp:settings")
