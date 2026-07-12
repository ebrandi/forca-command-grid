"""Native admin console for CEO/Directors — replaces the Django /admin for the
day-to-day corp configuration jobs (roles, doctrines, settings, maintenance).

Everything here is RBAC-gated and audit-logged; nothing exposes platform-level
superuser actions (that stays in Django admin).
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _t
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

from apps.doctrines import esi_fits
from apps.doctrines.fitparser import parse_eft
from apps.doctrines.killmail_import import eft_from_killmail
from apps.doctrines.models import (
    MAX_PER_PAGE,
    MIN_PER_PAGE,
    Doctrine,
    DoctrineCategory,
    DoctrineDisplayConfig,
    DoctrineFit,
    clamp_per_page,
)
from apps.doctrines.services import create_fit, imported_category, name_conflict
from apps.identity.models import RoleAssignment, RoleChangeRequest
from apps.killboard.models import Killmail
from apps.onboarding.models import GlossaryTerm
from apps.sde.models import SdeType
from apps.sso.services import ensure_role
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

User = get_user_model()
log = logging.getLogger("forca.admin_audit")

# Roles a Director may grant/revoke from the UI. `admin` is platform-level and
# stays in Django admin so the app can never mint a superuser.
# Rank tiers first, then the lateral capability roles (4.16) a director can delegate
# without granting officer-wide authority.
_MANAGEABLE_ROLES = [rbac.ROLE_MEMBER, rbac.ROLE_OFFICER, rbac.ROLE_DIRECTOR,
                     rbac.ROLE_RECRUITER, rbac.ROLE_FC]


# --- Hub ---------------------------------------------------------------------
@login_required
@role_required(rbac.ROLE_OFFICER)
def console_hub(request: HttpRequest) -> HttpResponse:
    # Officer-accessible: the hub is the single home for every management task.
    # Director-only cards are hidden in the template via the ``is_director`` flag
    # (from core.context.roles); each destination view still enforces its own role.
    return render(request, "admin_audit/console/hub.html", {
        "members": User.objects.count(),
        "doctrines": Doctrine.objects.count(),
        "glossary": GlossaryTerm.objects.count(),
    })


# --- Feature & service visibility --------------------------------------------
# Member services (Freight, Buyback, Corp Store) have a richer audience setting
# than a simple on/off — leadership picks who can see and use each one. The
# audience values are shared across the three services (apps.store.models.Audience);
# we relabel them here service-neutrally so the one-stop page reads consistently.
_SERVICE_AUDIENCE_CHOICES = [
    ("disabled", _t("Off — hidden from everyone")),
    ("corp", _t("Corp members only")),
    ("alliance", _t("Corp & alliance members")),
    ("public", _t("Public — anyone")),
]
_VALID_AUDIENCE = {value for value, _ in _SERVICE_AUDIENCE_CHOICES}


def _member_services():
    """The three member services as ``(key, label, settings_url, services_module, config_getter)``.

    Each service keeps a single active config row carrying its ``audience`` — the
    store/buyback expose it via ``active_config``, logistics via ``active_rate_card``.
    """
    from apps.buyback import services as buyback_services
    from apps.logistics import services as logistics_services
    from apps.store import services as store_services

    return [
        ("freight", "Freight service", "logistics:rates", logistics_services,
         logistics_services.active_rate_card),
        ("buyback", "Buyback service", "buyback:config", buyback_services,
         buyback_services.active_config),
        ("store", "Corp Store", "store:config", store_services,
         store_services.active_config),
    ]


def _apply_service_audience(request: HttpRequest) -> list[str]:
    """Persist any changed member-service audience selects; return a change log."""
    changed: list[str] = []
    for key, label, _url, module, get_config in _member_services():
        chosen = request.POST.get(f"audience:{key}")
        if chosen not in _VALID_AUDIENCE:
            continue  # not submitted or tampered — leave it alone
        config = get_config()
        if config.audience != chosen:
            config.audience = chosen
            config.save(update_fields=["audience"])
            module.invalidate_audience_cache()  # nav + access checks pick it up at once
            changed.append(f"{label}={chosen}")
    return changed


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def features(request: HttpRequest) -> HttpResponse:
    """One-stop visibility control: member features on/off + per-service audience.

    Default is everything enabled. Features are a simple on/off; the three member
    services additionally choose who sees them (off / corp / corp & alliance / public).
    """
    from itertools import groupby

    from core.features import (
        AUDIENCE_FEATURES,
        FEATURES,
        FEATURES_BY_KEY,
        disabled_set,
        feature_audience,
        set_disabled,
        set_feature_audiences,
    )

    # Plain on/off features are everything except the audience-controlled ones, which
    # render as their own 4-state dropdowns (like the member services).
    plain = [f for f in FEATURES if f.key not in AUDIENCE_FEATURES]

    if request.method == "POST":
        # A checked box = enabled; anything not posted is disabled.
        enabled = set(request.POST.getlist("feature"))
        disabled = [f.key for f in plain if f.key not in enabled]
        set_disabled(disabled, user=request.user)
        audience_changes = set_feature_audiences(
            {key: request.POST.get(f"feature_audience:{key}") for key in AUDIENCE_FEATURES},
            user=request.user,
        )
        service_changes = _apply_service_audience(request)
        audit_log(request.user, "features.updated", ip=client_ip(request),
                  metadata={"disabled": sorted(disabled),
                            "feature_audiences": audience_changes,
                            "services": service_changes})
        messages.success(request, _t("Service & feature availability updated."))
        return redirect("admin_audit:features")

    off = disabled_set()
    groups = [
        {"name": name, "features": [{"f": f, "enabled": f.key not in off} for f in items]}
        for name, items in groupby(plain, key=lambda f: f.group)
    ]
    audience_features = [
        {"key": key, "label": FEATURES_BY_KEY[key].label,
         "description": FEATURES_BY_KEY[key].description, "audience": feature_audience(key)}
        for key in AUDIENCE_FEATURES
    ]
    services = [
        {"key": key, "label": label, "url": url, "audience": module.current_audience()}
        for key, label, url, module, _get_config in _member_services()
    ]
    return render(request, "admin_audit/console/features.html", {
        "groups": groups,
        "audience_features": audience_features,
        "services": services,
        "audience_choices": _SERVICE_AUDIENCE_CHOICES,
    })


# --- Compliance & inactivity -------------------------------------------------
@login_required
@role_required(rbac.ROLE_DIRECTOR)
def compliance(request: HttpRequest) -> HttpResponse:
    """Who hasn't linked, who's missing a baseline scope, and who's gone inactive."""
    from apps.corporation.compliance import DEFAULT_INACTIVE_DAYS, compliance_report

    try:
        days = max(1, min(365, int(request.GET.get("inactive_days") or DEFAULT_INACTIVE_DAYS)))
    except (TypeError, ValueError):
        days = DEFAULT_INACTIVE_DAYS
    return render(request, "admin_audit/console/compliance.html", {
        "report": compliance_report(inactive_days=days),
    })


# --- Data retention (compliance config) --------------------------------------
# Which data classes the daily enforce_retention beat task actually prunes today.
# The others are stored so leadership can set intent, but are flagged as not-yet-wired
# so a configured window isn't mistaken for active enforcement.
_RETENTION_ENFORCED = {"skill_snapshot", "market_snapshot", "audit"}


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def retention_settings(request: HttpRequest) -> HttpResponse:
    """Leadership-tunable data-retention windows + on-member-leave policy per data class.

    These rows drive the daily ``admin_audit.enforce_retention`` beat task. Director-only:
    they govern how long corp/pilot data is kept, which is a compliance decision.
    """
    from .models import DataRetentionPolicy

    policies = [
        DataRetentionPolicy.objects.get_or_create(
            data_class=dc,
            defaults={
                "retention_days": 365,
                "on_member_leave": DataRetentionPolicy.OnLeave.DELETE,
                "active": True,
            },
        )[0]
        for dc, _ in DataRetentionPolicy.DataClass.choices
    ]
    if request.method == "POST":
        valid_leave = set(DataRetentionPolicy.OnLeave.values)
        for policy in policies:
            dc = policy.data_class
            raw_days = (request.POST.get(f"days:{dc}") or "").strip()
            try:
                days = int(raw_days)
                # keep current on blank/0/garbage — never silently delete-all; upper bound
                # stays well under the 32-bit PositiveIntegerField limit (no overflow 500).
                if 1 <= days <= 36500:
                    policy.retention_days = days
            except (TypeError, ValueError):
                pass
            leave = request.POST.get(f"on_leave:{dc}")
            if leave in valid_leave:
                policy.on_member_leave = leave
            policy.active = request.POST.get(f"active:{dc}") == "on"
            policy.save(update_fields=["retention_days", "on_member_leave", "active", "updated_at"])
        # Arm switch for the destructive on-member-leave sweep (off by default: report-only).
        from .models import AppSetting
        from .services import _ON_LEAVE_ARMED_KEY

        armed = request.POST.get("on_leave_armed") == "on"
        AppSetting.objects.update_or_create(
            key=_ON_LEAVE_ARMED_KEY, defaults={"value": {"armed": armed}, "updated_by": request.user}
        )
        audit_log(
            request.user, "retention.policy.update", target_type="data_retention_policy",
            ip=client_ip(request),
            metadata={"classes": [p.data_class for p in policies], "on_leave_armed": armed},
        )
        messages.success(request, _t("Data-retention policy saved."))
        return redirect("admin_audit:retention_settings")
    for policy in policies:
        policy.enforced = policy.data_class in _RETENTION_ENFORCED
    from .models import AppSetting
    from .services import _ON_LEAVE_REPORT_KEY, on_leave_armed

    report_row = AppSetting.objects.filter(key=_ON_LEAVE_REPORT_KEY).first()
    return render(request, "admin_audit/console/retention_settings.html", {
        "policies": policies,
        "on_leave_choices": DataRetentionPolicy.OnLeave.choices,
        "on_leave_armed": on_leave_armed(),
        "leave_report": report_row.value if report_row else None,
    })


# --- Members & roles ---------------------------------------------------------
@login_required
@role_required(rbac.ROLE_DIRECTOR)
def members(request: HttpRequest) -> HttpResponse:
    users = (
        User.objects.prefetch_related("role_assignments__role", "characters")
        .order_by("username")
    )
    from apps.impersonation.policy import can_impersonate

    rows = []
    for u in users:
        main = next((c for c in u.characters.all() if c.is_main), None) or next(iter(u.characters.all()), None)
        rows.append({
            "user": u,
            "main": main,
            "roles": set(u.role_keys),
            # Whether the current director may open the site as this pilot (read-only,
            # audited). No extra query per row: the target's rank is read from the
            # role_assignments__role prefetch above (max_role_rank is prefetch-aware).
            "can_impersonate": can_impersonate(request.user, u),
        })
    pending = list(
        RoleChangeRequest.objects.filter(status=RoleChangeRequest.Status.PENDING)
        .select_related("target", "requested_by")
    )
    return render(request, "admin_audit/console/members.html", {
        "rows": rows, "manageable": _MANAGEABLE_ROLES,
        "pending_requests": pending, "me_id": request.user.id,
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def member_audit(request: HttpRequest, user_id: int) -> HttpResponse:
    """A consolidated per-member dossier: characters, doctrine readiness, contributions."""
    from apps.doctrines.services import readiness_summary_for_character
    from apps.pilots.models import ContributionEvent

    target = get_object_or_404(User.objects.prefetch_related("characters"), pk=user_id)
    characters = list(target.characters.all())
    main = next((c for c in characters if c.is_main), characters[0] if characters else None)
    summary = readiness_summary_for_character(main) if main else []
    flyable = sum(1 for r in summary if r["status"] in ("viable", "optimal"))

    char_rows = []
    for c in characters:
        snap = c.skill_snapshots.filter(is_latest=True).first()
        char_rows.append({"char": c, "skills": len(snap.skills) if snap and snap.skills else 0})

    contributions = list(ContributionEvent.objects.filter(user=target)[:15])
    from apps.impersonation.policy import can_impersonate

    audit_log(request.user, "member.audit.view", target_type="user",
              target_id=str(user_id), ip=client_ip(request))
    return render(request, "admin_audit/console/member_audit.html", {
        "target": target, "main": main, "characters": char_rows,
        "flyable": flyable, "doctrine_total": len(summary),
        "near": [r for r in summary if r["status"] == "not_ready"][:8],
        "contributions": contributions,
        "roles": sorted(target.role_keys),
        "can_impersonate": can_impersonate(request.user, target),
    })


def _parse_expiry(raw: str):
    """A ``datetime-local`` grant-expiry (4.17), treated as EVE/UTC. Blank → None (a
    deliberate permanent grant); a non-blank date that's unreadable or already past raises
    ValueError so the caller surfaces an error — a fat-fingered past date must NOT silently
    become a permanent grant (review LOW)."""
    import datetime as _dt

    from django.utils.dateparse import parse_datetime
    raw = (raw or "").strip()
    if not raw:
        return None
    value = parse_datetime(raw)
    if value is None:
        raise ValueError(_t("Couldn't read that expiry date."))
    if timezone.is_naive(value):
        value = value.replace(tzinfo=_dt.UTC)
    if value <= timezone.now():
        raise ValueError(_t("Expiry must be in the future — leave it blank for a permanent grant."))
    return value


def _apply_grant(target, role_key: str, expires_at, granted_by) -> None:
    """Create/refresh the assignment. Updates expiry on an existing grant so a re-grant can
    extend or re-arm it."""
    role = ensure_role(role_key)
    obj, created = RoleAssignment.objects.get_or_create(
        user=target, role=role, defaults={"granted_by": granted_by, "expires_at": expires_at}
    )
    if not created and obj.expires_at != expires_at:
        obj.expires_at = expires_at
        obj.granted_by = granted_by
        obj.save(update_fields=["expires_at", "granted_by", "updated_at"])


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def set_role(request: HttpRequest, user_id: int) -> HttpResponse:
    target = get_object_or_404(User, pk=user_id)
    # One form per member row shares an optional expiry input; the clicked button names the
    # action + role in its value ("grant"/"revoke" = <role key>).
    grant = "grant" in request.POST
    role_key = request.POST.get("grant") if grant else request.POST.get("revoke")
    if role_key not in _MANAGEABLE_ROLES:
        raise PermissionDenied(_t("That role can't be managed here."))
    try:
        expires_at = _parse_expiry(request.POST.get("expires_at", "")) if grant else None
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("admin_audit:members")

    if grant and rbac.requires_dual_control(role_key):
        # Dual control (4.17): don't apply — open a request a *different* director must approve.
        _, created = RoleChangeRequest.objects.get_or_create(
            target=target, role_key=role_key, status=RoleChangeRequest.Status.PENDING,
            defaults={"requested_by": request.user, "expires_at": expires_at,
                      "reason": request.POST.get("reason", "")[:200]},
        )
        if not created:
            messages.info(request, _t("A %(role)s grant for that pilot is already awaiting approval.") % {
                "role": role_key.title()})
            return redirect("admin_audit:members")
        audit_log(request.user, "role.grant_requested", target_type="user", target_id=str(target.id),
                  metadata={"role": role_key}, ip=client_ip(request))
        _notify_role_request(target, role_key, request.user)
        messages.success(request, _t("%(role)s grant requested — a second director must approve it.") % {
            "role": role_key.title()})
        return redirect("admin_audit:members")

    if grant:
        _apply_grant(target, role_key, expires_at, request.user)
        action = "granted"
    else:
        # Never strip the last Director — that would lock everyone out of admin. Count only
        # ACTIVE (non-expired) director grants: since 4.17 director grants can expire, a stale
        # expired row must not inflate the floor and let the last effective director be removed.
        if role_key == rbac.ROLE_DIRECTOR:
            from django.db.models import Q
            directors = (
                RoleAssignment.objects.filter(role__key=rbac.ROLE_DIRECTOR)
                .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
                .count()
            )
            if directors <= 1:
                messages.error(request, _t("Can't remove the last Director — promote someone else first."))
                return redirect("admin_audit:members")
        RoleAssignment.objects.filter(user=target, role__key=role_key).delete()
        action = "revoked"

    audit_log(request.user, f"role.{action}", target_type="user", target_id=str(target.id),
              metadata={"role": role_key}, ip=client_ip(request))
    messages.success(request, _t("%(role)s %(action)s for %(name)s.") % {
        "role": role_key.title(), "action": action, "name": target.first_name or target.username})
    return redirect("admin_audit:members")


def _notify_role_request(target, role_key: str, requester) -> None:
    """DM the director group that a dual-control grant is awaiting a second approval."""
    try:
        from apps.pingboard import services as pingboard
        from apps.pingboard.models import AlertCategory
        who = getattr(target, "display_name", "") or target.get_username()
        by = getattr(requester, "display_name", "") or requester.get_username()
        pingboard.emit_broadcast(
            category=AlertCategory.SYSTEM,
            title="Role grant awaiting approval",
            body=(f"{by} requested a {role_key.title()} grant for {who}. A different director "
                  "must approve it in the Members console."),
            source_service="identity",
            audience={"kind": rbac.ROLE_DIRECTOR},
        )
    except Exception:  # noqa: BLE001 - notification is best-effort; the console surface is the source of truth
        log.exception("role-request notify failed")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def role_request_decide(request: HttpRequest, pk: int) -> HttpResponse:
    """A SECOND director approves or rejects a pending dual-control role grant (4.17).
    Separation of duties: the requester may not decide their own request."""
    from django.db import transaction
    approve = request.POST.get("decision") == "approve"
    # Lock the row + re-check PENDING inside the txn so two directors deciding at once can't
    # leave the grant applied while the record reads "rejected" (review LOW).
    with transaction.atomic():
        req = get_object_or_404(
            RoleChangeRequest.objects.select_for_update(),
            pk=pk, status=RoleChangeRequest.Status.PENDING,
        )
        if approve and req.requested_by_id == request.user.id and not request.user.is_superuser:
            messages.error(request, _t("You can't approve your own role request — another director must."))
            return redirect("admin_audit:members")
        req.decided_by = request.user
        req.decided_at = timezone.now()
        if approve:
            _apply_grant(req.target, req.role_key, req.expires_at, req.requested_by)
            req.status = RoleChangeRequest.Status.APPROVED
            outcome = "approved"
        else:
            req.status = RoleChangeRequest.Status.REJECTED
            outcome = "rejected"
        req.save(update_fields=["status", "decided_by", "decided_at", "updated_at"])
    audit_log(request.user, f"role.request_{outcome}", target_type="user", target_id=str(req.target_id),
              metadata={"role": req.role_key, "request_id": req.id}, ip=client_ip(request))
    messages.success(request, _t("%(role)s grant %(outcome)s for %(name)s.") % {
        "role": req.role_key.title(), "outcome": outcome,
        "name": req.target.first_name or req.target.username})
    return redirect("admin_audit:members")


# --- Doctrine management (officers and up) -----------------------------------
@login_required
@role_required(rbac.ROLE_OFFICER)
def doctrines_admin(request: HttpRequest) -> HttpResponse:
    return render(request, "admin_audit/console/doctrines.html", {
        "doctrines": Doctrine.objects.prefetch_related("fits").order_by("-priority", "name"),
        "categories": DoctrineCategory.objects.all(),
        "statuses": Doctrine.Status.choices,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
def doctrine_settings(request: HttpRequest) -> HttpResponse:
    """Display settings for the pilot-facing Doctrines and Shipyard pages.

    Currently the page size (how many doctrines/ships show per page). Officer-level:
    it lives beside doctrine management and is purely cosmetic. The saved value is
    clamped server-side so a typo can't make a page render thousands of cards."""
    config = DoctrineDisplayConfig.active()
    if request.method == "POST":
        raw = (request.POST.get("per_page") or "").strip()
        try:
            requested = int(raw)
        except ValueError:
            messages.error(request, _t("Enter a whole number."))
            return redirect("admin_audit:doctrine_settings")
        clamped = clamp_per_page(requested)
        config.per_page = clamped
        config.save(update_fields=["per_page", "updated_at"])
        audit_log(request.user, "doctrine.display.config", target_type="doctrine_display_config",
                  target_id=str(config.id), metadata={"per_page": clamped}, ip=client_ip(request))
        if clamped != requested:
            messages.info(request, _t("Page size set to %(clamped)s (kept within %(min)s–%(max)s).") % {
                "clamped": clamped, "min": MIN_PER_PAGE, "max": MAX_PER_PAGE})
        else:
            messages.success(request, _t("Page size set to %(clamped)s per page.") % {"clamped": clamped})
        return redirect("admin_audit:doctrine_settings")
    return render(request, "admin_audit/console/doctrine_settings.html", {
        "config": config, "min_per_page": MIN_PER_PAGE, "max_per_page": MAX_PER_PAGE,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def doctrine_create(request: HttpRequest) -> HttpResponse:
    name = (request.POST.get("name") or "").strip()
    if not name:
        messages.error(request, _t("Give the doctrine a name."))
        return redirect("admin_audit:doctrines")
    category = DoctrineCategory.objects.filter(pk=request.POST.get("category")).first()
    doctrine = Doctrine.objects.create(
        name=name, category=category, description=(request.POST.get("description") or "").strip(),
        priority=int(request.POST.get("priority") or 0), status=Doctrine.Status.ACTIVE,
        created_by=request.user,
    )
    audit_log(request.user, "doctrine.create", target_type="doctrine", target_id=str(doctrine.id),
              metadata={"name": name}, ip=client_ip(request))
    messages.success(request, _t("Doctrine “%(name)s” created — add a fit below.") % {"name": name})
    return redirect("admin_audit:doctrine_edit", pk=doctrine.pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
def doctrine_edit(request: HttpRequest, pk: int) -> HttpResponse:
    from apps.doctrines.models import DoctrineRequirement

    doctrine = get_object_or_404(
        Doctrine.objects.prefetch_related("fits", "fits__requirements"), pk=pk)
    return render(request, "admin_audit/console/doctrine_edit.html", {
        "doctrine": doctrine, "statuses": Doctrine.Status.choices,
        "categories": DoctrineCategory.objects.all(),
        "requirement_kinds": DoctrineRequirement.Kind.choices,
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def doctrine_update(request: HttpRequest, pk: int) -> HttpResponse:
    doctrine = get_object_or_404(Doctrine, pk=pk)
    doctrine.name = (request.POST.get("name") or doctrine.name).strip()
    doctrine.description = (request.POST.get("description") or "").strip()
    doctrine.priority = int(request.POST.get("priority") or doctrine.priority)
    status = request.POST.get("status")
    if status in Doctrine.Status.values:
        doctrine.status = status
    category = request.POST.get("category")
    doctrine.category = DoctrineCategory.objects.filter(pk=category).first() if category else None
    doctrine.save()
    messages.success(request, _t("Doctrine updated."))
    return redirect("admin_audit:doctrine_edit", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def doctrine_delete(request: HttpRequest, pk: int) -> HttpResponse:
    doctrine = get_object_or_404(Doctrine, pk=pk)
    audit_log(request.user, "doctrine.delete", target_type="doctrine", target_id=str(doctrine.id),
              metadata={"name": doctrine.name}, ip=client_ip(request))
    doctrine.delete()
    messages.success(request, _t("Doctrine deleted."))
    return redirect("admin_audit:doctrines")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def fit_add(request: HttpRequest, pk: int) -> HttpResponse:
    doctrine = get_object_or_404(Doctrine, pk=pk)
    eft = (request.POST.get("eft") or "").strip()
    try:
        parsed = parse_eft(eft)
    except ValueError as exc:
        messages.error(request, _t("Couldn't parse the EFT: %(error)s") % {"error": exc})
        return redirect("admin_audit:doctrine_edit", pk=pk)
    if not parsed["ship_type_id"]:
        messages.error(request, _t("Unknown ship “%(ship)s” — check the EFT header.") % {
            "ship": parsed['ship_name']})
        return redirect("admin_audit:doctrine_edit", pk=pk)
    fit = create_fit(
        doctrine, name=parsed["fit_name"], ship_type_id=parsed["ship_type_id"],
        modules=parsed["modules"], eft_text=eft,
        is_cheap_alt=request.POST.get("is_cheap_alt") == "1",
    )
    reqs = fit.skill_requirements.count()
    if parsed["unresolved"]:
        msg = _t("Fit “%(name)s” added (%(count)s skill requirements derived). "
                 "Unresolved lines: %(lines)s.") % {
            "name": fit.name, "count": reqs, "lines": ', '.join(parsed['unresolved'][:5])}
    else:
        msg = _t("Fit “%(name)s” added (%(count)s skill requirements derived).") % {
            "name": fit.name, "count": reqs}
    messages.success(request, msg)
    return redirect("admin_audit:doctrine_edit", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def fit_delete(request: HttpRequest, fit_id: int) -> HttpResponse:
    fit = get_object_or_404(DoctrineFit, pk=fit_id)
    pk = fit.doctrine_id
    fit.delete()
    messages.success(request, _t("Fit removed."))
    return redirect("admin_audit:doctrine_edit", pk=pk)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def requirement_add(request: HttpRequest, fit_id: int) -> HttpResponse:
    """Attach an 'also bring' recommendation (implant/booster/rig/ammo/note) to a fit."""
    from apps.doctrines.models import DoctrineRequirement

    fit = get_object_or_404(DoctrineFit, pk=fit_id)
    kind = request.POST.get("kind")
    if kind not in DoctrineRequirement.Kind.values:
        messages.error(request, _t("Pick a valid requirement type."))
        return redirect("admin_audit:doctrine_edit", pk=fit.doctrine_id)
    type_id = None
    raw_type = (request.POST.get("type_id") or "").strip()
    if raw_type:
        try:
            type_id = int(raw_type)
        except ValueError:
            type_id = None
        # type_id is a 32-bit IntegerField; reject out-of-range so an absurd value can't
        # 500 the insert on Postgres (SQLite would silently accept it).
        if type_id is not None and not (0 < type_id < 2_147_483_647):
            type_id = None
    text = (request.POST.get("text") or "").strip()
    if kind == DoctrineRequirement.Kind.NOTE and not text:
        messages.error(request, _t("A note needs some text."))
        return redirect("admin_audit:doctrine_edit", pk=fit.doctrine_id)
    if kind != DoctrineRequirement.Kind.NOTE and type_id is None:
        messages.error(request, _t("This requirement needs a type id (or record it as a note)."))
        return redirect("admin_audit:doctrine_edit", pk=fit.doctrine_id)
    DoctrineRequirement.objects.create(
        fit=fit, kind=kind, type_id=type_id, text=text,
        is_recommended=request.POST.get("is_recommended") == "on",
    )
    messages.success(request, _t("Requirement added."))
    return redirect("admin_audit:doctrine_edit", pk=fit.doctrine_id)


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def requirement_delete(request: HttpRequest, req_id: int) -> HttpResponse:
    from apps.doctrines.models import DoctrineRequirement

    req = get_object_or_404(DoctrineRequirement.objects.select_related("fit"), pk=req_id)
    pk = req.fit.doctrine_id
    req.delete()
    messages.success(request, _t("Requirement removed."))
    return redirect("admin_audit:doctrine_edit", pk=pk)


# --- Importing fits into doctrines (Director: uses their own ESI token) -------
_MAX_IMPORT = 50  # bound how many fits one apply can create


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def import_fits(request: HttpRequest) -> HttpResponse:
    """List the director's saved ESI fits to import as doctrine fits.

    Reads the character fittings endpoint with the director's own token (ESI has
    no corp-fittings endpoint). If no linked character has granted the fittings
    scope, the page prompts to grant it.
    """
    has_scope = bool(esi_fits.characters_with_fittings_scope(request.user))
    fits = esi_fits.fetch_all_fittings(request.user) if has_scope else []
    return render(request, "admin_audit/console/import_fits.html", {
        "has_scope": has_scope,
        "fits": fits,
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def import_fits_apply(request: HttpRequest) -> HttpResponse:
    """Import each selected saved fit as its own new doctrine.

    Every ticked fit becomes a new Doctrine (filed under the IMPORTED category) with
    one fit. The doctrine takes the saved fit's name unless an optional rename was
    given. Fit data is re-fetched from ESI (never trusted from the form), keyed by
    ``<character_id>:<fitting_id>``; the form only carries the selection + renames.
    Leaders refine the name/category/description, or remove it, on the doctrine page.
    """
    selected = request.POST.getlist("select")
    if not selected:
        messages.error(request, _t("Tick at least one fit to import."))
        return redirect("admin_audit:import_fits")

    category = imported_category()
    available = {
        f"{f['character_id']}:{f['fitting_id']}": f
        for f in esi_fits.fetch_all_fittings(request.user)
    }
    created, last = [], None
    duplicates, conflicts = [], []
    for key in selected[:_MAX_IMPORT]:
        fitting = available.get(key)
        if not fitting:
            continue
        # Optional rename → the doctrine name; blank keeps the saved fit name.
        name = (request.POST.get(f"name:{key}") or "").strip() or fitting["name"]
        # Don't create a second doctrine with a name we already use.
        kind, _existing = name_conflict(name, fitting["ship_type_id"], fitting["modules"])
        if kind == "duplicate":
            duplicates.append(name)
            continue
        if kind == "conflict":
            conflicts.append(name)
            continue
        doctrine = Doctrine.objects.create(
            name=name[:200], category=category, status=Doctrine.Status.ACTIVE,
            description=f"Imported from {fitting['character_name']}'s saved fit.",
            created_by=request.user,
        )
        create_fit(
            doctrine, name=fitting["name"][:200], ship_type_id=fitting["ship_type_id"],
            modules=fitting["modules"],
        )
        created.append(doctrine)
        last = doctrine

    audit_log(request.user, "doctrine.import_fits", target_type="doctrine_category",
              target_id=str(category.id),
              metadata={"created": len(created), "duplicates": len(duplicates),
                        "conflicts": len(conflicts)}, ip=client_ip(request))

    # Report each outcome so the director knows exactly what happened.
    if created:
        messages.success(request, ngettext(
            "Imported %(count)d fit as new doctrines under IMPORTED.",
            "Imported %(count)d fits as new doctrines under IMPORTED.",
            len(created)) % {"count": len(created)})
    if duplicates:
        names = ", ".join(f"“{n}”" for n in duplicates)
        messages.info(request, _t("Already in the library (identical name and fit) — skipped: %(names)s.") % {
            "names": names})
    if conflicts:
        names = ", ".join(f"“{n}”" for n in conflicts)
        messages.error(request, _t("A doctrine with this name already exists but with a "
                                   "different fit — rename it and import again: %(names)s.") % {"names": names})
    if not created and not duplicates and not conflicts:
        messages.error(request, _t("Nothing imported — the selected fits couldn't be read."))

    # Stay on the import page when something needs renaming; otherwise move on.
    if conflicts or not created:
        return redirect("admin_audit:import_fits")
    if len(created) == 1 and not duplicates:
        return redirect("admin_audit:doctrine_edit", pk=last.pk)
    return redirect("admin_audit:doctrines")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def doctrine_from_killmail(request: HttpRequest, killmail_id: int) -> HttpResponse:
    """Fine-tune page to turn a killmail's fit + cargo into a new doctrine.

    GET pre-fills an editable EFT block from the loss (modules and cargo
    consumables alike); the director adjusts it (adds ammo, drops wrecked rigs,
    renames) and POSTs to create the doctrine + fit.
    """
    killmail = get_object_or_404(
        Killmail.objects.prefetch_related("items"), killmail_id=killmail_id
    )
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        eft = (request.POST.get("eft") or "").strip()
        if not name:
            messages.error(request, _t("Give the doctrine a name."))
            return redirect("admin_audit:doctrine_from_killmail", killmail_id=killmail_id)
        try:
            parsed = parse_eft(eft)
        except ValueError as exc:
            messages.error(request, _t("Couldn't parse the fit: %(error)s") % {"error": exc})
            return redirect("admin_audit:doctrine_from_killmail", killmail_id=killmail_id)
        if not parsed["ship_type_id"]:
            messages.error(request, _t("Unknown ship “%(ship)s” — check the first line.") % {
                "ship": parsed['ship_name']})
            return redirect("admin_audit:doctrine_from_killmail", killmail_id=killmail_id)
        category = DoctrineCategory.objects.filter(pk=request.POST.get("category")).first()
        doctrine = Doctrine.objects.create(
            name=name, category=category, status=Doctrine.Status.ACTIVE,
            priority=int(request.POST.get("priority") or 0),
            description=(request.POST.get("description") or "").strip(),
            created_by=request.user,
        )
        fit = create_fit(
            doctrine, name=parsed["fit_name"], ship_type_id=parsed["ship_type_id"],
            modules=parsed["modules"], role=(request.POST.get("role") or "").strip(), eft_text=eft,
        )
        audit_log(request.user, "doctrine.from_killmail", target_type="doctrine",
                  target_id=str(doctrine.id), metadata={"killmail_id": killmail_id},
                  ip=client_ip(request))
        count = fit.skill_requirements.count()
        if parsed["unresolved"]:
            msg = _t("Doctrine “%(name)s” created from killmail (%(count)s skills derived). "
                     "Unresolved: %(lines)s.") % {
                "name": name, "count": count, "lines": ', '.join(parsed['unresolved'][:5])}
        else:
            msg = _t("Doctrine “%(name)s” created from killmail (%(count)s skills derived).") % {
                "name": name, "count": count}
        messages.success(request, msg)
        return redirect("admin_audit:doctrine_edit", pk=doctrine.pk)

    ship_name = (
        SdeType.objects.filter(type_id=killmail.victim_ship_type_id)
        .values_list("name", flat=True).first() or "Ship"
    )
    return render(request, "admin_audit/console/doctrine_from_killmail.html", {
        "killmail": killmail,
        "ship_name": ship_name,
        "suggested_name": f"{ship_name} doctrine",
        "eft": eft_from_killmail(killmail),
        "categories": DoctrineCategory.objects.all(),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def category_create(request: HttpRequest) -> HttpResponse:
    label = (request.POST.get("label") or "").strip()
    key = (request.POST.get("key") or label.lower().replace(" ", "-")).strip()
    if label and key:
        DoctrineCategory.objects.get_or_create(key=key, defaults={"label": label})
        messages.success(request, _t("Category “%(label)s” added.") % {"label": label})
    return redirect("admin_audit:doctrines")


# --- Onboarding content (milestones + glossary) -------------------------------
# What the engine can verify by itself; anything else is checked off by the
# pilot. Labels are what leaders read in the check-type dropdown.
_MILESTONE_CHECKS = [
    ("manual", _t("Manual — pilot checks it off")),
    ("linked", _t("Auto — a character is linked")),
    ("corp_member", _t("Auto — verified corp member")),
    ("skills_imported", _t("Auto — skills imported")),
    ("scopes", _t("Auto — ESI scopes granted")),
    ("skill_min", _t("Auto — skill trained to level")),
    ("doctrine_ready", _t("Auto — can fly a specific doctrine")),
    ("doctrine_any", _t("Auto — can fly ANY active doctrine")),
]


@login_required
@role_required(rbac.ROLE_OFFICER)
def content(request: HttpRequest) -> HttpResponse:
    from apps.onboarding.models import OnboardingMilestone
    from apps.onboarding.services import is_manual

    milestones = list(OnboardingMilestone.objects.order_by("sort_order", "id"))
    for m in milestones:
        m.check_type = (m.criteria or {}).get("type") or "manual"
        m.is_manual = is_manual(m.criteria)
        m.doctrine_id_param = (m.criteria or {}).get("doctrine_id")
        m.skill_type_id_param = (m.criteria or {}).get("skill_type_id")
        m.skill_level_param = (m.criteria or {}).get("level")
        m.scopes_param = ", ".join((m.criteria or {}).get("scopes") or [])
    return render(request, "admin_audit/console/content.html", {
        "glossary": GlossaryTerm.objects.order_by("term"),
        "milestones": milestones,
        "categories": OnboardingMilestone.Category.choices,
        "check_types": _MILESTONE_CHECKS,
        "doctrines": Doctrine.objects.filter(status=Doctrine.Status.ACTIVE).order_by("name"),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def milestone_save(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    """Create or update an onboarding milestone from the console's inline form."""
    from django.utils.text import slugify

    from apps.onboarding.models import OnboardingMilestone

    title = (request.POST.get("title") or "").strip()
    if not title:
        messages.error(request, _t("A milestone needs a title."))
        return redirect("admin_audit:content")

    category = request.POST.get("category")
    if category not in OnboardingMilestone.Category.values:
        category = OnboardingMilestone.Category.ACCOUNT

    check = request.POST.get("check")
    if check not in {key for key, _ in _MILESTONE_CHECKS}:
        check = "manual"
    criteria: dict = {}
    if check in ("linked", "corp_member", "skills_imported", "doctrine_any"):
        criteria = {"type": check}
    elif check == "doctrine_ready":
        try:
            doctrine_id = int(request.POST.get("doctrine_id") or "")
        except ValueError:
            messages.error(request, _t("Pick the doctrine this milestone checks."))
            return redirect("admin_audit:content")
        if not Doctrine.objects.filter(pk=doctrine_id).exists():
            messages.error(request, _t("That doctrine no longer exists."))
            return redirect("admin_audit:content")
        criteria = {"type": "doctrine_ready", "doctrine_id": doctrine_id}
    elif check == "skill_min":
        try:
            skill_type_id = int(request.POST.get("skill_type_id") or "")
            level = min(5, max(1, int(request.POST.get("skill_level") or "1")))
        except ValueError:
            messages.error(request, _t("Skill milestones need a skill type id and a level (1–5)."))
            return redirect("admin_audit:content")
        criteria = {"type": "skill_min", "skill_type_id": skill_type_id, "level": level}
    elif check == "scopes":
        scopes = [s.strip() for s in (request.POST.get("scopes") or "").split(",") if s.strip()]
        if not scopes:
            messages.error(request, _t("List at least one ESI scope (comma-separated)."))
            return redirect("admin_audit:content")
        criteria = {"type": "scopes", "scopes": scopes}

    try:
        sort_order = int(request.POST.get("sort_order") or "0")
    except ValueError:
        sort_order = 0
    fields = {
        "title": title[:200],
        "description": (request.POST.get("description") or "").strip(),
        "category": category,
        "criteria": criteria,
        "url": (request.POST.get("url") or "").strip()[:300],
        "sort_order": sort_order,
        "active": request.POST.get("active") == "on",
    }
    if pk:
        obj = OnboardingMilestone.objects.filter(pk=pk).first()
        if obj is None:
            messages.error(request, _t("That milestone no longer exists."))
            return redirect("admin_audit:content")
        for key, value in fields.items():
            setattr(obj, key, value)
        obj.save()
        action = "update"
    else:
        base = slugify(title)[:56] or "milestone"
        key, n = base, 2
        while OnboardingMilestone.objects.filter(key=key).exists():
            key, n = f"{base}-{n}", n + 1
        obj = OnboardingMilestone.objects.create(key=key, **fields)
        action = "create"
    audit_log(request.user, f"onboarding.milestone.{action}",
              target_type="onboarding_milestone", target_id=str(obj.pk), ip=client_ip(request))
    messages.success(request, _t("Milestone “%(title)s” saved.") % {"title": obj.title})
    return redirect("admin_audit:content")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def milestone_delete(request: HttpRequest, pk: int) -> HttpResponse:
    from apps.onboarding.models import OnboardingMilestone

    get_object_or_404(OnboardingMilestone, pk=pk).delete()
    audit_log(request.user, "onboarding.milestone.delete",
              target_type="onboarding_milestone", target_id=str(pk), ip=client_ip(request))
    messages.success(request, _t("Milestone removed — pilots' progress rows went with it."))
    return redirect("admin_audit:content")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def glossary_create(request: HttpRequest) -> HttpResponse:
    term = (request.POST.get("term") or "").strip()
    definition = (request.POST.get("definition") or "").strip()
    if term and definition:
        GlossaryTerm.objects.update_or_create(term=term, defaults={"definition": definition})
        messages.success(request, _t("Glossary term “%(term)s” saved.") % {"term": term})
    else:
        messages.error(request, _t("Both term and definition are required."))
    return redirect("admin_audit:content")


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def glossary_delete(request: HttpRequest, term_id: int) -> HttpResponse:
    get_object_or_404(GlossaryTerm, pk=term_id).delete()
    messages.success(request, _t("Glossary term removed."))
    return redirect("admin_audit:content")


# --- Settings & maintenance --------------------------------------------------
# Maintenance jobs a Director can trigger by name (enqueued on the worker).
_MAINTENANCE_TASKS = {
    "recommendations": ("recommendations.run", _t("Recommendation engine")),
    "market_history": ("market.sync_history", _t("Market history refresh")),
    "corp_assets": ("stockpile.sync_corp_assets", _t("Corp asset sync")),
    "personal_assets": ("stockpile.sync_personal_assets", _t("Personal asset sync")),
    "killmails": ("killboard.discover_all_member_killmails", _t("Killmail discovery")),
    "skills": ("characters.sync_all_member_skills", _t("Member skill sync")),
    "capsuleer_reconcile": ("capsuleer.reconcile_progress", _t("Capsuleer Path reconcile")),
}


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def settings_view(request: HttpRequest) -> HttpResponse:
    from django.conf import settings as dj

    from apps.market.models import MarketLocation
    return render(request, "admin_audit/console/settings.html", {
        "home_corp_id": dj.FORCA_HOME_CORP_ID,
        "locations": MarketLocation.objects.order_by("name"),
        "tasks": [(k, label) for k, (_, label) in _MAINTENANCE_TASKS.items()],
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def run_maintenance(request: HttpRequest, action: str) -> HttpResponse:
    entry = _MAINTENANCE_TASKS.get(action)
    if not entry:
        raise PermissionDenied(_t("Unknown maintenance action."))
    task_name, label = entry
    from config.celery import app as celery_app

    celery_app.send_task(task_name)
    audit_log(request.user, "maintenance.run", target_type="task", target_id=task_name,
              ip=client_ip(request))
    messages.success(request, _t("%(label)s queued — results appear under Integrations as they finish.") % {
        "label": label})
    return redirect("admin_audit:settings")


@login_required
@role_required(rbac.ROLE_OFFICER)
def contribution_weights(request: HttpRequest) -> HttpResponse:
    """Leadership: tune how each contribution kind scores points."""
    from apps.pilots.forms import ContributionWeightsForm
    from apps.pilots.weights import active_weights

    weights = active_weights()
    if request.method == "POST":
        # Freeze every completed Hall-of-Fame month at the CURRENT (pre-change) weights
        # BEFORE the form mutates them, so retuning never reshuffles past boards (4.15).
        # Captured now because Django's is_valid() writes cleaned values onto the instance.
        from apps.pilots.weights import weights_snapshot_dict
        pre_change = weights_snapshot_dict(weights)
        form = ContributionWeightsForm(request.POST, instance=weights)
        if form.is_valid():
            from apps.pilots.halloffame import freeze_completed_months, invalidate_cache
            from apps.pilots.weights import weights_from_snapshot
            freeze_completed_months(weights=weights_from_snapshot(pre_change))
            form.save()
            invalidate_cache()  # weights changed → recompute the Hall of Fame
            audit_log(request.user, "contribution.weights_update",
                      target_type="contribution_weights", target_id=str(weights.pk),
                      ip=client_ip(request))
            messages.success(request, _t("Contribution weights updated."))
            return redirect("admin_audit:contribution_weights")
    else:
        form = ContributionWeightsForm(instance=weights)
    return render(request, "admin_audit/console/contribution_weights.html",
                  {"form": form, "weights": weights})
