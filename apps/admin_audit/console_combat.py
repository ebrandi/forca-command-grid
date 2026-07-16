"""Admin Console: Combat Rank ladder configuration + reward management.

Rank-ladder config and the reward engine's money settings are Director-gated
(ISK-adjacent, corp-wide); day-to-day reward-event triage (approve / reject / mark
paid) is Officer-gated, mirroring the raffle/SRP split. Every mutation writes an
immutable AuditLog row. The reward engine itself never moves ISK — it only creates
pending events a human approves and marks paid.
"""
from __future__ import annotations

import csv
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.killboard import ranks, rewards
from apps.killboard.models import (
    CombatRankTitle,
    NewbroConfig,
    RankMetric,
    RankRewardEvent,
    RankRewardSettings,
    RewardType,
)
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

_INPUT = {"class": "input-field"}


# --------------------------------------------------------------------------- #
#  Forms
# --------------------------------------------------------------------------- #
class CombatRankForm(forms.ModelForm):
    class Meta:
        model = CombatRankTitle
        fields = [
            "name", "metric", "min_kills", "description", "badge_icon", "color_class",
            "sort_order", "is_active", "is_visible", "grants_reward", "reward_type",
            "reward_amount", "reward_item_type_id", "reward_notes",
        ]
        widgets = {
            "name": forms.TextInput(attrs=_INPUT),
            "metric": forms.Select(attrs=_INPUT),
            "min_kills": forms.NumberInput(attrs={**_INPUT, "min": 0}),
            "description": forms.TextInput(attrs=_INPUT),
            "badge_icon": forms.TextInput(attrs=_INPUT),
            "color_class": forms.TextInput(attrs=_INPUT),
            "sort_order": forms.NumberInput(attrs={**_INPUT, "min": 0}),
            "reward_type": forms.Select(attrs=_INPUT),
            "reward_amount": forms.NumberInput(attrs={**_INPUT, "min": 0, "step": "any"}),
            "reward_item_type_id": forms.NumberInput(attrs={**_INPUT, "min": 0}),
            "reward_notes": forms.TextInput(attrs=_INPUT),
        }

    def clean_name(self) -> str:
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError(_("A rank title can't be empty."))
        return name

    def clean_reward_amount(self):
        amt = self.cleaned_data.get("reward_amount") or Decimal("0")
        if amt < 0:
            raise forms.ValidationError(_("Reward amount can't be negative."))
        return amt

    def clean(self):
        cleaned = super().clean()
        metric = cleaned.get("metric")
        min_kills = cleaned.get("min_kills")
        # Thresholds must be unique per metric (the ladder is ascending by threshold).
        if metric is not None and min_kills is not None:
            clash = CombatRankTitle.objects.filter(metric=metric, min_kills=min_kills)
            if self.instance and self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                self.add_error("min_kills", _("Another rank already uses this threshold for this metric."))
        # A reward-granting rank must have a valid reward configured.
        if cleaned.get("grants_reward"):
            # Rewards are kills-only by design — the engine scans metric=KILLS. Reject a
            # reward on a support-role track so a director never arms an inert reward (4.3).
            if metric is not None and metric != RankMetric.KILLS:
                self.add_error(
                    "grants_reward",
                    _("Rewards apply to the all-time PvP kills track only — "
                      "the support-role tracks are recognition-only."),
                )
            rtype = cleaned.get("reward_type")
            if rtype in (None, RewardType.NONE):
                self.add_error("reward_type", _("Choose a reward type, or untick “grants reward”."))
            elif rtype == RewardType.ITEM and not cleaned.get("reward_item_type_id"):
                self.add_error("reward_item_type_id", _("An item reward needs an EVE type id."))
            elif rtype in (RewardType.ISK, RewardType.PLEX) and not (cleaned.get("reward_amount") or 0) > 0:
                self.add_error("reward_amount", _("An ISK/PLEX reward needs a positive amount."))
        return cleaned


class RankRewardSettingsForm(forms.ModelForm):
    class Meta:
        model = RankRewardSettings
        fields = [
            "monthly_budget", "max_income_pct", "monthly_cap", "payout_currency",
            "plex_isk_rate", "default_strategy",
        ]
        widgets = {
            "monthly_budget": forms.NumberInput(attrs={**_INPUT, "min": 0, "step": "any"}),
            "max_income_pct": forms.NumberInput(attrs={**_INPUT, "min": 0, "max": 100, "step": "any"}),
            "monthly_cap": forms.NumberInput(attrs={**_INPUT, "min": 0, "step": "any"}),
            "payout_currency": forms.Select(attrs=_INPUT),
            "plex_isk_rate": forms.NumberInput(attrs={**_INPUT, "min": 0, "step": "any"}),
            "default_strategy": forms.Select(attrs=_INPUT),
        }

    def clean(self):
        cleaned = super().clean()
        for f in ("monthly_budget", "max_income_pct", "monthly_cap", "plex_isk_rate"):
            v = cleaned.get(f)
            if v is not None and v < 0:
                self.add_error(f, _("Must be zero or positive."))
        if (cleaned.get("max_income_pct") or 0) > 100:
            self.add_error("max_income_pct", _("Can't exceed 100%."))
        return cleaned


class NewbroConfigForm(forms.ModelForm):
    class Meta:
        model = NewbroConfig
        fields = ["soften_danger_label", "soften_below_events"]
        widgets = {
            "soften_below_events": forms.NumberInput(attrs={**_INPUT, "min": 0, "step": 1}),
        }


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def newbro_settings(request: HttpRequest) -> HttpResponse:
    """Save the newbro danger-label softening settings (KB-4)."""
    from django.core.cache import cache

    form = NewbroConfigForm(request.POST, instance=NewbroConfig.load())
    if form.is_valid():
        form.save()
        cache.delete("killboard:newbro_soften")  # take effect immediately, not after the TTL
        audit_log(request.user, "combat.newbro.settings", target_type="newbro_config",
                  target_id="1", ip=client_ip(request))
        messages.success(request, _("Newbro settings saved."))
    else:
        messages.error(request, _("Please correct the newbro settings."))
    return redirect("admin_audit:combat_reward_settings")


# --------------------------------------------------------------------------- #
#  Rank ladder configuration (Director)
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_DIRECTOR)
def combat_ranks(request: HttpRequest) -> HttpResponse:
    """The rank ladder: view/reorder, pilots-at-rank, projected rank-ups, reward
    suggestions and estimated liability, plus a dashboard preview."""
    all_ranks = list(CombatRankTitle.objects.order_by("min_kills", "sort_order"))
    admin_rows = {r["min_kills"]: r for r in rewards.rank_admin_rows()}
    suggestions = rewards.reward_suggestions()
    std = suggestions["strategies"]["standard"]["amounts"]
    con = suggestions["strategies"]["conservative"]["amounts"]
    agg = suggestions["strategies"]["aggressive"]["amounts"]

    rows = []
    for r in all_ranks:
        a = admin_rows.get(r.min_kills, {})
        rows.append({
            "rank": r,
            "pilots_now": a.get("pilots_now", 0),
            "proj_30": a.get("proj_30", 0), "proj_90": a.get("proj_90", 0), "proj_180": a.get("proj_180", 0),
            "suggest_conservative": con.get(r.min_kills, Decimal("0")),
            "suggest_standard": std.get(r.min_kills, Decimal("0")),
            "suggest_aggressive": agg.get(r.min_kills, Decimal("0")),
        })

    settings = RankRewardSettings.load()
    ctx = {
        "rows": rows,
        "form": CombatRankForm(initial={"sort_order": len(all_ranks), "color_class": "text-faint",
                                        "metric": RankMetric.KILLS}),
        "suggestions": suggestions,
        "settings": settings,
        "ladder_preview": ranks.active_ladder(),
        "estimated_liability": rewards.estimated_monthly_liability(settings),
        "pending_liability": rewards.pending_liability(),
        "rank_count": len(all_ranks),
        "active_count": sum(1 for r in all_ranks if r.is_active),
    }
    return render(request, "admin_audit/console/combat_ranks.html", ctx)


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def combat_rank_edit(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    """Create or edit a single rank rung (full-page form)."""
    instance = get_object_or_404(CombatRankTitle, pk=pk) if pk else None
    creating = instance is None
    if request.method == "POST":
        form = CombatRankForm(request.POST, instance=instance)
        if form.is_valid():
            rank = form.save(commit=False)
            if creating:
                rank.created_by = request.user
            rank.updated_by = request.user
            rank.save()
            ranks.invalidate_ladder_cache()
            audit_log(request.user, "combat.rank.save", target_type="combat_rank",
                      target_id=str(rank.pk), ip=client_ip(request),
                      metadata={"name": rank.name, "min_kills": rank.min_kills,
                                "grants_reward": rank.grants_reward})
            messages.success(request, _("Rank “%(name)s” saved.") % {"name": rank.name})
            return redirect("admin_audit:combat_ranks")
        messages.error(request, _("Please correct the errors below."))
    else:
        form = CombatRankForm(instance=instance)

    pilots_now = 0
    if instance:
        pilots_now = rewards.pilots_at_each_rank().get(instance.min_kills, 0)
    return render(request, "admin_audit/console/combat_rank_form.html", {
        "form": form, "creating": creating, "rank": instance, "pilots_now": pilots_now,
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def combat_rank_save(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    """Inline create/save from the ladder page (delegates to the same logic)."""
    return combat_rank_edit(request, pk)


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def combat_rank_delete(request: HttpRequest, pk: int) -> HttpResponse:
    rank = get_object_or_404(CombatRankTitle, pk=pk)
    name = rank.name
    rank.delete()
    ranks.invalidate_ladder_cache()
    audit_log(request.user, "combat.rank.delete", target_type="combat_rank",
              target_id=str(pk), ip=client_ip(request), metadata={"name": name})
    messages.success(request, _("Rank “%(name)s” removed.") % {"name": name})
    return redirect("admin_audit:combat_ranks")


# --------------------------------------------------------------------------- #
#  Reward settings + engine controls (Director)
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_DIRECTOR)
def combat_reward_settings(request: HttpRequest) -> HttpResponse:
    settings = RankRewardSettings.load()
    if request.method == "POST":
        form = RankRewardSettingsForm(request.POST, instance=settings)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.updated_by = request.user
            obj.save()
            audit_log(request.user, "combat.reward.settings", target_type="rank_reward_settings",
                      target_id=str(obj.pk), ip=client_ip(request))
            messages.success(request, _("Reward settings saved."))
            return redirect("admin_audit:combat_reward_settings")
        messages.error(request, _("Please correct the errors below."))
    else:
        form = RankRewardSettingsForm(instance=settings)

    reward_ranks = CombatRankTitle.objects.filter(
        is_active=True, metric=RankMetric.KILLS, grants_reward=True
    ).exclude(reward_type=RewardType.NONE).count()
    return render(request, "admin_audit/console/combat_settings.html", {
        "form": form, "settings": settings,
        "pool_info": rewards.reward_pool_isk(settings),
        "estimated_liability": rewards.estimated_monthly_liability(settings),
        "reward_rank_count": reward_ranks,
        "baseline_count": rewards.pilots_at_each_rank(),
        "newbro_form": NewbroConfigForm(instance=NewbroConfig.load()),
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def combat_reward_enable(request: HttpRequest) -> HttpResponse:
    """Enable rewards + snapshot the future-only baseline (the explicit confirm step)."""
    n = rewards.establish_baseline(actor=request.user)
    audit_log(request.user, "combat.reward.enable", target_type="rank_reward_settings",
              target_id="1", ip=client_ip(request), metadata={"baselined": n})
    messages.success(
        request,
        _("Rewards enabled. Baseline snapshotted for %(n)d pilot(s) — only ranks reached "
          "from now on will create rewards.") % {"n": n},
    )
    return redirect("admin_audit:combat_reward_settings")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def combat_reward_disable(request: HttpRequest) -> HttpResponse:
    rewards.disable_rewards(actor=request.user)
    audit_log(request.user, "combat.reward.disable", target_type="rank_reward_settings",
              target_id="1", ip=client_ip(request))
    messages.success(request, _("Rewards disabled. No new reward events will be created."))
    return redirect("admin_audit:combat_reward_settings")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def combat_reward_rebaseline(request: HttpRequest) -> HttpResponse:
    """Re-snapshot the baseline to now (used after big ladder edits — never backfills)."""
    n = rewards.establish_baseline(actor=request.user)
    audit_log(request.user, "combat.reward.baseline", target_type="rank_reward_settings",
              target_id="1", ip=client_ip(request), metadata={"baselined": n})
    messages.success(request, _("Baseline re-snapshotted for %(n)d pilot(s).") % {"n": n})
    return redirect("admin_audit:combat_reward_settings")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def combat_reward_scan(request: HttpRequest) -> HttpResponse:
    """Run the reward scan now (also runs every 30 min on the beat)."""
    n = rewards.scan_and_award(actor=request.user)
    audit_log(request.user, "combat.reward.scan", target_type="rank_reward",
              target_id="", ip=client_ip(request), metadata={"created": n})
    messages.success(request, _("Reward scan complete — %(n)d new reward event(s) created.") % {"n": n})
    return redirect("admin_audit:combat_rewards")


# --------------------------------------------------------------------------- #
#  Reward-event management (Officer)
# --------------------------------------------------------------------------- #
_ACTIONS = {"approve", "reject", "paid", "cancel"}

# Cap CSV exports so a filtered set can't buffer an unbounded number of rows into one in-memory
# HttpResponse. Matches the audit-log export's deliberate 5000-row cap (apps/admin_audit/views.py).
_CSV_EXPORT_MAX = 5000


@login_required
@role_required(rbac.ROLE_OFFICER)
def combat_rewards(request: HttpRequest) -> HttpResponse:
    """Pending reward events + filters + liability totals. CSV export on ?export=csv."""
    qs = RankRewardEvent.objects.all().order_by("-created_at")
    status = request.GET.get("status", "").strip()
    pilot = request.GET.get("pilot", "").strip()
    rtype = request.GET.get("reward_type", "").strip()
    if status in dict(RankRewardEvent.Status.choices):
        qs = qs.filter(status=status)
    if rtype in dict(RewardType.choices):
        qs = qs.filter(reward_type=rtype)
    if pilot:
        q = Q(character_name__icontains=pilot)
        if pilot.isdigit():
            q |= Q(character_id=int(pilot))
        qs = qs.filter(q)

    if request.GET.get("export") == "csv":
        return _export_csv(qs)

    now = timezone.now()
    paid = RankRewardEvent.objects.filter(status=RankRewardEvent.Status.PAID)
    rate = rewards.plex_isk_rate()

    def _isk_sum(events):
        return sum(
            (rewards.reward_isk_value(e["reward_type"], e["reward_amount"], rate=rate)
             for e in events.values("reward_type", "reward_amount")),
            Decimal("0"),
        )

    ctx = {
        "events": qs[:300],
        "total": qs.count(),
        "status": status, "pilot": pilot, "reward_type": rtype,
        "status_choices": RankRewardEvent.Status.choices,
        "reward_type_choices": RewardType.choices,
        "pending_liability": rewards.pending_liability(),
        "paid_this_month": _isk_sum(paid.filter(paid_at__year=now.year, paid_at__month=now.month)),
        "paid_this_year": _isk_sum(paid.filter(paid_at__year=now.year)),
        "pending_count": RankRewardEvent.objects.filter(status=RankRewardEvent.Status.PENDING).count(),
        "settings": RankRewardSettings.load(),
    }
    return render(request, "admin_audit/console/combat_rewards.html", ctx)


def _export_csv(qs) -> HttpResponse:
    # character_name is attacker-influenced and this opens in a director's spreadsheet,
    # so neutralise formula injection on every cell.
    from core.exporting import csv_safe_row

    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="combat-rank-rewards.csv"'
    w = csv.writer(resp)
    w.writerow(["character_id", "character", "rank", "kills_at_award", "achieved_at",
                "reward_type", "reward_amount", "status", "approved_at", "paid_at",
                "payment_reference"])
    for e in qs[:_CSV_EXPORT_MAX]:
        w.writerow(csv_safe_row([
            e.character_id, e.character_name, e.rank_name, e.kills_at_award,
            e.achieved_at.isoformat() if e.achieved_at else "", e.reward_type,
            e.reward_amount, e.status,
            e.approved_at.isoformat() if e.approved_at else "",
            e.paid_at.isoformat() if e.paid_at else "", e.payment_reference]))
    return resp


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def combat_reward_action(request: HttpRequest, pk: int) -> HttpResponse:
    event = get_object_or_404(RankRewardEvent, pk=pk)
    action = request.POST.get("action", "")
    if action not in _ACTIONS:
        raise PermissionDenied(_("Unknown action."))
    # Marking paid is the ISK-moving confirmation → Director only.
    if action == "paid" and not rbac.has_role(request.user, rbac.ROLE_DIRECTOR):
        raise PermissionDenied(_("Only a Director can mark a reward paid."))
    reason = request.POST.get("reason", "").strip()
    reference = request.POST.get("reference", "").strip()
    try:
        if action == "approve":
            rewards.approve(event, request.user)
        elif action == "reject":
            rewards.reject(event, request.user, reason=reason)
        elif action == "cancel":
            rewards.cancel(event, request.user, reason=reason)
        elif action == "paid":
            rewards.mark_paid(event, request.user, reference=reference)
    except rewards.InvalidTransition as exc:
        messages.error(request, str(exc))
        return redirect("admin_audit:combat_rewards")
    audit_log(request.user, f"combat.reward.{action}", target_type="rank_reward",
              target_id=str(event.pk), ip=client_ip(request),
              metadata={"character_id": event.character_id, "rank": event.rank_name,
                        "reference": reference, "reason": reason})
    messages.success(request, _("Reward %(action)s — %(character)s · %(rank)s.") % {
        "action": action, "character": event.character_name, "rank": event.rank_name})
    return redirect("admin_audit:combat_rewards")
