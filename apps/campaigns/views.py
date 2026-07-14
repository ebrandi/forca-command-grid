"""Campaign Command web views (Phase 1 — design doc 10).

Thin request handlers: parse input, delegate every state change to :mod:`.services`, render with
the house design-system primitives. The security spine (doc 07) is uniform here — every view is
``@login_required`` and feature-gated by the ``campaigns`` namespace; object routes resolve the
campaign then re-check ``services.can_view`` (``Http404`` on failure, the no-existence-oracle
rule); subresources are fetched *through* their campaign FK; mutations are POST-only and
re-derive authority server-side from the object (``can_manage`` / ``can_update_objective`` /
``can_approve``) rather than trusting a hidden form field, raising ``PermissionDenied`` (403) when
a *visible* campaign forbids the specific action. Sensitive objective values and budget figures
are stripped in the service/view-model before the template is ever handed them.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.freshness import humanize_as_of, is_stale

from . import config, metrics, notify, services
from .models import (
    Campaign,
    CampaignDependency,
    CampaignEvidence,
    CampaignTemplate,
    DependencyKind,
    EvidenceKind,
    Issue,
    MeasurementSource,
    Milestone,
    Objective,
    Risk,
    Workstream,
)

User = get_user_model()

# The definition fields the generic campaign edit handler will accept — the explicit allow-list
# that is the mass-assignment guard (doc 07 T4). Lifecycle status, health, progress, verification
# and closure columns are NEVER in here: they change only through their dedicated services.
_MAX_TAGS = 20
_MAX_TAG_LEN = 40

# The exact columns each generic edit handler may write back. An edit view loads the row at request
# start, so a plain ``save()`` would clobber every locked column (status, progress_pct, health,
# current_value, measured_at) an intervening service write set (doc 05 §5, #16). ``budget_isk`` is
# appended for the campaign form only when the actor may set it.
_CAMPAIGN_EDIT_FIELDS = [
    "name", "summary", "description", "rationale", "desired_outcome", "success_criteria",
    "failure_criteria", "category", "priority", "progress_mode", "recognition_mode",
    "recognition_public", "visibility", "commander", "sponsor", "start_at", "target_end_at",
    "staging_system_id", "staging_system_name", "tags", "updated_at",
]
_OBJECTIVE_EDIT_FIELDS = [
    "title", "description", "workstream", "owner", "weight", "due_at", "unit", "direction",
    "baseline_value", "target_value", "is_mandatory", "help_wanted", "requires_verification",
    "measurement_paused", "metric_source", "metric_params", "is_sensitive", "updated_at",
]


# --------------------------------------------------------------------------- #
#  Small shared helpers
# --------------------------------------------------------------------------- #
def _user_pool():
    """Users holding any role — the people who can command, sponsor, lead or own (doc 10 §6.10).
    Characters are prefetched because every user-facing surface renders ``display_name``
    (main-character name), which walks the character set."""
    from apps.identity.models import RoleAssignment

    ids = RoleAssignment.objects.values_list("user_id", flat=True).distinct()
    return User.objects.filter(pk__in=ids).prefetch_related("characters").order_by("username")


def _user_choices():
    """The pool as a list sorted by the pilot name a select actually shows."""
    return sorted(_user_pool(), key=lambda u: _user_label(u).lower())


def _user_label(user) -> str:
    return getattr(user, "display_name", "") or user.get_username()


def _staging_system_name(system_id: int | None) -> str:
    """Resolve the cached staging-system name from the SDE — the id (picked via the
    autocomplete) is the source of truth; the client never types or submits the name."""
    if system_id is None:
        return ""
    from apps.sde.models import SdeSolarSystem

    row = SdeSolarSystem.objects.filter(system_id=system_id).only("name").first()
    return row.name if row else ""


@login_required
def system_search(request: HttpRequest) -> JsonResponse:
    """Autocomplete for the staging-system picker: ``[{id, name, security}]``.

    SDE data is not sensitive; login (plus the campaigns feature gate on the
    namespace) keeps the endpoint off the anonymous surface."""
    q = (request.GET.get("q") or "").strip()
    rows = []
    if len(q) >= 2:
        from apps.sde.models import SdeSolarSystem

        rows = [
            {"id": s.system_id, "name": s.name, "security": round(s.security, 1)}
            for s in SdeSolarSystem.objects.filter(name__icontains=q).order_by("name")[:15]
        ]
    return JsonResponse(rows, safe=False)


@login_required
def type_search(request: HttpRequest) -> JsonResponse:
    """Autocomplete for the SDE item-type pickers (industry / stockpile params): ``[{type_id, name}]``.

    Mirrors :func:`system_search` — SDE data is not sensitive, login + the campaigns feature gate keep
    it off the anonymous surface. Returns ``type_id`` (not ``id``) so it drops into the shared
    ``typePicker`` Alpine factory unchanged."""
    q = (request.GET.get("q") or "").strip()
    rows = []
    if len(q) >= 2:
        from apps.sde.models import SdeType

        rows = [
            {"type_id": t.type_id, "name": t.name}
            for t in SdeType.objects.filter(name__icontains=q).order_by("name")[:15]
        ]
    return JsonResponse(rows, safe=False)


def _campaign_for_view(request, pk: int) -> Campaign:
    """Resolve a campaign and enforce the visibility chokepoint — 404 for anything the viewer may
    not see (no existence oracle, doc 07 §1.4).

    ``commander``/``sponsor``/``closed_by`` are join-loaded because the detail, report and portfolio
    surfaces all render their ``display_name`` (main-character name); the join avoids a per-page
    lazy FK load and the ``.display_name`` character walk stays a single query for the one row."""
    campaign = get_object_or_404(
        Campaign.objects.select_related("commander", "sponsor", "closed_by"), pk=pk
    )
    if not services.can_view(request.user, campaign):
        raise Http404(_("No such campaign."))
    return campaign


def _objective_for(request, pk: int) -> Objective:
    """Resolve an objective *through* its campaign and re-check visibility (subresource rule).

    ``owner``/``verified_by`` are join-loaded because the objective page renders their
    ``display_name``; the join keeps each a single character-walk query, not a lazy FK load first."""
    objective = get_object_or_404(
        Objective.objects.select_related("campaign", "workstream", "owner", "verified_by"), pk=pk
    )
    if not services.can_view(request.user, objective.campaign):
        raise Http404(_("No such objective."))
    return objective


def _back(request, default):
    """Redirect to the POSTed same-origin ``next`` (or Referer), else a safe default."""
    nxt = request.POST.get("next") or request.headers.get("Referer", "")
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(nxt)
    return redirect(default)


def _dt(value):
    """Parse a ``datetime-local`` field into an aware datetime, or ``None`` when blank."""
    value = (value or "").strip()
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValidationError(_("Enter a valid date and time."))
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _dt_local(value) -> str:
    """Format a stored datetime for a ``datetime-local`` input value."""
    if not value:
        return ""
    return timezone.localtime(value).strftime("%Y-%m-%dT%H:%M")


def _dec(value):
    """Parse a decimal field, or ``None`` when blank; raise on non-numeric input."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(_("Enter a valid number.")) from exc


def _int(value, default=None):
    value = (value or "").strip()
    if not value:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _pool_user(value):
    """A user id from the POST resolved against the role pool, or ``None`` (soft-ignore junk)."""
    uid = _int(value)
    if uid is None:
        return None
    return _user_pool().filter(pk=uid).first()


def _notify_assignment(campaign, kind, obj_id, new_owner, old_owner_id, actor, *, what="") -> None:
    """Fire ``campaigns.assigned`` when an owner/lead/commander is set or *changed* to a user
    (doc 09 §4). Skips a no-op re-save (unchanged) and never pings a pilot about assigning
    themselves. Fail-soft — emission lives in ``notify`` and swallows its own errors."""
    if new_owner is None or new_owner.pk == old_owner_id:
        return
    if new_owner.pk == getattr(actor, "pk", None):
        return
    notify.assigned(campaign, kind, obj_id, new_owner, what=what)


def _parse_tags(raw) -> list[str]:
    """Comma-separated tags → a bounded, de-duplicated JSON list (growth cap, doc 07 T20)."""
    seen: list[str] = []
    for part in (raw or "").split(","):
        tag = part.strip()[:_MAX_TAG_LEN]
        if tag and tag not in seen:
            seen.append(tag)
        if len(seen) >= _MAX_TAGS:
            break
    return seen


# --------------------------------------------------------------------------- #
#  Chip / view-model helpers
# --------------------------------------------------------------------------- #
def _objective_vm(objective, user) -> dict:
    """A render-safe view of one objective — sensitive measurements stripped (doc 07 §1.5).

    Carries the auto-source metadata the P3 UI needs: the source label, whether the value is auto,
    a paused flag, and an ``is_stale`` marker computed against the source's ``core.freshness``
    threshold (doc 10 §5, doc 04 §3.3) so the explain table and objective page can flag a lagging
    auto value without leaking anything sensitive.
    """
    value_visible = services.can_view_objective_value(user, objective)
    source = metrics.get_source(objective.metric_source) if objective.metric_source else None
    is_auto = bool(objective.metric_source)
    data_class = source.data_class if source else "default"
    return {
        "obj": objective,
        "value_visible": value_visible,
        "current": objective.current_value if value_visible else None,
        "baseline": objective.baseline_value if value_visible else None,
        "target": objective.target_value if value_visible else None,
        "progress_pct": objective.progress_pct,
        "freshness": humanize_as_of(objective.measured_at) if objective.measured_at else None,
        "manual": objective.measurement_source == MeasurementSource.MANUAL,
        "is_auto": is_auto,
        "source_label": source.label if source else "",
        "paused": objective.measurement_paused,
        "is_stale": is_auto and not objective.measurement_paused
        and is_stale(objective.measured_at, data_class),
    }


def _upcoming_milestones_prefetch() -> Prefetch:
    """Prefetch spec matching ``_next_milestone`` — the portfolio attaches it to each page row
    so the per-row lookup is a list index, not a query (perf budget, doc 12 §6)."""
    return Prefetch(
        "milestones",
        queryset=Milestone.objects.filter(due_at__isnull=False)
        .exclude(status__in=[Milestone.MilestoneStatus.DONE, Milestone.MilestoneStatus.MISSED])
        .order_by("due_at"),
        to_attr="upcoming_milestones",
    )


def _next_milestone(campaign):
    prefetched = getattr(campaign, "upcoming_milestones", None)
    if prefetched is not None:
        return prefetched[0] if prefetched else None
    return (
        campaign.milestones.filter(due_at__isnull=False)
        .exclude(status__in=[Milestone.MilestoneStatus.DONE, Milestone.MilestoneStatus.MISSED])
        .order_by("due_at")
        .first()
    )


def _worst_reason(campaign):
    """The single worst health reason (already appended worst-first by the evaluator)."""
    reasons = campaign.health_reasons or []
    return reasons[0] if reasons else None


# --------------------------------------------------------------------------- #
#  Portfolio (doc 10 §6.1)
# --------------------------------------------------------------------------- #
@login_required
def portfolio(request: HttpRequest) -> HttpResponse:
    """The corp strategic picture: summary strip (officer+), filter bar, paginated rows."""
    user = request.user
    base = services.visible_campaigns(user).select_related("commander")
    can_manage_campaigns = rbac.has_perm(user, rbac.PERM_CAMPAIGN_MANAGE)
    is_officer = rbac.has_role(user, rbac.ROLE_OFFICER)

    f_status = (request.GET.get("status") or "").strip()
    f_health = (request.GET.get("health") or "").strip()
    f_category = (request.GET.get("category") or "").strip()
    f_commander = (request.GET.get("commander") or "").strip()
    f_tag = (request.GET.get("tag") or "").strip()
    q = (request.GET.get("q") or "").strip()

    qs = base
    if f_status in Campaign.Status.values:
        qs = qs.filter(status=f_status)
    if f_health in Campaign.Health.values:
        qs = qs.filter(health=f_health)
    if f_category in Campaign.Category.values:
        qs = qs.filter(category=f_category)
    if _int(f_commander) is not None:
        qs = qs.filter(commander_id=_int(f_commander))
    if f_tag:
        qs = qs.filter(tags__contains=[f_tag])
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(summary__icontains=q))
    qs = qs.distinct().prefetch_related(
        _upcoming_milestones_prefetch(), "commander__characters"
    )

    page_obj = _page(request, qs, 25)
    now = timezone.now()
    soon = now + timezone.timedelta(days=7)
    rows = []
    for c in page_obj.object_list:
        milestone = _next_milestone(c)
        rows.append({
            "c": c,
            "next_milestone": milestone,
            "ms_overdue": bool(milestone and milestone.due_at and milestone.due_at < now),
            "ms_soon": bool(milestone and milestone.due_at and now <= milestone.due_at <= soon),
            "worst_reason": _worst_reason(c),
        })

    # Summary strip (officer+ only) — computed inline over the *visible* set, uncached (doc 10 §6.1).
    summary = None
    if is_officer or can_manage_campaigns:
        summary = {
            "active": base.filter(status=Campaign.Status.ACTIVE).count(),
            "at_risk": base.filter(health=Campaign.Health.AT_RISK).count(),
            "blocked": base.filter(health=Campaign.Health.BLOCKED).count(),
            "awaiting": base.filter(status=Campaign.Status.PROPOSED).count(),
        }

    commanders = sorted(
        _user_pool().filter(pk__in=base.values_list("commander_id", flat=True)).distinct(),
        key=lambda u: _user_label(u).lower(),
    )
    active_filters = any([f_status, f_health, f_category, f_commander, f_tag, q])
    ctx = {
        "rows": rows,
        "page_obj": page_obj,
        "base_qs": _base_qs(request),
        "summary": summary,
        "is_officer": is_officer,
        "can_manage_campaigns": can_manage_campaigns,
        "statuses": Campaign.Status.choices,
        "healths": Campaign.Health.choices,
        "categories": Campaign.Category.choices,
        "commanders": commanders,
        "f_status": f_status,
        "f_health": f_health,
        "f_category": f_category,
        "f_commander": f_commander,
        "f_tag": f_tag,
        "q": q,
        "active_filters": active_filters,
    }
    template = (
        "campaigns/_portfolio_results.html"
        if request.headers.get("HX-Request")
        else "campaigns/portfolio.html"
    )
    return render(request, template, ctx)


def _page(request, qs, per_page):
    return Paginator(qs, per_page).get_page(request.GET.get("page"))


def _base_qs(request) -> str:
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


# --------------------------------------------------------------------------- #
#  Campaign create / edit (doc 10 §6.10)
# --------------------------------------------------------------------------- #
def _apply_campaign_fields(campaign, request, *, budget_allowed) -> bool:
    """Copy the allow-listed definition fields from the POST onto ``campaign`` (raising
    ``ValidationError`` on bad input). Returns whether visibility changed (for the audit).
    Budget keys from an unauthorised user are silently dropped — the mass-assignment guard."""
    post = request.POST
    name = (post.get("name") or "").strip()
    if not name:
        raise ValidationError(_("A campaign needs a name."))

    old_visibility = campaign.visibility
    campaign.name = name[:120]
    campaign.summary = (post.get("summary") or "").strip()[:200]
    campaign.description = (post.get("description") or "").strip()
    campaign.rationale = (post.get("rationale") or "").strip()
    campaign.desired_outcome = (post.get("desired_outcome") or "").strip()
    campaign.success_criteria = (post.get("success_criteria") or "").strip()
    campaign.failure_criteria = (post.get("failure_criteria") or "").strip()

    category = (post.get("category") or "").strip()
    campaign.category = category if category in Campaign.Category.values else Campaign.Category.OTHER
    campaign.priority = _int(post.get("priority"), 0) or 0

    progress_mode = (post.get("progress_mode") or "").strip()
    if progress_mode in Campaign.ProgressMode.values:
        campaign.progress_mode = progress_mode

    recognition_mode = (post.get("recognition_mode") or "").strip()
    if recognition_mode in Campaign.RecognitionMode.values:
        campaign.recognition_mode = recognition_mode
    campaign.recognition_public = bool(post.get("recognition_public"))

    visibility = (post.get("visibility") or "").strip()
    if visibility in Campaign.Visibility.values:
        campaign.visibility = visibility

    campaign.commander = _pool_user(post.get("commander"))
    campaign.sponsor = _pool_user(post.get("sponsor"))
    campaign.start_at = _dt(post.get("start_at"))
    campaign.target_end_at = _dt(post.get("target_end_at"))
    if campaign.target_end_at and campaign.start_at and campaign.target_end_at < campaign.start_at:
        raise ValidationError(_("The target end date cannot be before the start date."))

    campaign.staging_system_id = _int(post.get("staging_system_id"))
    campaign.staging_system_name = _staging_system_name(campaign.staging_system_id)
    campaign.tags = _parse_tags(post.get("tags"))

    if budget_allowed:
        campaign.budget_isk = _dec(post.get("budget_isk"))

    return old_visibility != campaign.visibility


def _active_template(key):
    """An active template by key, or ``None`` (blank/unknown ⇒ start-blank)."""
    key = (key or "").strip()
    if not key:
        return None
    return CampaignTemplate.objects.filter(key=key, active=True).first()


@login_required
def campaign_create(request: HttpRequest) -> HttpResponse:
    """Create a new campaign — always as a draft (propose is a separate detail action).

    Optionally seeded from a template (doc 10 §6.10): a ``template_key`` (GET pre-fills the form
    from the blueprint and previews its children; POST instantiates the whole structure as one
    draft via :func:`services.instantiate_template`, with the name/dates taken from the form)."""
    if not rbac.has_perm(request.user, rbac.PERM_CAMPAIGN_MANAGE):
        raise PermissionDenied(_("You cannot create campaigns."))
    budget_allowed = rbac.has_role(request.user, rbac.ROLE_DIRECTOR)
    if request.method == "POST":
        template = _active_template(request.POST.get("template_key"))
        if template is not None:
            try:
                campaign = services.instantiate_template(
                    template, request.user,
                    name=(request.POST.get("name") or "").strip() or None,
                    start_at=_dt(request.POST.get("start_at")),
                    target_end_at=_dt(request.POST.get("target_end_at")),
                )
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
                return render(request, "campaigns/form.html",
                              _campaign_form_ctx(request, Campaign(), template=template))
            _notify_assignment(campaign, "campaign", campaign.pk, campaign.commander, None,
                               request.user, what="command of this campaign")
            messages.success(request, _("Campaign draft created from template — edit anything you like."))
            return redirect("campaigns:detail", pk=campaign.pk)

        campaign = _new_campaign(request.user)
        try:
            _apply_campaign_fields(campaign, request, budget_allowed=budget_allowed)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(request, "campaigns/form.html", _campaign_form_ctx(request, campaign))
        campaign.save()
        _sync_restricted_users(campaign, request)
        services.record_activity(
            campaign, request.user, "campaign.created", target_kind="campaign",
            target_id=campaign.pk, after={"status": campaign.status},
        )
        _notify_assignment(campaign, "campaign", campaign.pk, campaign.commander, None,
                           request.user, what="command of this campaign")
        messages.success(request, _("Campaign draft created."))
        return redirect("campaigns:detail", pk=campaign.pk)

    template = _active_template(request.GET.get("template"))
    if template is not None:
        campaign = _campaign_from_blueprint(template)
        return render(request, "campaigns/form.html",
                      _campaign_form_ctx(request, campaign, template=template))
    return render(request, "campaigns/form.html",
                  _campaign_form_ctx(request, _new_campaign(request.user)))


def _new_campaign(user) -> Campaign:
    """A fresh unsaved campaign carrying the config recognition defaults (doc 00 §5) so a manually
    created campaign gets them just like a templated one does (#45)."""
    recognition_cfg = config.get("recognition")
    return Campaign(
        created_by=user,
        recognition_mode=recognition_cfg.get("default_mode", "none"),
        recognition_public=bool(recognition_cfg.get("default_public", False)),
    )


def _campaign_from_blueprint(template) -> Campaign:
    """A transient (unsaved) campaign carrying the template's suggested prose for form pre-fill —
    people/dates/ids are never pre-filled (doc 04 §13)."""
    bp = template.blueprint or {}
    return Campaign(
        name=template.name, summary=(bp.get("summary") or "")[:200],
        rationale=bp.get("rationale") or "", desired_outcome=bp.get("desired_outcome") or "",
        success_criteria=bp.get("success_criteria") or "",
        failure_criteria=bp.get("failure_criteria") or "",
        category=template.category or Campaign.Category.OTHER,
    )


def _blueprint_counts(template) -> dict:
    """Child counts for a template card / preview (doc 10 §6.10)."""
    bp = template.blueprint or {}
    return {
        "objectives": len(bp.get("objectives", [])),
        "workstreams": len(bp.get("workstreams", [])),
        "milestones": len(bp.get("milestones", [])),
        "risks": len(bp.get("risks", [])),
    }


@login_required
def template_picker(request: HttpRequest) -> HttpResponse:
    """The start-from-template gallery (doc 10 §6.10): active templates as cards, filterable by
    category and search. ``PERM_CAMPAIGN_MANAGE`` — only campaign managers create."""
    if not rbac.has_perm(request.user, rbac.PERM_CAMPAIGN_MANAGE):
        raise PermissionDenied(_("You cannot create campaigns."))
    f_category = (request.GET.get("category") or "").strip()
    q = (request.GET.get("q") or "").strip()
    qs = CampaignTemplate.objects.filter(active=True)
    if f_category in Campaign.Category.values:
        qs = qs.filter(category=f_category)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
    cards = [
        {"template": t, "counts": _blueprint_counts(t)}
        for t in qs.order_by("-is_builtin", "name")
    ]
    ctx = {
        "cards": cards,
        "categories": Campaign.Category.choices,
        "f_category": f_category,
        "q": q,
    }
    return render(request, "campaigns/template_picker.html", ctx)


@login_required
def campaign_edit(request: HttpRequest, pk: int) -> HttpResponse:
    """Edit a campaign's definition (``can_manage``); a visibility change is audited (doc 07 T4)."""
    campaign = _campaign_for_view(request, pk)
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage this campaign."))
    budget_allowed = services.can_view_budget(request.user, campaign)
    if request.method == "POST":
        old_commander_id = campaign.commander_id
        try:
            visibility_changed = _apply_campaign_fields(campaign, request, budget_allowed=budget_allowed)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(request, "campaigns/form.html", _campaign_form_ctx(request, campaign))
        update_fields = list(_CAMPAIGN_EDIT_FIELDS)
        if budget_allowed:
            update_fields.append("budget_isk")
        campaign.save(update_fields=update_fields)
        _sync_restricted_users(campaign, request)
        services.record_activity(
            campaign, request.user, "campaign.edited", target_kind="campaign",
            target_id=campaign.pk,
        )
        _notify_assignment(campaign, "campaign", campaign.pk, campaign.commander, old_commander_id,
                           request.user, what="command of this campaign")
        if visibility_changed:
            audit_log(
                request.user, "campaigns.visibility_changed", target_type="campaign",
                target_id=str(campaign.pk), ip=client_ip(request),
                metadata={"to": campaign.visibility},
            )
        # A progress-mode or window change must recompute progress/health in the same request, not
        # wait for the hourly sweep (doc 04 line 156, #11); recompute takes the row lock itself.
        services.recompute(campaign)
        messages.success(request, _("Campaign updated."))
        return redirect("campaigns:detail", pk=campaign.pk)
    return render(request, "campaigns/form.html", _campaign_form_ctx(request, campaign))


def _sync_restricted_users(campaign, request) -> None:
    """Set the restricted-users M2M from the POST (only meaningful for restricted visibility)."""
    if campaign.visibility != Campaign.Visibility.RESTRICTED:
        campaign.restricted_users.clear()
        return
    ids = [uid for uid in (_int(v) for v in request.POST.getlist("restricted_users")) if uid]
    campaign.restricted_users.set(_user_pool().filter(pk__in=ids))


def _campaign_form_ctx(request, campaign, *, template=None) -> dict:
    editing = campaign.pk is not None
    budget_allowed = (
        services.can_view_budget(request.user, campaign)
        if editing
        else rbac.has_role(request.user, rbac.ROLE_DIRECTOR)
    )
    ctx = {
        "campaign": campaign,
        "editing": editing,
        "users": _user_choices(),
        "categories": Campaign.Category.choices,
        "visibilities": Campaign.Visibility.choices,
        "progress_modes": Campaign.ProgressMode.choices,
        "budget_allowed": budget_allowed,
        "restricted_ids": set(campaign.restricted_users.values_list("pk", flat=True)) if editing else set(),
        "start_at_local": _dt_local(campaign.start_at),
        "target_end_at_local": _dt_local(campaign.target_end_at),
        "tags_str": ", ".join(campaign.tags or []),
        "template": template,
    }
    if template is not None:
        bp = template.blueprint or {}
        ctx["template_counts"] = _blueprint_counts(template)
        ctx["template_objectives"] = bp.get("objectives", [])
        ctx["template_workstreams"] = bp.get("workstreams", [])
        ctx["template_milestones"] = bp.get("milestones", [])
        ctx["template_risks"] = bp.get("risks", [])
    return ctx


# --------------------------------------------------------------------------- #
#  Campaign detail + lifecycle (doc 10 §6.2)
# --------------------------------------------------------------------------- #
# Per-target lifecycle button metadata: label, button class, whether it needs a confirm dialog.
# Reason-required targets (rework/pause/fail/cancel) grow a reason textarea in the template.
# COMPLETED and FAILED are deliberately absent: they are reachable only through the guided
# close-out (the "Close out…" button → campaigns:close), never a one-click direct transition, so
# the mandatory permanent record can never be bypassed (doc 04 T7/T8, #2).
_TRANSITION_META = {
    Campaign.Status.PROPOSED: (_l("Propose"), "btn-cyan", False),
    Campaign.Status.APPROVED: (_l("Approve"), "btn-cyan", False),
    Campaign.Status.ACTIVE: (_l("Start"), "btn-gold", False),
    Campaign.Status.PAUSED: (_l("Pause"), "btn-ghost", True),
    Campaign.Status.CANCELLED: (_l("Cancel"), "btn-danger", True),
    Campaign.Status.DRAFT: (_l("Send back to draft"), "btn-ghost", False),
    Campaign.Status.ARCHIVED: (_l("Archive"), "btn-ghost", True),
}
_REASON_TARGETS = {
    Campaign.Status.DRAFT, Campaign.Status.PAUSED, Campaign.Status.FAILED, Campaign.Status.CANCELLED,
}


def _lifecycle_buttons(campaign, user) -> list[dict]:
    """The transitions this user may perform from the current status — nothing illegal ever
    renders, so a button can never offer a transition the service would reject (doc 04 §1)."""
    buttons = []
    for to_status, (label, btn, confirm) in _TRANSITION_META.items():
        if not services.can_transition(campaign, to_status, user):
            continue
        if to_status == Campaign.Status.ACTIVE and campaign.status == Campaign.Status.PAUSED:
            label = _("Resume")
        buttons.append({
            "to": to_status,
            "label": label,
            "btn": btn,
            "confirm": confirm,
            "reason": to_status in _REASON_TARGETS,
            "override": to_status == Campaign.Status.COMPLETED,
        })
    return buttons


@login_required
def campaign_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """The single source of truth for one campaign — all panels, permission-gated (doc 10 §6.2)."""
    campaign = _campaign_for_view(request, pk)
    user = request.user
    can_manage = services.can_manage(user, campaign)
    can_budget = services.can_view_budget(user, campaign)

    objectives = list(
        campaign.objectives.select_related("workstream", "owner").order_by("sort_order", "id")
    )
    workstreams = list(
        campaign.workstreams.select_related("lead")
        .prefetch_related("lead__characters")
        .order_by("sort_order", "id")
    )
    milestones = list(
        campaign.milestones.select_related("owner", "workstream").order_by("sort_order", "due_at", "id")
    )

    # Objectives grouped by workstream (None group last), with sensitive values stripped.
    grouped: list[dict] = []
    for ws in [*workstreams, None]:
        members = [
            _objective_vm(o, user) for o in objectives
            if (o.workstream_id == ws.pk if ws else o.workstream_id is None)
        ]
        if members or ws is not None:
            grouped.append({"workstream": ws, "objectives": members})

    ctx = {
        "campaign": campaign,
        "can_manage": can_manage,
        "can_budget": can_budget,
        "grouped_objectives": grouped,
        "has_objectives": bool(objectives),
        "workstreams": workstreams,
        "milestones": milestones,
        "risks": campaign.risks.select_related("owner").exclude(status=Risk.RiskStatus.RETIRED),
        "issues": campaign.issues.select_related("owner", "objective").exclude(
            status=Issue.IssueStatus.RESOLVED
        ),
        "dependencies": _dependency_rows(campaign, objectives, milestones, workstreams),
        "activity": campaign.activity.select_related("actor").prefetch_related("actor__characters")[:20],
        "next_actions": _recommended_actions(campaign, objectives),
        "lifecycle_buttons": _lifecycle_buttons(campaign, user),
        "staging_visible": _staging_visible(user, campaign),
        "evidence": list(_evidence_rows(campaign, EvidenceKind.CAMPAIGN, campaign.pk)),
        "can_attach_evidence": services.can_attach_evidence(
            user, campaign, EvidenceKind.CAMPAIGN, campaign.pk
        ),
        "participation": services.participation_panel(campaign, user),
        "is_terminal": campaign.status in _TERMINAL_STATUSES,
        "can_close": can_manage and campaign.status == Campaign.Status.ACTIVE,
        "linked_operations": _linked_operation_rows(campaign),
        "linkable_operations": _linkable_operations(campaign) if can_manage else [],
        "manual_progress": can_manage and campaign.progress_mode == Campaign.ProgressMode.MANUAL,
    }
    if can_manage:
        # The dependency builder's From/To pickers offer this campaign's own items by name, plus other
        # visible campaigns as blockers — never a free-typed kind/id (doc 04 §8).
        ctx["dep_objectives"] = [(o.pk, o.title) for o in objectives]
        ctx["dep_milestones"] = [(m.pk, m.title) for m in milestones]
        ctx["dep_workstreams"] = [(w.pk, w.name) for w in workstreams]
        ctx["dep_campaigns"] = list(
            services.visible_campaigns(user).exclude(pk=campaign.pk)
            .order_by("name").values_list("pk", "name")[:100]
        )
    return render(request, "campaigns/detail.html", ctx)


# Statuses at which a campaign has a permanent close-out record (report + lessons surfaces).
_TERMINAL_STATUSES = (
    Campaign.Status.COMPLETED, Campaign.Status.FAILED,
    Campaign.Status.CANCELLED, Campaign.Status.ARCHIVED,
)


def _staging_visible(user, campaign) -> bool:
    """Staging system on a restricted campaign is participants + directors only (doc 07 §1.5)."""
    if campaign.visibility != Campaign.Visibility.RESTRICTED:
        return True
    return services.can_manage(user, campaign) or rbac.has_role(user, rbac.ROLE_DIRECTOR)


def _dependency_rows(campaign, objectives, milestones, workstreams) -> list[dict]:
    """Unresolved dependency edges, each endpoint resolved to a friendly name instead of a bare
    ``objective #5`` (doc 10 §6.2). Own-campaign endpoints resolve from the already-loaded objective/
    milestone/workstream lists (no extra query); only a cross-campaign endpoint costs one lookup, and
    only when such an edge exists. A since-removed endpoint degrades to ``kind #id (removed)``."""
    deps = list(campaign.dependencies.filter(is_resolved=False).select_related("campaign"))
    if not deps:
        return []
    dk = DependencyKind
    names = {
        dk.OBJECTIVE: {o.pk: o.title for o in objectives},
        dk.MILESTONE: {m.pk: m.title for m in milestones},
        dk.WORKSTREAM: {w.pk: w.name for w in workstreams},
        dk.CAMPAIGN: {},
    }
    camp_ids = {
        d.to_id for d in deps if d.to_kind == dk.CAMPAIGN
    } | {d.from_id for d in deps if d.from_kind == dk.CAMPAIGN}
    if camp_ids:
        names[dk.CAMPAIGN] = dict(
            Campaign.objects.filter(pk__in=camp_ids).values_list("pk", "name")
        )

    def label(kind, oid):
        if kind == dk.EXTERNAL:
            return "external"
        if kind == dk.CAMPAIGN and oid == campaign.pk:
            return campaign.name
        resolved = names.get(kind, {}).get(oid)
        return resolved or f"{kind} #{oid} (removed)"

    return [
        {
            "dep": dep,
            "external": dep.to_kind == dk.EXTERNAL,
            "from_label": label(dep.from_kind, dep.from_id),
            "to_label": label(dep.to_kind, dep.to_id),
        }
        for dep in deps
    ]


def _recommended_actions(campaign, objectives) -> list[dict]:
    """Deterministic next-actions list: overdue → blocked → unowned → stale (doc 10 §6.2)."""
    now = timezone.now()
    obj_status = Objective.ObjectiveStatus
    terminal = {obj_status.MET, obj_status.MISSED, obj_status.DROPPED}
    overdue, blocked, unowned = [], [], []
    for o in objectives:
        if o.status in terminal:
            continue
        if o.due_at and o.due_at < now:
            overdue.append({"kind": "overdue", "obj": o, "note": _("overdue")})
        elif o.status == obj_status.BLOCKED:
            blocked.append({"kind": "blocked", "obj": o, "note": o.block_reason or _("blocked")})
        elif o.owner_id is None:
            unowned.append({"kind": "unowned", "obj": o, "note": _("no owner assigned")})
    return (overdue + blocked + unowned)[:8]


@login_required
@require_POST
def campaign_set_status(request: HttpRequest, pk: int) -> HttpResponse:
    """Guarded lifecycle transition (doc 04 §1). Permission failures on a *visible* campaign are
    403; an illegal edge surfaces as a message from the service's ``ValidationError``."""
    campaign = _campaign_for_view(request, pk)
    to_status = (request.POST.get("to") or "").strip()
    if to_status not in Campaign.Status.values:
        messages.error(request, _("Unknown status."))
        return redirect("campaigns:detail", pk=campaign.pk)

    # 403 for a forbidden action on a campaign the user can see (doc 07: 403 ≠ 404).
    if to_status == Campaign.Status.APPROVED:
        if not services.can_approve(request.user):
            raise PermissionDenied(_("Approval is director-only."))
    elif not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage this campaign."))

    try:
        services.set_status(campaign, to_status, request.user, reason=request.POST.get("reason", ""))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(
            request,
            _("Campaign is now %(status)s.") % {"status": campaign.get_status_display().lower()},
        )
    return redirect("campaigns:detail", pk=campaign.pk)


# --------------------------------------------------------------------------- #
#  Manual progress entry (doc 04 §4)
# --------------------------------------------------------------------------- #
@login_required
@require_POST
def campaign_set_progress(request: HttpRequest, pk: int) -> HttpResponse:
    """Set a manual-progress campaign's percent by hand (``can_manage``, doc 04 §4). The service
    requires a provenance note and rejects any non-manual progress mode."""
    campaign = _campaign_for_view(request, pk)
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage this campaign."))
    try:
        services.set_manual_progress(
            campaign, request.user, request.POST.get("progress_pct", ""),
            request.POST.get("note", ""),
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, _("Progress updated."))
    return redirect("campaigns:detail", pk=campaign.pk)


# --------------------------------------------------------------------------- #
#  Progress explanation (doc 10 §6.4)
# --------------------------------------------------------------------------- #
@login_required
def progress_explanation(request: HttpRequest, pk: int) -> HttpResponse:
    """The transparency table: every objective decomposed, sensitive values masked but the math
    kept honest (weight + contribution still shown, doc 10 §6.4)."""
    campaign = _campaign_for_view(request, pk)
    objectives = [
        o for o in campaign.objectives.select_related("workstream").order_by("sort_order", "id")
        if o.status != Objective.ObjectiveStatus.DROPPED
    ]
    total_weight = sum(o.weight for o in objectives) or 0
    rows = []
    for o in objectives:
        vm = _objective_vm(o, request.user)
        contribution = (
            round(o.progress_pct * o.weight / total_weight, 1) if total_weight else 0.0
        )
        rows.append({**vm, "weight": o.weight, "contribution": contribution})
    ctx = {
        "campaign": campaign,
        "rows": rows,
        "total_weight": total_weight,
        "mode": campaign.progress_mode,
        "milestone_total": campaign.milestones.count(),
        "milestone_done": campaign.milestones.filter(
            status=Milestone.MilestoneStatus.DONE
        ).count(),
        "recomputed": humanize_as_of(campaign.updated_at),
    }
    return render(request, "campaigns/explain.html", ctx)


# --------------------------------------------------------------------------- #
#  Objectives (doc 10 §6.3, §6.10)
# --------------------------------------------------------------------------- #
def _apply_objective_fields(objective, request, campaign) -> None:
    """Allow-listed objective definition fields, including the P3 metric-source picker.

    ``metric_source`` is validated against the registry (unknown ⇒ manual); when a source is
    chosen its params are read from the namespaced ``p__<key>__<field>`` inputs and validated
    against the source's ``params_schema`` via ``metrics.clean_params`` — a malformed configuration
    raises here (form re-render), never inside the beat (doc 12 §3c). A finance/SRP source defaults
    ``is_sensitive`` on. ``measurement_paused`` freezes auto refresh so a manual value stands (doc
    04 §3.2); this handler is manage-gated in the view.
    """
    post = request.POST
    title = (post.get("title") or "").strip()
    if not title:
        raise ValidationError(_("An objective needs a title."))
    objective.title = title[:200]
    objective.description = (post.get("description") or "").strip()

    ws_id = _int(post.get("workstream"))
    objective.workstream = (
        campaign.workstreams.filter(pk=ws_id).first() if ws_id else None
    )
    objective.owner = _pool_user(post.get("owner"))
    objective.weight = max(1, _int(post.get("weight"), 1) or 1)
    objective.due_at = _dt(post.get("due_at"))
    objective.unit = (post.get("unit") or "").strip()[:16]
    direction = (post.get("direction") or "").strip()
    objective.direction = (
        direction if direction in Objective.Direction.values else Objective.Direction.GTE
    )
    objective.baseline_value = _dec(post.get("baseline_value"))
    objective.target_value = _dec(post.get("target_value"))
    objective.is_mandatory = bool(post.get("is_mandatory"))
    objective.help_wanted = bool(post.get("help_wanted"))
    objective.requires_verification = bool(post.get("requires_verification"))
    objective.measurement_paused = bool(post.get("measurement_paused"))

    source = metrics.get_source((post.get("metric_source") or "").strip())
    if source is None:
        objective.metric_source = ""
        objective.metric_params = {}
    else:
        objective.metric_source = source.key
        raw = {}
        for f in source.params_schema:
            field_name = f"p__{source.key}__{f['name']}"
            # A native multi-select posts one value per choice → read the list; every other widget
            # (including the type-chips, which post one comma-joined hidden field) is a single value.
            if f.get("widget") == "structure_multi":
                raw[f["name"]] = post.getlist(field_name)
            else:
                raw[f["name"]] = post.get(field_name)
        objective.metric_params = metrics.clean_params(source, raw)
    objective.is_sensitive = bool(post.get("is_sensitive")) or (
        source is not None and source.sensitive_default
    )


# --------------------------------------------------------------------------- #
#  Metric-param widgets (doc 04 §3, doc 12 §3): the objective form and objective page
#  resolve every entity id to a friendly name here, at the presentation boundary. The
#  stored ``metric_params`` primitives (int / list-of-int / str) are never changed — only
#  the rendered control (a name select / picker) and the displayed value are resolved.
# --------------------------------------------------------------------------- #
# Widgets whose options are a fixed model-backed ``(value, label)`` list (a server ``<select>``);
# the type autocomplete and the month input resolve differently and are not in this set.
_OPTION_WIDGETS = {"doctrine", "stockpile", "wallet_division", "structure_multi", "readiness_dimension"}


def _widget_options(widget: str) -> list[tuple]:
    """``(value, label)`` options for a model-backed metric-param ``<select>`` — active doctrines,
    stockpiles, wallet divisions, corp structures, or readiness dimensions, each labelled by name.
    Empty for a non-option widget, or when the backing table is empty (the form shows an empty
    state, never a number box)."""
    if widget == "doctrine":
        from apps.doctrines.models import Doctrine

        return [
            (d.pk, d.name)
            for d in Doctrine.objects.filter(status=Doctrine.Status.ACTIVE).order_by("name")
        ]
    if widget == "stockpile":
        from apps.stockpile.models import Stockpile

        rows = []
        for s in Stockpile.objects.select_related("location").order_by("name"):
            loc = (s.location.name or str(s.location_id)) if s.location_id else ""
            rows.append((s.pk, f"{s.name} — {loc}" if loc else s.name))
        return rows
    if widget == "wallet_division":
        from apps.corporation.models import CorpWalletDivision

        rows = []
        for d in CorpWalletDivision.objects.order_by("division"):
            label = (
                f"{d.division} — {d.name}"
                if d.name
                else _("Division %(number)s") % {"number": d.division}
            )
            rows.append((d.division, label))
        return rows
    if widget == "structure_multi":
        from apps.corporation.models import CorpStructure

        rows = []
        for s in CorpStructure.objects.order_by("name"):
            system = s.system_name or _("unknown system")
            rows.append((s.structure_id, f"{s.name or s.structure_id} — {system}"))
        return rows
    if widget == "readiness_dimension":
        from apps.readiness.engine import registry

        return [(p.key, p.label) for p in registry.providers()]
    return []


def _sde_type_names(type_ids) -> dict:
    """``{str(type_id): name}`` for the given ids (the type-picker seed / objective-page display)."""
    ids = [int(t) for t in type_ids if str(t).strip().lstrip("-").isdigit()]
    if not ids:
        return {}
    from apps.sde.models import SdeType

    return {
        str(tid): name
        for tid, name in SdeType.objects.filter(type_id__in=ids).values_list("type_id", "name")
    }


def _lookup_or_removed(labels: dict, value) -> str:
    """Resolve an id/key against a ``{str(key): label}`` map, degrading a since-removed id to
    ``id N (removed)`` rather than leaking (or 404ing on) the bare number (soft-link discipline)."""
    return labels.get(str(value)) or _("id %(value)s (removed)") % {"value": value}


def _metric_source_options(current_key="", current_params=None) -> list[dict]:
    """Registry sources shaped for the objective-form picker (doc 10 §5). Each param field carries
    its widget kind, the resolved name options for a model-backed select, and — for the objective's
    current source — its stored value pre-filled (a scalar, a multi-select id list, or the SDE
    type name(s) that seed a type picker), so the form never asks for a raw id."""
    current_params = current_params or {}
    options = []
    for source in metrics.all_sources():
        is_current = source.key == current_key
        fields = []
        for f in source.params_schema:
            widget = f.get("widget", "")
            raw = current_params.get(f["name"]) if is_current else None
            raw_list = raw if isinstance(raw, list) else ([] if raw in (None, "") else [raw])
            field = {
                "name": f["name"],
                "kind": f.get("kind", "str"),
                "widget": widget,
                "label": f.get("label", f["name"]),
                "required": bool(f.get("required")),
                "help": f.get("help", ""),
                "choices": metrics.resolve_choices(f) if f.get("kind") == "choice" else [],
                "is_select": widget in _OPTION_WIDGETS,
                "options": _widget_options(widget) if widget in _OPTION_WIDGETS else [],
                "value": "" if isinstance(raw, list) or raw is None else str(raw),
                "value_list": [str(v) for v in raw_list],
            }
            if widget == "type":
                field["value_name"] = _sde_type_names([raw]).get(str(raw), "") if raw not in (None, "") else ""
            elif widget == "type_multi":
                names = _sde_type_names(raw_list)
                field["value_items"] = [
                    {
                        "id": str(v),
                        "name": names.get(str(v), _("type %(id)s") % {"id": v}),
                    }
                    for v in raw_list
                ]
            fields.append(field)
        options.append({
            "key": source.key, "label": source.label, "unit": source.unit,
            "sensitive": source.sensitive_default, "fields": fields,
        })
    return options


def params_display(source, params) -> list[dict]:
    """Friendly ``(label, value)`` pairs for an objective's stored metric params (objective page) —
    every doctrine / stockpile / wallet / structure / dimension / item-type id resolved to its name.
    A since-removed id degrades to ``id N (removed)``; an omitted optional multi reads ``all``."""
    params = params or {}
    # One SDE lookup for every item-type id referenced by this source's type widgets.
    type_ids: list = []
    for f in source.params_schema:
        if f.get("widget") in ("type", "type_multi") and f["name"] in params:
            v = params[f["name"]]
            type_ids += v if isinstance(v, list) else [v]
    type_names = _sde_type_names(type_ids)

    out = []
    for f in source.params_schema:
        name = f["name"]
        if name not in params:
            continue
        value = params[name]
        widget = f.get("widget", "")
        if widget in _OPTION_WIDGETS:
            labels = {str(k): str(v) for k, v in _widget_options(widget)}
            if isinstance(value, list):
                display = ", ".join(_lookup_or_removed(labels, v) for v in value) or _("all")
            else:
                display = _lookup_or_removed(labels, value)
        elif widget in ("type", "type_multi"):
            if isinstance(value, list):
                display = ", ".join(_lookup_or_removed(type_names, v) for v in value) or _("all")
            else:
                display = _lookup_or_removed(type_names, value)
        elif f.get("kind") == "choice":
            labels = {str(c[0]): str(c[1]) for c in metrics.resolve_choices(f)}
            display = labels.get(str(value), value)
        elif isinstance(value, list):
            display = ", ".join(str(v) for v in value)
        else:
            display = value
        out.append({"label": f.get("label", name), "value": display})
    return out


def _objective_structural_snapshot(objective) -> dict:
    """The 'goalpost' fields whose change on an *active* campaign is an audited event (doc 04
    lines 83-85) — moving a target/weight/direction/mandatory flag after the campaign is live."""
    return {
        "target_value": None if objective.target_value is None else str(objective.target_value),
        "baseline_value": None if objective.baseline_value is None else str(objective.baseline_value),
        "weight": objective.weight,
        "direction": objective.direction,
        "is_mandatory": objective.is_mandatory,
    }


def _objective_form_ctx(request, campaign, objective) -> dict:
    return {
        "campaign": campaign,
        "objective": objective,
        "editing": objective.pk is not None,
        "users": _user_choices(),
        "workstreams": campaign.workstreams.order_by("sort_order", "id"),
        "directions": Objective.Direction.choices,
        "due_at_local": _dt_local(objective.due_at),
        "metric_sources": _metric_source_options(objective.metric_source, objective.metric_params),
    }


@login_required
def objective_create(request: HttpRequest, pk: int) -> HttpResponse:
    campaign = _campaign_for_view(request, pk)
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage this campaign."))
    objective = Objective(campaign=campaign)
    if request.method == "POST":
        try:
            _apply_objective_fields(objective, request, campaign)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(request, "campaigns/objective_form.html",
                          _objective_form_ctx(request, campaign, objective))
        objective.save()
        services.record_activity(
            campaign, request.user, "objective.created", target_kind="objective",
            target_id=objective.pk,
        )
        _notify_assignment(campaign, "objective", objective.pk, objective.owner, None,
                           request.user, what="an objective")
        services.recompute(campaign)
        messages.success(request, _("Objective added."))
        return redirect("campaigns:detail", pk=campaign.pk)
    return render(request, "campaigns/objective_form.html",
                  _objective_form_ctx(request, campaign, objective))


@login_required
def objective_edit(request: HttpRequest, pk: int) -> HttpResponse:
    objective = _objective_for(request, pk)
    campaign = objective.campaign
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage this campaign."))
    if request.method == "POST":
        old_owner_id = objective.owner_id
        structural_before = _objective_structural_snapshot(objective)
        try:
            _apply_objective_fields(objective, request, campaign)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(request, "campaigns/objective_form.html",
                          _objective_form_ctx(request, campaign, objective))
        objective.save(update_fields=_OBJECTIVE_EDIT_FIELDS)
        # Moving a goalpost on a live campaign is the doc 04 lines 83-85 abuse case: record the
        # before/after diff and the dedicated audit so a target change can never be silent.
        structural_after = _objective_structural_snapshot(objective)
        goalpost_moved = (
            campaign.status == Campaign.Status.ACTIVE and structural_before != structural_after
        )
        services.record_activity(
            campaign, request.user, "objective.edited", target_kind="objective",
            target_id=objective.pk,
            before=structural_before if goalpost_moved else None,
            after=structural_after if goalpost_moved else None,
        )
        if goalpost_moved:
            audit_log(
                request.user, "campaigns.objective_target_changed",
                target_type="campaign_objective", target_id=str(objective.pk), ip=client_ip(request),
                metadata={"campaign_id": campaign.pk, "before": structural_before,
                          "after": structural_after},
            )
        _notify_assignment(campaign, "objective", objective.pk, objective.owner, old_owner_id,
                           request.user, what="an objective")
        services.recompute(campaign)
        messages.success(request, _("Objective updated."))
        return redirect("campaigns:objective_detail", pk=objective.pk)
    return render(request, "campaigns/objective_form.html",
                  _objective_form_ctx(request, campaign, objective))


@login_required
def objective_detail(request: HttpRequest, pk: int) -> HttpResponse:
    objective = _objective_for(request, pk)
    campaign = objective.campaign
    user = request.user
    vm = _objective_vm(objective, user)
    can_update = services.can_update_objective(user, objective)
    can_verify = (
        objective.status == Objective.ObjectiveStatus.MET
        and services.can_verify(user, objective)
    )
    samples = []
    sparkline_values = []
    if vm["value_visible"]:
        samples = list(objective.samples.all()[:30])
        # ``samples`` are newest-first; the sparkline reads oldest → newest, left to right.
        sparkline_values = [float(s.value) for s in reversed(samples)]
    source = metrics.get_source(objective.metric_source) if objective.metric_source else None
    params_summary = params_display(source, objective.metric_params) if source else []
    activity = campaign.activity.select_related("actor").prefetch_related("actor__characters").filter(
        target_kind="objective", target_id=objective.pk
    )[:20]
    can_manage = services.can_manage(user, campaign)
    linked_tasks = list(
        objective.linked_tasks().select_related("assignee")
        .prefetch_related("assignee__characters").order_by("status", "-updated_at")
    )
    ctx = {
        "campaign": campaign,
        "objective": objective,
        "vm": vm,
        "can_manage": can_manage,
        "can_update": can_update,
        "can_verify_now": can_verify,
        "is_officer": rbac.has_role(user, rbac.ROLE_OFFICER),
        "samples": samples,
        "sparkline_values": sparkline_values,
        "source": source,
        "params_summary": params_summary,
        "activity": activity,
        "open_issues": objective.issues.exclude(status=Issue.IssueStatus.RESOLVED),
        "status_choices": Objective.ObjectiveStatus.choices,
        "linked_tasks": linked_tasks,
        "can_create_task": can_manage or objective.owner_id == user.pk,
        "can_volunteer": (
            objective.help_wanted and rbac.has_role(user, rbac.ROLE_MEMBER)
            and objective.status not in (Objective.ObjectiveStatus.MET,
                                         Objective.ObjectiveStatus.DROPPED,
                                         Objective.ObjectiveStatus.MISSED)
        ),
        "evidence": list(
            _evidence_rows(campaign, EvidenceKind.OBJECTIVE, objective.pk)
        ),
        "can_attach_evidence": services.can_attach_evidence(
            user, campaign, EvidenceKind.OBJECTIVE, objective.pk
        ),
    }
    return render(request, "campaigns/objective_detail.html", ctx)


def _evidence_rows(campaign, kind, attached_id):
    """Evidence attached to one object, newest first (author resolved for the render layer).

    ``added_by__characters`` is prefetched because the list renders each author's ``display_name``
    (main-character name) — one query for the set instead of a per-row character walk."""
    return (
        campaign.evidence.filter(attached_kind=kind, attached_id=attached_id)
        .select_related("added_by")
        .prefetch_related("added_by__characters")
    )


@login_required
@require_POST
def objective_update_value(request: HttpRequest, pk: int) -> HttpResponse:
    """Record a manual value (owner / workstream lead / manage). Note is mandatory (service)."""
    objective = _objective_for(request, pk)
    if not services.can_update_objective(request.user, objective):
        raise PermissionDenied(_("You cannot update this objective."))
    try:
        services.update_manual_value(
            objective, request.user, request.POST.get("value", ""), request.POST.get("note", "")
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, _("Value recorded."))
    return redirect("campaigns:objective_detail", pk=objective.pk)


@login_required
@require_POST
def objective_verify(request: HttpRequest, pk: int) -> HttpResponse:
    """Officer sign-off on a met claim — officer rank required (403 below), verifier ≠ claimant."""
    objective = _objective_for(request, pk)
    if not rbac.has_role(request.user, rbac.ROLE_OFFICER):
        raise PermissionDenied(_("Verification requires officer rank."))
    try:
        services.verify_objective(objective, request.user)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, _("Objective verified."))
    return redirect("campaigns:objective_detail", pk=objective.pk)


@login_required
@require_POST
def objective_set_status(request: HttpRequest, pk: int) -> HttpResponse:
    """Move an objective through its status set (owner / lead / manage)."""
    objective = _objective_for(request, pk)
    if not services.can_update_objective(request.user, objective):
        raise PermissionDenied(_("You cannot change this objective."))
    to_status = (request.POST.get("to") or "").strip()
    try:
        services.set_objective_status(
            objective, request.user, to_status, reason=request.POST.get("reason", "")
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, _("Objective status updated."))
    return redirect("campaigns:objective_detail", pk=objective.pk)


# --------------------------------------------------------------------------- #
#  Milestones (doc 10 §6.10)
# --------------------------------------------------------------------------- #
def _apply_milestone_fields(milestone, request, campaign) -> None:
    post = request.POST
    title = (post.get("title") or "").strip()
    if not title:
        raise ValidationError(_("A milestone needs a title."))
    milestone.title = title[:200]
    milestone.description = (post.get("description") or "").strip()
    ws_id = _int(post.get("workstream"))
    milestone.workstream = campaign.workstreams.filter(pk=ws_id).first() if ws_id else None
    milestone.owner = _pool_user(post.get("owner"))
    milestone.due_at = _dt(post.get("due_at"))
    milestone.sort_order = _int(post.get("sort_order"), 0) or 0


def _milestone_form_ctx(request, campaign, milestone) -> dict:
    ctx = {
        "campaign": campaign,
        "milestone": milestone,
        "editing": milestone.pk is not None,
        "users": _user_choices(),
        "workstreams": campaign.workstreams.order_by("sort_order", "id"),
        "due_at_local": _dt_local(milestone.due_at),
    }
    if milestone.pk:  # evidence attaches to an existing milestone (doc 04 §9)
        ctx["evidence"] = list(_evidence_rows(campaign, EvidenceKind.MILESTONE, milestone.pk))
        ctx["can_attach_evidence"] = services.can_attach_evidence(
            request.user, campaign, EvidenceKind.MILESTONE, milestone.pk
        )
    return ctx


@login_required
def milestone_create(request: HttpRequest, pk: int) -> HttpResponse:
    campaign = _campaign_for_view(request, pk)
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage this campaign."))
    milestone = Milestone(campaign=campaign)
    if request.method == "POST":
        try:
            _apply_milestone_fields(milestone, request, campaign)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(request, "campaigns/milestone_form.html",
                          _milestone_form_ctx(request, campaign, milestone))
        services.save_milestone(milestone, request.user)
        _notify_assignment(campaign, "milestone", milestone.pk, milestone.owner, None,
                           request.user, what="a milestone")
        messages.success(request, _("Milestone added."))
        return redirect("campaigns:detail", pk=campaign.pk)
    return render(request, "campaigns/milestone_form.html",
                  _milestone_form_ctx(request, campaign, milestone))


@login_required
def milestone_edit(request: HttpRequest, pk: int) -> HttpResponse:
    milestone = get_object_or_404(Milestone.objects.select_related("campaign"), pk=pk)
    campaign = milestone.campaign
    if not services.can_view(request.user, campaign):
        raise Http404(_("No such milestone."))
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage this campaign."))
    if request.method == "POST":
        old_owner_id = milestone.owner_id
        try:
            _apply_milestone_fields(milestone, request, campaign)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(request, "campaigns/milestone_form.html",
                          _milestone_form_ctx(request, campaign, milestone))
        services.save_milestone(milestone, request.user)
        _notify_assignment(campaign, "milestone", milestone.pk, milestone.owner, old_owner_id,
                           request.user, what="a milestone")
        messages.success(request, _("Milestone updated."))
        return redirect("campaigns:detail", pk=campaign.pk)
    return render(request, "campaigns/milestone_form.html",
                  _milestone_form_ctx(request, campaign, milestone))


@login_required
@require_POST
def milestone_set_status(request: HttpRequest, pk: int) -> HttpResponse:
    """Owner/lead may mark ready-for-review; ``can_manage`` may approve (done) or miss it."""
    milestone = get_object_or_404(Milestone.objects.select_related("campaign", "workstream"), pk=pk)
    campaign = milestone.campaign
    if not services.can_view(request.user, campaign):
        raise Http404(_("No such milestone."))
    to_status = (request.POST.get("to") or "").strip()
    ms = Milestone.MilestoneStatus
    if to_status == ms.READY_FOR_REVIEW:
        lead_id = milestone.workstream.lead_id if milestone.workstream_id else None
        allowed = (
            services.can_manage(request.user, campaign)
            or milestone.owner_id == request.user.pk
            or lead_id == request.user.pk
        )
    else:
        allowed = services.can_manage(request.user, campaign)
    if not allowed:
        raise PermissionDenied(_("You cannot change this milestone."))
    try:
        services.set_milestone_status(milestone, request.user, to_status)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, _("Milestone status updated."))
    return redirect("campaigns:detail", pk=campaign.pk)


# --------------------------------------------------------------------------- #
#  Workstreams (doc 10 §6.10)
# --------------------------------------------------------------------------- #
def _apply_workstream_fields(workstream, request) -> None:
    post = request.POST
    name = (post.get("name") or "").strip()
    if not name:
        raise ValidationError(_("A workstream needs a name."))
    workstream.name = name[:120]
    workstream.description = (post.get("description") or "").strip()
    workstream.lead = _pool_user(post.get("lead"))
    status = (post.get("status") or "").strip()
    if status in Workstream.WorkstreamStatus.values:
        workstream.status = status
    workstream.sort_order = _int(post.get("sort_order"), 0) or 0


def _workstream_form_ctx(request, campaign, workstream) -> dict:
    return {
        "campaign": campaign,
        "workstream": workstream,
        "editing": workstream.pk is not None,
        "users": _user_choices(),
        "statuses": Workstream.WorkstreamStatus.choices,
    }


@login_required
def workstream_create(request: HttpRequest, pk: int) -> HttpResponse:
    campaign = _campaign_for_view(request, pk)
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage this campaign."))
    workstream = Workstream(campaign=campaign)
    if request.method == "POST":
        try:
            _apply_workstream_fields(workstream, request)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(request, "campaigns/workstream_form.html",
                          _workstream_form_ctx(request, campaign, workstream))
        services.save_workstream(workstream, request.user)
        _notify_assignment(campaign, "workstream", workstream.pk, workstream.lead, None,
                           request.user, what="a workstream")
        messages.success(request, _("Workstream added."))
        return redirect("campaigns:detail", pk=campaign.pk)
    return render(request, "campaigns/workstream_form.html",
                  _workstream_form_ctx(request, campaign, workstream))


@login_required
def workstream_edit(request: HttpRequest, pk: int) -> HttpResponse:
    workstream = get_object_or_404(Workstream.objects.select_related("campaign"), pk=pk)
    campaign = workstream.campaign
    if not services.can_view(request.user, campaign):
        raise Http404(_("No such workstream."))
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage this campaign."))
    if request.method == "POST":
        old_lead_id = workstream.lead_id
        try:
            _apply_workstream_fields(workstream, request)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(request, "campaigns/workstream_form.html",
                          _workstream_form_ctx(request, campaign, workstream))
        services.save_workstream(workstream, request.user)
        _notify_assignment(campaign, "workstream", workstream.pk, workstream.lead, old_lead_id,
                           request.user, what="a workstream")
        messages.success(request, _("Workstream updated."))
        return redirect("campaigns:detail", pk=campaign.pk)
    return render(request, "campaigns/workstream_form.html",
                  _workstream_form_ctx(request, campaign, workstream))


# --------------------------------------------------------------------------- #
#  Risks (doc 10 §6.10)
# --------------------------------------------------------------------------- #
def _apply_risk_fields(risk, request, campaign) -> None:
    post = request.POST
    description = (post.get("description") or "").strip()
    if not description:
        raise ValidationError(_("A risk needs a description."))
    risk.description = description
    levels = Risk.RiskLevel.values
    prob = (post.get("probability") or "").strip()
    impact = (post.get("impact") or "").strip()
    risk.probability = prob if prob in levels else Risk.RiskLevel.MEDIUM
    risk.impact = impact if impact in levels else Risk.RiskLevel.MEDIUM
    ws_id = _int(post.get("workstream"))
    risk.workstream = campaign.workstreams.filter(pk=ws_id).first() if ws_id else None
    risk.owner = _pool_user(post.get("owner"))
    risk.mitigation = (post.get("mitigation") or "").strip()
    risk.contingency = (post.get("contingency") or "").strip()
    risk.trigger = (post.get("trigger") or "").strip()[:200]
    risk.due_at = _dt(post.get("due_at"))
    status = (post.get("status") or "").strip()
    if status in Risk.RiskStatus.values:
        risk.status = status


def _risk_form_ctx(request, campaign, risk) -> dict:
    return {
        "campaign": campaign,
        "risk": risk,
        "editing": risk.pk is not None,
        "users": _user_choices(),
        "workstreams": campaign.workstreams.order_by("sort_order", "id"),
        "levels": Risk.RiskLevel.choices,
        "statuses": Risk.RiskStatus.choices,
        "due_at_local": _dt_local(risk.due_at),
    }


def _can_manage_risks(user, campaign, risk=None) -> bool:
    if services.can_manage(user, campaign):
        return True
    if risk is not None and risk.owner_id == user.pk:
        return True
    return any(ws.lead_id == user.pk for ws in campaign.workstreams.all())


@login_required
def risk_create(request: HttpRequest, pk: int) -> HttpResponse:
    campaign = _campaign_for_view(request, pk)
    if not _can_manage_risks(request.user, campaign):
        raise PermissionDenied(_("You cannot add risks to this campaign."))
    risk = Risk(campaign=campaign)
    if request.method == "POST":
        try:
            _apply_risk_fields(risk, request, campaign)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(request, "campaigns/risk_form.html",
                          _risk_form_ctx(request, campaign, risk))
        services.save_risk(risk, request.user)
        messages.success(request, _("Risk added."))
        return redirect("campaigns:detail", pk=campaign.pk)
    return render(request, "campaigns/risk_form.html", _risk_form_ctx(request, campaign, risk))


@login_required
def risk_edit(request: HttpRequest, pk: int) -> HttpResponse:
    risk = get_object_or_404(Risk.objects.select_related("campaign"), pk=pk)
    campaign = risk.campaign
    if not services.can_view(request.user, campaign):
        raise Http404(_("No such risk."))
    if not _can_manage_risks(request.user, campaign, risk):
        raise PermissionDenied(_("You cannot edit this risk."))
    if request.method == "POST":
        try:
            _apply_risk_fields(risk, request, campaign)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(request, "campaigns/risk_form.html",
                          _risk_form_ctx(request, campaign, risk))
        services.save_risk(risk, request.user)
        messages.success(request, _("Risk updated."))
        return redirect("campaigns:detail", pk=campaign.pk)
    return render(request, "campaigns/risk_form.html", _risk_form_ctx(request, campaign, risk))


# --------------------------------------------------------------------------- #
#  Issues (doc 10 §6.10)
# --------------------------------------------------------------------------- #
@login_required
def issue_create(request: HttpRequest, pk: int) -> HttpResponse:
    """Raise a blocker — open to any participant who can view the campaign (doc 10 §5)."""
    campaign = _campaign_for_view(request, pk)
    if not rbac.has_role(request.user, rbac.ROLE_MEMBER):
        raise PermissionDenied(_("Members only."))
    if request.method == "POST":
        obj_id = _int(request.POST.get("objective"))
        objective = campaign.objectives.filter(pk=obj_id).first() if obj_id else None
        try:
            services.raise_issue(
                campaign, request.user, request.POST.get("description", ""),
                objective=objective, effect=request.POST.get("effect", ""),
                owner=_pool_user(request.POST.get("owner")),
                target_resolution_at=_dt(request.POST.get("target_resolution_at")),
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(request, "campaigns/issue_form.html", _issue_form_ctx(request, campaign))
        messages.success(request, _("Issue raised."))
        return redirect("campaigns:detail", pk=campaign.pk)
    return render(request, "campaigns/issue_form.html", _issue_form_ctx(request, campaign))


def _issue_form_ctx(request, campaign) -> dict:
    return {
        "campaign": campaign,
        "users": _user_choices(),
        "objectives": campaign.objectives.exclude(
            status=Objective.ObjectiveStatus.DROPPED
        ).order_by("sort_order", "id"),
    }


@login_required
@require_POST
def issue_resolve(request: HttpRequest, pk: int) -> HttpResponse:
    """Resolve an issue (``can_manage`` or the issue owner); unblocks its objective if last."""
    issue = get_object_or_404(Issue.objects.select_related("campaign", "objective"), pk=pk)
    campaign = issue.campaign
    if not services.can_view(request.user, campaign):
        raise Http404(_("No such issue."))
    if not (services.can_manage(request.user, campaign) or issue.owner_id == request.user.pk):
        raise PermissionDenied(_("You cannot resolve this issue."))
    try:
        services.resolve_issue(issue, request.user, request.POST.get("resolution_notes", ""))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        if issue.objective_id and issue.objective.status != Objective.ObjectiveStatus.BLOCKED:
            messages.success(request, _("Issue resolved. Objective unblocked."))
        else:
            messages.success(request, _("Issue resolved."))
    return redirect("campaigns:detail", pk=campaign.pk)


# --------------------------------------------------------------------------- #
#  Dependencies (doc 10 §6.2)
# --------------------------------------------------------------------------- #
def _parse_dep_endpoint(raw: str) -> tuple[str, int]:
    """Split a dependency picker value ``"kind:id"`` (e.g. ``"objective:5"``, ``"campaign:7"``) into
    ``(kind, id)``; the bare ``"external"`` option maps to ``("external", 0)``. The kind/id are then
    validated by ``services.add_dependency`` exactly as before (in-campaign source, reachable target)."""
    raw = (raw or "").strip()
    if raw == DependencyKind.EXTERNAL:
        return DependencyKind.EXTERNAL, 0
    kind, _sep, sid = raw.partition(":")
    return kind.strip(), _int(sid, 0) or 0


@login_required
@require_POST
def dependency_create(request: HttpRequest, pk: int) -> HttpResponse:
    """Add a blocked-by edge — cycle/self/depth validated in the service (doc 04 §8). The From/To
    pickers post ``kind:id`` values (or ``external``); the service re-checks every endpoint."""
    campaign = _campaign_for_view(request, pk)
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage this campaign."))
    from_kind, from_id = _parse_dep_endpoint(request.POST.get("from"))
    to_kind, to_id = _parse_dep_endpoint(request.POST.get("to"))
    try:
        services.add_dependency(
            campaign,
            from_kind,
            from_id,
            to_kind,
            to_id=to_id,
            note=request.POST.get("note", ""),
            user=request.user,
        )
    except (ValidationError, ValueError) as exc:
        detail = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        messages.error(request, detail)
    else:
        messages.success(request, _("Dependency added."))
    return redirect("campaigns:detail", pk=campaign.pk)


@login_required
@require_POST
def dependency_resolve(request: HttpRequest, pk: int) -> HttpResponse:
    """Manually clear a dependency edge (``can_manage``)."""
    dependency = get_object_or_404(CampaignDependency.objects.select_related("campaign"), pk=pk)
    campaign = dependency.campaign
    if not services.can_view(request.user, campaign):
        raise Http404(_("No such dependency."))
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage this campaign."))
    try:
        services.resolve_dependency(dependency, request.user, reason=request.POST.get("reason", ""))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, _("Dependency resolved."))
    return redirect("campaigns:detail", pk=campaign.pk)


# --------------------------------------------------------------------------- #
#  Linked operations (doc 10 line 216)
# --------------------------------------------------------------------------- #
def _linked_operation_rows(campaign) -> list[dict]:
    """This campaign's linked operations, each resolved defensively — a since-deleted operation
    renders as "removed" rather than 404ing the page (soft-link discipline, doc 06 §3.13)."""
    links = list(campaign.linked_operations.all())
    if not links:
        return []
    from apps.operations.models import Operation

    ops = {o.pk: o for o in Operation.objects.filter(pk__in=[link.operation_id for link in links])}
    return [
        {"link": link, "op": ops.get(link.operation_id), "removed": link.operation_id not in ops}
        for link in links
    ]


def _linkable_operations(campaign):
    """Operations not yet linked to this campaign, for the link picker: those scheduled within the
    last 90 days or in the future, newest first, capped at 100 (doc 10 line 216). The picker offers
    a named operation to select, never a bare id to type."""
    from apps.operations.models import Operation

    cutoff = timezone.now() - timezone.timedelta(days=90)
    linked = set(campaign.linked_operations.values_list("operation_id", flat=True))
    return list(
        Operation.objects.filter(target_at__gte=cutoff)
        .exclude(pk__in=linked or [0])
        .order_by("-target_at", "-id")[:100]
    )


@login_required
@require_POST
def operation_link(request: HttpRequest, pk: int) -> HttpResponse:
    """Soft-link an operation to a campaign (``can_manage``, doc 10 line 216). Idempotent."""
    campaign = _campaign_for_view(request, pk)
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage this campaign."))
    op_id = _int(request.POST.get("operation_id"))
    if op_id is None:
        messages.error(request, _("Choose an operation to link."))
        return redirect("campaigns:detail", pk=campaign.pk)
    try:
        services.link_operation(campaign, request.user, op_id, note=request.POST.get("note", ""))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, _("Operation linked."))
    return redirect("campaigns:detail", pk=campaign.pk)


@login_required
@require_POST
def operation_unlink(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove a campaign↔operation link (``can_manage``)."""
    campaign = _campaign_for_view(request, pk)
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage this campaign."))
    services.unlink_operation(campaign, request.user, _int(request.POST.get("operation_id")))
    messages.success(request, _("Operation unlinked."))
    return redirect("campaigns:detail", pk=campaign.pk)


# --------------------------------------------------------------------------- #
#  Issue escalation (doc 04 §7)
# --------------------------------------------------------------------------- #
@login_required
@require_POST
def issue_escalate(request: HttpRequest, pk: int) -> HttpResponse:
    """Escalate an issue (``can_manage``; mandatory reason). Notifies leadership (doc 09 §4)."""
    issue = get_object_or_404(Issue.objects.select_related("campaign"), pk=pk)
    campaign = issue.campaign
    if not services.can_view(request.user, campaign):
        raise Http404(_("No such issue."))
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("Escalation requires manage capability."))
    try:
        services.escalate_issue(issue, request.user, request.POST.get("reason", ""))
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, _("Issue escalated to leadership."))
    return redirect("campaigns:detail", pk=campaign.pk)


# --------------------------------------------------------------------------- #
#  Linked tasks & volunteering (doc 04 §10, doc 10 §6.3, §6.5)
# --------------------------------------------------------------------------- #
@login_required
@require_POST
def objective_create_task(request: HttpRequest, pk: int) -> HttpResponse:
    """Create a task linked to an objective (``can_manage`` or the objective owner, doc 10 §5)."""
    objective = _objective_for(request, pk)
    if not (services.can_manage(request.user, objective.campaign)
            or objective.owner_id == request.user.pk):
        raise PermissionDenied(_("You cannot add a task to this objective."))
    title = (request.POST.get("title") or "").strip() or objective.title
    assignee = _pool_user(request.POST.get("assignee"))
    services.create_objective_task(
        objective, request.user, title=title, assignee=assignee, due_at=objective.due_at,
    )
    messages.success(request, _("Task created and linked to this objective."))
    return redirect("campaigns:objective_detail", pk=objective.pk)


@login_required
@require_POST
def objective_volunteer(request: HttpRequest, pk: int) -> HttpResponse:
    """A pilot opts in to help with a ``help_wanted`` objective (``can_view`` + member).

    Self-service: creates a self-assigned linked task and records an activity row the owner sees
    in their workspace (doc 10 §6.5–§6.6). No status change, no officer approval."""
    objective = _objective_for(request, pk)
    if not rbac.has_role(request.user, rbac.ROLE_MEMBER):
        raise PermissionDenied(_("Members only."))
    fallback = reverse("campaigns:objective_detail", args=[objective.pk])
    if not objective.help_wanted:
        messages.error(request, _("This objective is not currently asking for help."))
        return _back(request, fallback)
    services.volunteer_for_objective(objective, request.user)
    messages.success(request, _("You're on it — added to your tasks."))
    return _back(request, fallback)


# --------------------------------------------------------------------------- #
#  Evidence (doc 04 §9, doc 10 §6.3)
# --------------------------------------------------------------------------- #
def _evidence_default_url(campaign, kind, attached_id) -> str:
    """The page an evidence action falls back to (a URL path for ``_back``/``redirect``)."""
    from django.urls import reverse

    if kind == EvidenceKind.OBJECTIVE:
        return reverse("campaigns:objective_detail", args=[attached_id])
    if kind == EvidenceKind.MILESTONE:
        return reverse("campaigns:milestone_edit", args=[attached_id])
    return reverse("campaigns:detail", args=[campaign.pk])


@login_required
@require_POST
def evidence_create(request: HttpRequest, pk: int) -> HttpResponse:
    """Attach link/note evidence to a campaign, objective or milestone (doc 04 §9).

    Gate: the attached object's owner / workstream lead / manage capability. A ``url`` is
    optional but must be ``https://`` when present; a ``note`` is required when ``url`` is blank.
    Stored as text — the template renders links ``rel="noopener noreferrer nofollow"``."""
    campaign = _campaign_for_view(request, pk)
    kind = (request.POST.get("attached_kind") or "").strip()
    if kind not in EvidenceKind.values:
        raise Http404(_("Unknown evidence target."))
    attached_id = _int(request.POST.get("attached_id"), 0) or 0
    if kind == EvidenceKind.CAMPAIGN:
        attached_id = campaign.pk
    elif kind == EvidenceKind.OBJECTIVE:
        if not campaign.objectives.filter(pk=attached_id).exists():
            raise Http404(_("No such objective."))
    elif not campaign.milestones.filter(pk=attached_id).exists():
        raise Http404(_("No such milestone."))

    if not services.can_attach_evidence(request.user, campaign, kind, attached_id):
        raise PermissionDenied(_("You cannot attach evidence here."))

    url = (request.POST.get("url") or "").strip()
    note = (request.POST.get("note") or "").strip()
    fallback = _evidence_default_url(campaign, kind, attached_id)
    if url and not url.lower().startswith("https://"):
        messages.error(request, _("Evidence links must start with https://."))
        return redirect(fallback)
    if not url and not note:
        messages.error(request, _("Add a link or a note."))
        return redirect(fallback)

    CampaignEvidence.objects.create(
        campaign=campaign, attached_kind=kind, attached_id=attached_id,
        url=url[:400], note=note[:400], added_by=request.user,
    )
    services.record_activity(
        campaign, request.user, "evidence.added", target_kind=kind, target_id=attached_id,
        after={"url": url[:200], "note": note[:100]},
    )
    messages.success(request, _("Evidence attached."))
    return _back(request, fallback)


@login_required
@require_POST
def evidence_delete(request: HttpRequest, pk: int) -> HttpResponse:
    """Remove evidence — its author or manage capability, never an auto-written row, never on an
    archived campaign. Logged with the removed content in ``before`` (doc 04 §9, reconstructable)."""
    ev = get_object_or_404(CampaignEvidence.objects.select_related("campaign"), pk=pk)
    campaign = ev.campaign
    if not services.can_view(request.user, campaign):
        raise Http404(_("No such evidence."))
    if ev.added_by_id is None:
        raise PermissionDenied(_("Automatically-recorded evidence cannot be removed."))
    if not (ev.added_by_id == request.user.pk or services.can_manage(request.user, campaign)):
        raise PermissionDenied(_("You cannot remove this evidence."))
    if campaign.status == Campaign.Status.ARCHIVED:
        raise PermissionDenied(_("Archived campaigns are read-only."))

    kind, attached_id = ev.attached_kind, ev.attached_id
    before = {"url": ev.url[:200], "note": ev.note[:200], "kind": kind, "attached_id": attached_id}
    ev.delete()
    services.record_activity(
        campaign, request.user, "evidence.removed", target_kind=kind, target_id=attached_id,
        before=before,
    )
    audit_log(
        request.user, "campaigns.evidence_removed", target_type="campaign",
        target_id=str(campaign.pk), ip=client_ip(request), metadata=before,
    )
    messages.success(request, _("Evidence removed."))
    return _back(request, _evidence_default_url(campaign, kind, attached_id))


# --------------------------------------------------------------------------- #
#  Officer workspace (doc 10 §6.7)
# --------------------------------------------------------------------------- #
@login_required
def officer_workspace(request: HttpRequest) -> HttpResponse:
    """The working queue for people who run campaigns: everything of mine that needs a decision
    or an update. Officer+ / campaign_lead / campaign owner (``services.workspace_access``)."""
    user = request.user
    if not services.workspace_access(user):
        raise PermissionDenied(_("The campaign workspace is for officers and campaign leads."))
    q = services.workspace_queues(user)
    # Tab order + labels + per-queue empty copy + row kind, built here so the template stays
    # filter-free (doc 10 §6.7 tab bar).
    tabs = [
        ("overdue", _("Overdue"), "objectives", _("Nothing overdue — good.")),
        ("blocked", _("Blocked"), "objectives", _("Nothing blocked.")),
        ("awaiting_verification", _("Awaiting verification"), "objectives", _("Nothing awaiting verification.")),
        ("stale_metrics", _("Stale metrics"), "objectives", _("No stale metrics.")),
        ("my_objectives", _("My objectives"), "objectives", _("You own no live objectives.")),
        ("my_workstreams", _("My workstreams"), "workstreams", _("You lead no workstreams.")),
        ("volunteers", _("Volunteers"), "volunteers", _("No new volunteers.")),
    ]
    workspace_tabs = [
        {"key": key, "label": label, "kind": kind, "empty": empty,
         "items": q.get(key, []), "count": len(q.get(key, []))}
        for key, label, kind, empty in tabs
    ]
    total = sum(t["count"] for t in workspace_tabs)
    is_director = rbac.has_role(user, rbac.ROLE_DIRECTOR)
    awaiting = None
    if is_director:
        vis = services.visible_campaigns(user)
        awaiting = {
            "proposed": list(
                vis.filter(status=Campaign.Status.PROPOSED)
                .select_related("commander").prefetch_related("commander__characters")[:25]
            ),
            "milestones": list(
                Milestone.objects.filter(
                    status=Milestone.MilestoneStatus.READY_FOR_REVIEW, campaign__in=vis,
                ).select_related("campaign")[:25]
            ),
        }
    ctx = {
        "workspace_tabs": workspace_tabs,
        "total": total,
        "awaiting": awaiting,
        "is_director": is_director,
        "now": timezone.now(),
    }
    return render(request, "campaigns/workspace.html", ctx)


# --------------------------------------------------------------------------- #
#  Close-out (doc 04 §11, doc 10 §6.8)
# --------------------------------------------------------------------------- #
def _close_ctx(request, campaign, budget_allowed) -> dict:
    """Context for the close-out form — every non-terminal objective with its resolution control,
    the recognition preview, and the budget line (gated)."""
    user = request.user
    obj_status = Objective.ObjectiveStatus
    terminal = {obj_status.MET, obj_status.MISSED, obj_status.DROPPED}
    objective_rows = []
    open_mandatory = False
    for o in campaign.objectives.select_related("workstream").order_by("sort_order", "id"):
        vm = _objective_vm(o, user)
        is_open = o.status not in terminal
        unverified = (o.status == obj_status.MET and o.requires_verification
                      and o.verified_by_id is None)
        if o.is_mandatory and (is_open or unverified):
            open_mandatory = True
        objective_rows.append({**vm, "is_open": is_open, "unverified": unverified})
    return {
        "campaign": campaign,
        "objective_rows": objective_rows,
        "open_mandatory": open_mandatory,
        "is_director": rbac.has_role(user, rbac.ROLE_DIRECTOR),
        "budget_allowed": budget_allowed,
        "resolution_choices": [
            (obj_status.MET, _("Met")), (obj_status.MISSED, _("Missed")), (obj_status.DROPPED, _("Dropped")),
        ],
        "participation": services.participation_panel(campaign, user),
        "users": _user_choices(),
    }


def _parse_close(request, campaign, budget_allowed) -> dict:
    """Parse the single close-out POST into :func:`services.close_campaign` kwargs."""
    post = request.POST
    obj_status = Objective.ObjectiveStatus
    terminal = {obj_status.MET, obj_status.MISSED, obj_status.DROPPED}
    resolutions: dict = {}
    manual_values: dict = {}
    for obj in campaign.objectives.all():
        rstatus = (post.get(f"resolve_{obj.pk}") or "").strip()
        note = post.get(f"note_{obj.pk}", "")
        if rstatus and obj.status not in terminal:
            resolutions[obj.pk] = {"status": rstatus, "note": note}
        value = (post.get(f"value_{obj.pk}") or "").strip()
        if value and not obj.metric_source:
            manual_values[obj.pk] = (value, note)

    followups = [fid for fid in (_int(v) for v in post.getlist("followup")) if fid]

    recognitions = []
    r_users = post.getlist("rec_user")
    r_categories = post.getlist("rec_category")
    r_points = post.getlist("rec_points")
    r_reasons = post.getlist("rec_reason")
    for i, raw_uid in enumerate(r_users):
        target = _pool_user(raw_uid)
        reason = r_reasons[i] if i < len(r_reasons) else ""
        if target is None or not (reason or "").strip():
            continue
        recognitions.append({
            "user": target,
            "category": r_categories[i] if i < len(r_categories) else "",
            "points": _int(r_points[i] if i < len(r_points) else "", 0) or 0,
            "reason": reason,
        })

    save_template = None
    if post.get("save_template"):
        save_template = {
            "key": post.get("template_name", ""),
            "name": post.get("template_name", ""),
            "description": post.get("template_description", ""),
        }

    spent = post.get("spent_isk")
    return {
        "final_status": (post.get("final_status") or "").strip(),
        "reason": post.get("override_reason") or post.get("reason") or "",
        "resolutions": resolutions,
        "manual_values": manual_values,
        "outcome_summary": post.get("outcome_summary", ""),
        "lessons_learned": post.get("lessons_learned", ""),
        "spent_isk": spent if (budget_allowed and (spent or "").strip()) else None,
        "budget_allowed": budget_allowed,
        "followup_objective_ids": followups,
        "recognitions": recognitions,
        "save_template": save_template,
    }


@login_required
def campaign_close(request: HttpRequest, pk: int) -> HttpResponse:
    """The guided single-POST close-out (doc 04 §11). ``can_manage`` + status ``active``; the
    service validates and applies everything in one transaction."""
    campaign = _campaign_for_view(request, pk)
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot close this campaign."))
    if campaign.status != Campaign.Status.ACTIVE:
        messages.error(request, _("Only an active campaign can be closed."))
        return redirect("campaigns:detail", pk=campaign.pk)
    budget_allowed = services.can_view_budget(request.user, campaign)
    if request.method == "POST":
        try:
            services.close_campaign(campaign, request.user,
                                    **_parse_close(request, campaign, budget_allowed))
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return render(request, "campaigns/close.html", _close_ctx(request, campaign, budget_allowed))
        messages.success(
            request,
            _("Campaign closed — now %(status)s.") % {"status": campaign.get_status_display().lower()},
        )
        return redirect("campaigns:detail", pk=campaign.pk)
    return render(request, "campaigns/close.html", _close_ctx(request, campaign, budget_allowed))


# --------------------------------------------------------------------------- #
#  Recognition (doc 04 §12, doc 10 §6.2)
# --------------------------------------------------------------------------- #
@login_required
def recognition_manage(request: HttpRequest, pk: int) -> HttpResponse:
    """Recognition management + participation for one campaign (``can_manage``). Awards are audited
    and a self-award is blocked unless the awarder is a director (service-enforced)."""
    campaign = _campaign_for_view(request, pk)
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot manage recognition on this campaign."))
    if request.method == "POST":
        target = _pool_user(request.POST.get("user"))
        if target is None:
            messages.error(request, _("Choose a pilot to recognise."))
        else:
            try:
                services.award_recognition(
                    campaign, target, request.user,
                    category=request.POST.get("category", ""),
                    points=_int(request.POST.get("points"), 0) or 0,
                    reason=request.POST.get("reason", ""),
                )
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(request, _("Recognition recorded."))
        return redirect("campaigns:recognition", pk=campaign.pk)
    ctx = {
        "campaign": campaign,
        "participation": services.participation_panel(campaign, request.user),
        "recognitions": list(
            campaign.recognitions.select_related("user", "awarded_by")
            .prefetch_related("user__characters", "awarded_by__characters")[:100]
        ),
        "users": _user_choices(),
    }
    return render(request, "campaigns/recognition.html", ctx)


@login_required
@require_POST
def campaign_save_template(request: HttpRequest, pk: int) -> HttpResponse:
    """Save a terminal campaign's structure as a reusable template (``can_manage``, doc 04 §13)."""
    campaign = _campaign_for_view(request, pk)
    if not services.can_manage(request.user, campaign):
        raise PermissionDenied(_("You cannot save this campaign as a template."))
    if campaign.status not in _TERMINAL_STATUSES:
        messages.error(request, _("Save-as-template is available once a campaign is closed."))
        return redirect("campaigns:detail", pk=campaign.pk)
    try:
        template = services.save_as_template(
            campaign, request.user,
            key=request.POST.get("template_name", ""),
            name=request.POST.get("template_name", ""),
            description=request.POST.get("template_description", ""),
        )
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    else:
        messages.success(request, _("Saved as template “%(name)s”.") % {"name": template.name})
    return _back(request, reverse("campaigns:detail", args=[campaign.pk]))


# --------------------------------------------------------------------------- #
#  Reports & lessons (doc 11 §2.5, §2.6)
# --------------------------------------------------------------------------- #
@login_required
def campaign_report(request: HttpRequest, pk: int) -> HttpResponse:
    """The permanent close-out report — ``can_view`` + terminal status (doc 11 §2.5). Viewing a
    restricted campaign's report is a sensitive-access audit event."""
    campaign = _campaign_for_view(request, pk)
    if campaign.status not in _TERMINAL_STATUSES:
        raise Http404(_("This campaign has no close-out report yet."))
    user = request.user
    can_budget = services.can_view_budget(user, campaign)
    objective_rows = [
        _objective_vm(o, user)
        for o in campaign.objectives.select_related("workstream", "owner", "verified_by")
        .prefetch_related("verified_by__characters").order_by("sort_order", "id")
    ]
    # Close-out follow-ups: selected STRUCTURALLY, by the ``{pk}:f…`` marker close_out() writes
    # into related_id (Objective.FOLLOWUP_MARKER) — never by title. The follow-up title is written
    # through gettext, so the old title filter ("Follow-up: …" / "Campaign follow-up task") matched
    # nothing for an officer closing the campaign in any non-English locale, and their follow-ups
    # silently vanished from the report; on a restricted campaign it also over-matched, because
    # every linked task there (volunteer, manually added) carries the same neutral title.
    # One query over the campaign's objective-id prefixes (via the related_type/related_id index)
    # instead of a linked_tasks() query per objective (#38, #21).
    followups = []
    obj_ids = [str(vm["obj"].pk) for vm in objective_rows]
    if obj_ids:
        from apps.tasks.models import Task

        followup_match = Q()
        for oid in obj_ids:
            followup_match |= Q(related_id__startswith=Objective.followup_id_prefix(oid))
        followups = list(
            Task.objects.filter(related_type=Objective.RELATED_TYPE)
            .filter(followup_match)
            .order_by("id")
        )

    if campaign.visibility == Campaign.Visibility.RESTRICTED:
        audit_log(
            user, "campaigns.report_viewed", target_type="campaign", target_id=str(campaign.pk),
            ip=client_ip(request), metadata={"status": campaign.status},
        )
    ctx = {
        "campaign": campaign,
        "can_budget": can_budget,
        "objective_rows": objective_rows,
        "milestones": campaign.milestones.order_by("sort_order", "due_at", "id"),
        "risks": campaign.risks.order_by("-severity", "id"),
        "issues": campaign.issues.select_related("objective").order_by("-created_at"),
        "participation": services.participation_panel(campaign, user),
        "followups": followups,
        "worst_reason": _worst_reason(campaign),
        "staging_visible": _staging_visible(user, campaign),
    }
    return render(request, "campaigns/report.html", ctx)


_LESSONS_OUTCOMES = [
    (Campaign.Status.COMPLETED, _l("Completed")),
    (Campaign.Status.FAILED, _l("Failed")),
    (Campaign.Status.CANCELLED, _l("Cancelled")),
    (Campaign.Status.ARCHIVED, _l("Archived")),
]


@login_required
def lessons_library(request: HttpRequest) -> HttpResponse:
    """Cross-campaign lessons-learned library — officer+ over ``visible_campaigns`` (doc 11 §2.6)."""
    user = request.user
    if not rbac.has_role(user, rbac.ROLE_OFFICER):
        raise PermissionDenied(_("Lessons learned are a leadership retrospection tool (officer+)."))
    base = (
        services.visible_campaigns(user)
        .filter(status__in=_TERMINAL_STATUSES).exclude(lessons_learned="")
    )
    f_category = (request.GET.get("category") or "").strip()
    f_outcome = (request.GET.get("outcome") or "").strip()
    f_tag = (request.GET.get("tag") or "").strip()
    qs = base
    if f_category in Campaign.Category.values:
        qs = qs.filter(category=f_category)
    if f_outcome in Campaign.Status.values:
        qs = qs.filter(status=f_outcome)
    if f_tag:
        qs = qs.filter(tags__contains=[f_tag])
    qs = (
        qs.select_related("commander").prefetch_related("commander__characters")
        .order_by("-actual_end_at", "-updated_at").distinct()
    )
    page_obj = _page(request, qs, 25)
    ctx = {
        "page_obj": page_obj,
        "base_qs": _base_qs(request),
        "categories": Campaign.Category.choices,
        "outcomes": _LESSONS_OUTCOMES,
        "f_category": f_category,
        "f_outcome": f_outcome,
        "f_tag": f_tag,
        "active_filters": any([f_category, f_outcome, f_tag]),
    }
    return render(request, "campaigns/lessons.html", ctx)


# --------------------------------------------------------------------------- #
#  Timeline (doc 10 §6.9)
# --------------------------------------------------------------------------- #
def _timeline_events(campaign) -> list[dict]:
    """Chronological campaign events for the timeline (doc 10 §6.9): start, milestones, objective
    due dates, linked operations (resolved defensively), close and target-end markers."""
    now = timezone.now()
    events: list[dict] = []
    if campaign.start_at:
        events.append({"kind": "start", "date": campaign.start_at, "title": _("Campaign start")})
    for ms in campaign.milestones.select_related("workstream").order_by("due_at", "id"):
        if ms.due_at:
            events.append({
                "kind": "milestone", "date": ms.due_at, "title": ms.title,
                "status": ms.status, "status_label": ms.get_status_display(),
                "overdue": ms.due_at < now and ms.status not in (
                    Milestone.MilestoneStatus.DONE, Milestone.MilestoneStatus.MISSED),
                "url": reverse("campaigns:milestone_edit", args=[ms.pk]),
            })
    obj_terminal = {Objective.ObjectiveStatus.MET, Objective.ObjectiveStatus.MISSED,
                    Objective.ObjectiveStatus.DROPPED}
    for o in campaign.objectives.order_by("due_at", "id"):
        if o.due_at:
            events.append({
                "kind": "objective", "date": o.due_at, "title": o.title,
                "overdue": o.due_at < now and o.status not in obj_terminal,
                "url": reverse("campaigns:objective_detail", args=[o.pk]),
            })
    op_ids = list(campaign.linked_operations.values_list("operation_id", flat=True))
    if op_ids:
        from apps.operations.models import Operation

        ops = {o.pk: o for o in Operation.objects.filter(pk__in=op_ids)}
        for oid in op_ids:
            op = ops.get(oid)
            events.append({
                "kind": "operation",
                "date": getattr(op, "target_at", None) if op else None,
                "title": op.name if op else _("Operation #%(id)s (removed)") % {"id": oid},
                "removed": op is None,
            })
    if campaign.actual_end_at:
        events.append({"kind": "close", "date": campaign.actual_end_at,
                       "title": _("Campaign %(status)s") % {"status": campaign.get_status_display().lower()}})
    if campaign.target_end_at:
        events.append({"kind": "target_end", "date": campaign.target_end_at, "title": _("Target end")})

    events.sort(key=lambda e: (e["date"] is None, e["date"] or now))
    return events


@login_required
def campaign_timeline(request: HttpRequest, pk: int) -> HttpResponse:
    """The campaign timeline/roadmap (doc 10 §6.9) — ``can_view``. A CSS strip plus the canonical
    chronological event list (the accessible, mobile-first view)."""
    campaign = _campaign_for_view(request, pk)
    now = timezone.now()
    today_pct = None
    if campaign.start_at and campaign.target_end_at and campaign.target_end_at > campaign.start_at:
        span = (campaign.target_end_at - campaign.start_at).total_seconds()
        today_pct = max(0, min(100, int((now - campaign.start_at).total_seconds() / span * 100)))
    ctx = {
        "campaign": campaign,
        "events": _timeline_events(campaign),
        "today_pct": today_pct,
        "now": now,
    }
    return render(request, "campaigns/timeline.html", ctx)


@login_required
def campaign_activity(request: HttpRequest, pk: int) -> HttpResponse:
    """The full campaign activity stream, paged (doc 10 line 199) — ``can_view``. The detail page
    shows the latest 20; this is the complete history, 50 per page (newest first)."""
    campaign = _campaign_for_view(request, pk)
    # ``actor__characters`` prefetched: the stream renders each actor's display_name (doc 10 line 199).
    qs = campaign.activity.select_related("actor").prefetch_related(
        "actor__characters"
    )  # model default ordering is -created_at, -id
    ctx = {
        "campaign": campaign,
        "page_obj": _page(request, qs, 50),
        "base_qs": _base_qs(request),
    }
    return render(request, "campaigns/activity.html", ctx)
