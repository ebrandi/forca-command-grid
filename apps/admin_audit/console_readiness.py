"""Readiness Platform configuration pages (admin console, Director-gated).

Per design doc 07, readiness *configuration* lives in the ``admin_audit:`` namespace
(the leadership/pilot readiness pages keep ``readiness:``). These views follow the
console contract exactly — ``@login_required`` + ``@role_required(ROLE_DIRECTOR)``,
render to ``templates/admin_audit/console/readiness/*.html``, and on every write
funnel through the single ``apps.readiness.config`` writer (validate → persist →
version-bump → cache-bust) then ``audit_log``. Phase 1 ships the **Dimensions &
weights** page + per-domain reset; later phases add the remaining nine pages.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.readiness import config
from apps.readiness.engine import registry
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required


def _dimension_rows() -> list[dict]:
    """Each registered dimension joined to its config, in registry order.

    Driving the rows off the registry (not the config doc) means the page always
    shows exactly the dimensions the engine can actually score — new providers
    appear automatically, with their configured (or default) weight/thresholds.
    """
    cfg = config.get("dimensions")
    rows = []
    for provider in registry.providers():
        entry = cfg.get(provider.key, {})
        thresholds = entry.get("thresholds", {})
        rows.append({
            "key": provider.key,
            "label": getattr(provider, "label", provider.key.title()),
            "enabled": entry.get("enabled", True),
            "weight": entry.get("weight", 1.0),
            "amber": thresholds.get("amber", 60),
            "red": thresholds.get("red", 40),
        })
    return rows


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def readiness_dimensions(request: HttpRequest) -> HttpResponse:
    """Tune which dimensions score, their weight in the index, and amber/red bands."""
    if request.method == "POST":
        return _save_dimensions(request)
    return render(request, "admin_audit/console/readiness/dimensions.html", {
        "rows": _dimension_rows(),
        "meta": config.meta("dimensions"),
    })


def _save_dimensions(request: HttpRequest) -> HttpResponse:
    # Fall back to the current stored value for any field absent from the POST, so a
    # numeric field is never submitted blank (and unrelated dimensions are preserved).
    current = config.get("dimensions")
    doc: dict[str, dict] = {}
    for provider in registry.providers():
        key = provider.key
        cur = current.get(key, {})
        cur_thr = cur.get("thresholds", {})
        doc[key] = {
            "enabled": request.POST.get(f"dim_{key}_enabled") == "on",
            "weight": (request.POST.get(f"dim_{key}_weight") or "").strip() or cur.get("weight", 1.0),
            "thresholds": {
                "amber": (request.POST.get(f"dim_{key}_amber") or "").strip() or cur_thr.get("amber", 60),
                "red": (request.POST.get(f"dim_{key}_red") or "").strip() or cur_thr.get("red", 40),
            },
        }
    try:
        config.set("dimensions", doc, user=request.user)
    except config.ConfigError as exc:
        # No partial write — the stored doc and version are untouched.
        messages.error(request, str(exc))
        return redirect("admin_audit:readiness_dimensions")
    audit_log(request.user, "readiness.config.update",
              target_type="readiness_config", target_id="dimensions",
              metadata={"domain": "dimensions"}, ip=client_ip(request))
    messages.success(request, "Dimensions & weights saved.")
    return redirect("admin_audit:readiness_dimensions")


# Domains that have an admin page to redirect back to after a reset.
_RESET_RETURN = {
    "dimensions": "admin_audit:readiness_dimensions",
    "finance": "admin_audit:readiness_finance",
    "srp": "admin_audit:readiness_srp",
    "responsibilities": "admin_audit:readiness_responsibilities",
    "alerts": "admin_audit:readiness_alerts",
    "kpis": "admin_audit:readiness_kpis",
    "notifications": "admin_audit:readiness_alerts",
}


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def readiness_reset(request: HttpRequest, domain: str) -> HttpResponse:
    """Restore one config domain to its code defaults (a single audited ``set``)."""
    back = _RESET_RETURN.get(domain, "admin_audit:readiness_dimensions")
    try:
        config.reset(domain, user=request.user)
    except config.ConfigError as exc:
        messages.error(request, str(exc))
        return redirect(back)
    audit_log(request.user, "readiness.config.reset",
              target_type="readiness_config", target_id=domain,
              metadata={"domain": domain}, ip=client_ip(request))
    messages.success(request, f"{domain.title()} reset to defaults.")
    return redirect(back)


# --- Mandatory ships (config table CRUD; doc 07 §3.4) ------------------------
def _int_or(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def readiness_mandatory_ships(request: HttpRequest) -> HttpResponse:
    """List the ships every pilot (or role) should own — drives strategic readiness."""
    from apps.readiness.models import MandatoryShip

    return render(request, "admin_audit/console/readiness/mandatory_ships.html", {
        "ships": list(MandatoryShip.objects.all()),
        "categories": MandatoryShip.Category.choices,
        "location_kinds": MandatoryShip.LocationKind.choices,
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def readiness_mandatory_ship_save(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    """Create or update a MandatoryShip from the inline form."""
    from apps.readiness.models import MandatoryShip

    label = (request.POST.get("label") or "").strip()
    ship_type_id = _int_or(request.POST.get("ship_type_id"))
    if not label:
        messages.error(request, "A mandatory ship needs a label.")
        return redirect("admin_audit:readiness_mandatory_ships")
    if ship_type_id is None:
        messages.error(request, "Enter the ship type id (the hull this requires).")
        return redirect("admin_audit:readiness_mandatory_ships")

    category = request.POST.get("category")
    if category not in MandatoryShip.Category.values:
        category = MandatoryShip.Category.OTHER
    location_kind = request.POST.get("required_location_kind")
    if location_kind not in MandatoryShip.LocationKind.values:
        location_kind = MandatoryShip.LocationKind.ANY

    fields = {
        "label": label[:120],
        "category": category,
        "ship_type_id": ship_type_id,
        "required_quantity": max(1, _int_or(request.POST.get("required_quantity"), 1)),
        "required_location_kind": location_kind,
        "required_system_id": _int_or(request.POST.get("required_system_id")),
        "require_fitted": request.POST.get("require_fitted") == "on",
        "applies_to_role": (request.POST.get("applies_to_role") or "").strip()[:20],
        "active": request.POST.get("active") == "on",
        "sort_order": _int_or(request.POST.get("sort_order"), 0),
    }
    if pk:
        obj = MandatoryShip.objects.filter(pk=pk).first()
        if obj is None:
            messages.error(request, "That mandatory ship no longer exists.")
            return redirect("admin_audit:readiness_mandatory_ships")
        for key, value in fields.items():
            setattr(obj, key, value)
        obj.save()
        action = "update"
    else:
        obj = MandatoryShip.objects.create(**fields)
        action = "create"
    audit_log(request.user, f"readiness.mandatory_ship.{action}",
              target_type="mandatory_ship", target_id=str(obj.pk), ip=client_ip(request))
    messages.success(request, f"Mandatory ship {'updated' if pk else 'added'}: {obj.label}.")
    return redirect("admin_audit:readiness_mandatory_ships")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def readiness_mandatory_ship_delete(request: HttpRequest, pk: int) -> HttpResponse:
    from apps.readiness.models import MandatoryShip

    MandatoryShip.objects.filter(pk=pk).delete()
    audit_log(request.user, "readiness.mandatory_ship.delete",
              target_type="mandatory_ship", target_id=str(pk), ip=client_ip(request))
    messages.success(request, "Mandatory ship removed.")
    return redirect("admin_audit:readiness_mandatory_ships")


# --- Strategic roles (config table CRUD; doc 07 §3.5) ------------------------
# The role catalogue (doc 03 §3.2) — leadership picks a key from this set.
_ROLE_CATALOGUE = [
    "fc", "logi", "dictor", "hic", "recon", "links", "fax", "dread", "carrier",
    "super", "titan", "hauler", "industrialist", "recruiter", "mentor", "diplomat",
]


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def readiness_strategic_roles(request: HttpRequest) -> HttpResponse:
    """List the desired headcount per strategic role — drives fleet_comp/strategic/leadership."""
    import json

    from apps.readiness.models import StrategicRoleTarget

    roles = list(StrategicRoleTarget.objects.all())
    # Serialise params as real JSON for the edit field (so a round-trip re-parses).
    for role in roles:
        role.params_json = json.dumps(role.detection_params or {})
    existing = {r.role_key for r in roles}
    return render(request, "admin_audit/console/readiness/strategic_roles.html", {
        "roles": roles,
        "detections": StrategicRoleTarget.Detection.choices,
        "available_keys": [k for k in _ROLE_CATALOGUE if k not in existing],
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def readiness_strategic_role_save(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    """Create or update a StrategicRoleTarget. detection_params is parsed as JSON."""
    import json

    from apps.readiness.models import StrategicRoleTarget

    detection = request.POST.get("detection")
    if detection not in StrategicRoleTarget.Detection.values:
        detection = StrategicRoleTarget.Detection.MANUAL

    raw_params = (request.POST.get("detection_params") or "").strip()
    try:
        params = json.loads(raw_params) if raw_params else {}
        if not isinstance(params, dict):
            raise ValueError
    except (ValueError, json.JSONDecodeError):
        messages.error(request, "Detection params must be a JSON object, e.g. "
                                '{"skills": {"3300": 5}}.')
        return redirect("admin_audit:readiness_strategic_roles")

    fields = {
        "label": (request.POST.get("label") or "").strip()[:80],
        "desired_count": max(0, _int_or(request.POST.get("desired_count"), 0)),
        "detection": detection,
        "detection_params": params,
        "active": request.POST.get("active") == "on",
    }
    if pk:
        obj = StrategicRoleTarget.objects.filter(pk=pk).first()
        if obj is None:
            messages.error(request, "That strategic role no longer exists.")
            return redirect("admin_audit:readiness_strategic_roles")
        for key, value in fields.items():
            setattr(obj, key, value)
        obj.save()
        action = "update"
    else:
        role_key = (request.POST.get("role_key") or "").strip().lower()
        if role_key not in _ROLE_CATALOGUE:
            messages.error(request, "Pick a role from the catalogue.")
            return redirect("admin_audit:readiness_strategic_roles")
        if StrategicRoleTarget.objects.filter(role_key=role_key).exists():
            messages.error(request, f"A target for '{role_key}' already exists — edit it instead.")
            return redirect("admin_audit:readiness_strategic_roles")
        if not fields["label"]:
            fields["label"] = role_key.title()
        obj = StrategicRoleTarget.objects.create(role_key=role_key, **fields)
        action = "create"
    audit_log(request.user, f"readiness.strategic_role.{action}",
              target_type="strategic_role", target_id=str(obj.pk), ip=client_ip(request))
    messages.success(request, f"Strategic role {'updated' if pk else 'added'}: {obj.label}.")
    return redirect("admin_audit:readiness_strategic_roles")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def readiness_strategic_role_delete(request: HttpRequest, pk: int) -> HttpResponse:
    from apps.readiness.models import StrategicRoleTarget

    StrategicRoleTarget.objects.filter(pk=pk).delete()
    audit_log(request.user, "readiness.strategic_role.delete",
              target_type="strategic_role", target_id=str(pk), ip=client_ip(request))
    messages.success(request, "Strategic role removed.")
    return redirect("admin_audit:readiness_strategic_roles")


# --- shared config-domain audit (one trail per write) ------------------------
def _audited_set(request: HttpRequest, domain: str, doc: dict, *, ok_message: str, back: str) -> HttpResponse:
    """Validate+persist a config document, audit it, and redirect — the common path."""
    try:
        config.set(domain, doc, user=request.user)
    except config.ConfigError as exc:
        # No partial write: the stored doc and version are untouched.
        messages.error(request, str(exc))
        return redirect(back)
    audit_log(request.user, "readiness.config.update",
              target_type="readiness_config", target_id=domain,
              metadata={"domain": domain}, ip=client_ip(request))
    messages.success(request, ok_message)
    return redirect(back)


# --- Financial configuration (config domain "finance"; doc 07 page 7) --------
_FINANCE_ISK_FIELDS = [
    ("min_wallet", "Minimum wallet balance", "Floor the corp wallet should never drop below."),
    ("monthly_burn_target", "Monthly burn target", "Expected ISK outflow per month."),
    ("srp_budget", "SRP budget", "Monthly ISK set aside for ship replacement."),
    ("emergency_reserve", "Emergency reserve", "Untouchable war-chest the runway scores against."),
    ("alliance_payments_monthly", "Alliance payments / mo", "Dues owed upward each month."),
    ("sov_costs_monthly", "Sovereignty costs / mo", "Bills/indices for held space."),
    ("infrastructure_costs_monthly", "Infrastructure costs / mo", "Fuel, structures and services."),
]


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def readiness_finance(request: HttpRequest) -> HttpResponse:
    """Set the ISK targets the Financial Health dimension scores wallet/runway/burn against."""
    if request.method == "POST":
        current = config.get("finance")
        doc = dict(current)
        for key, _label, _help in _FINANCE_ISK_FIELDS:
            raw = (request.POST.get(key) or "").strip()
            if raw:
                doc[key] = raw
        doc["wallet_division_scope"] = (request.POST.get("wallet_division_scope") or "all").strip() or "all"
        return _audited_set(request, "finance", doc,
                            ok_message="Financial targets saved.",
                            back="admin_audit:readiness_finance")
    cfg = config.get("finance")
    rows = [{"key": k, "label": label, "help": help_, "value": cfg.get(k, 0)}
            for k, label, help_ in _FINANCE_ISK_FIELDS]
    return render(request, "admin_audit/console/readiness/finance.html", {
        "rows": rows,
        "wallet_division_scope": cfg.get("wallet_division_scope", "all"),
        "meta": config.meta("finance"),
    })


# --- SRP thresholds (config domain "srp"; doc 07 page 8) ---------------------
_SRP_FIELDS = [
    ("max_pending_claims", "Max pending claims", "Backlog above this scores red."),
    ("max_avg_wait_hours", "Max average wait (hours)", "Mean approve time the KPI scores against."),
    ("max_claim_age_days", "Max claim age (days)", "Oldest unresolved claim that's still acceptable."),
]


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def readiness_srp(request: HttpRequest) -> HttpResponse:
    """Bound the SRP-health KPIs (backlog size, wait time, oldest claim)."""
    if request.method == "POST":
        current = config.get("srp")
        doc = dict(current)
        for key, _label, _help in _SRP_FIELDS:
            raw = (request.POST.get(key) or "").strip()
            if raw:
                doc[key] = raw
        return _audited_set(request, "srp", doc,
                            ok_message="SRP thresholds saved.",
                            back="admin_audit:readiness_srp")
    cfg = config.get("srp")
    rows = [{"key": k, "label": label, "help": help_, "value": cfg.get(k, 0)}
            for k, label, help_ in _SRP_FIELDS]
    return render(request, "admin_audit/console/readiness/srp.html", {
        "rows": rows,
        "meta": config.meta("srp"),
    })


# --- Officer responsibilities (config domain "responsibilities"; doc 07 page 10) -
def _assignable_users() -> list[dict]:
    """Corp members (by main character) that an owner tag can be assigned to."""
    from apps.sso.models import EveCharacter

    rows = []
    for ch in (EveCharacter.objects
               .filter(is_main=True, is_corp_member=True, user__isnull=False)
               .select_related("user").order_by("name")):
        rows.append({"id": ch.user_id, "name": ch.name})
    return rows


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def readiness_responsibilities(request: HttpRequest) -> HttpResponse:
    """Map each dimension to an owner desk and assign members to those desks.

    A finding routes ``kpi_owner → dimension_owner → unassigned``; the first user on
    an owner tag becomes the auto-assignee of any task generated for that desk.
    """
    if request.method == "POST":
        return _save_responsibilities(request)
    cfg = config.get("responsibilities")
    owner_tags = cfg.get("owner_tags") or {}
    dimension_owner = cfg.get("dimension_owner") or {}
    assignable = _assignable_users()
    tag_rows = []
    for tag, entry in owner_tags.items():
        assigned = {int(u) for u in (entry.get("users") or []) if str(u).isdigit()}
        tag_rows.append({
            "tag": tag,
            "label": entry.get("label", tag),
            "users": [{**u, "assigned": u["id"] in assigned} for u in assignable],
        })
    dim_rows = []
    for provider in registry.providers():
        dim_rows.append({
            "key": provider.key,
            "label": getattr(provider, "label", provider.key.title()),
            "owner": dimension_owner.get(provider.key, ""),
        })
    return render(request, "admin_audit/console/readiness/responsibilities.html", {
        "tag_rows": tag_rows,
        "dim_rows": dim_rows,
        "owner_tags": [{"tag": t, "label": (e.get("label") or t)} for t, e in owner_tags.items()],
        "meta": config.meta("responsibilities"),
    })


def _save_responsibilities(request: HttpRequest) -> HttpResponse:
    cfg = config.get("responsibilities")
    owner_tags = cfg.get("owner_tags") or {}
    # Rebuild owner tags: keep keys, overlay edited label + assigned users.
    new_tags: dict[str, dict] = {}
    for tag, entry in owner_tags.items():
        label = (request.POST.get(f"tag_{tag}_label") or entry.get("label") or tag).strip()
        users = [int(u) for u in request.POST.getlist(f"tag_{tag}_users") if u.isdigit()]
        new_tags[tag] = {"label": label[:60], "users": users}
    # Dimension → owner mapping (blank = unassigned/claimable pool).
    dimension_owner: dict[str, str] = {}
    for provider in registry.providers():
        chosen = (request.POST.get(f"dim_{provider.key}_owner") or "").strip()
        if chosen and chosen in new_tags:
            dimension_owner[provider.key] = chosen
    doc = {"owner_tags": new_tags, "dimension_owner": dimension_owner, "kpi_owner": cfg.get("kpi_owner") or {}}
    return _audited_set(request, "responsibilities", doc,
                        ok_message="Officer responsibilities saved.",
                        back="admin_audit:readiness_responsibilities")


# --- Alerts rule editor (config domain "alerts"; doc 07 page 9) --------------
_ALERT_SEVERITIES = ["info", "warn", "high", "critical"]
_ALERT_CHANNELS = [("discord", "Discord"), ("eve_mail", "EVE-mail")]
_ALERT_KINDS = [("", "Any"), ("risk", "Risk (current gap)"), ("forecast", "Forecast (predicted breach)")]


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def readiness_alerts(request: HttpRequest) -> HttpResponse:
    """Author the alert rules that turn findings into Discord/EVE-mail notifications.

    Rules match structurally on dimension / kind today (score-precise matching lands
    with persisted per-KPI scores); each carries severity, channels, a cooldown and an
    optional escalation window. With no rules the alert layer is inert.
    """
    cfg = config.get("alerts")
    rules = cfg.get("rules") or []
    view_rules = []
    for rule in rules:
        match = rule.get("match") or {}
        view_rules.append({
            "key": rule.get("key", ""),
            "severity": rule.get("severity", "warn"),
            "dimension": match.get("dimension", ""),
            "kind": match.get("kind", ""),
            "score_below": match.get("score_below", ""),
            "channels": rule.get("channels") or [],
            "cooldown_hours": rule.get("cooldown_hours", 24),
            "escalate_after_hours": rule.get("escalate_after_hours") or "",
            "escalate_channels": rule.get("escalate_channels") or [],
        })
    dimensions = [{"key": p.key, "label": getattr(p, "label", p.key.title())}
                  for p in registry.providers()]
    from apps.readiness.mail import eligible_senders

    return render(request, "admin_audit/console/readiness/alerts.html", {
        "rules": view_rules,
        "dimensions": dimensions,
        "severities": _ALERT_SEVERITIES,
        "channels": _ALERT_CHANNELS,
        "kinds": _ALERT_KINDS,
        "meta": config.meta("alerts"),
        "senders": eligible_senders(),
        "current_sender": config.get("notifications").get("eve_mail_sender_character_id"),
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def readiness_mail_sender(request: HttpRequest) -> HttpResponse:
    """Choose the director character that sends readiness alert e-mails in-game."""
    return _audited_set(
        request, "notifications",
        {"eve_mail_sender_character_id": (request.POST.get("sender_character_id") or "").strip() or None},
        ok_message="EVE-mail sender saved.",
        back="admin_audit:readiness_alerts",
    )


def _rule_from_post(request: HttpRequest, key: str) -> dict:
    """Build a single alert rule dict from the posted fields."""
    match: dict = {}
    dimension = (request.POST.get("dimension") or "").strip()
    if dimension:
        match["dimension"] = dimension
    kind = (request.POST.get("kind") or "").strip()
    if kind:
        match["kind"] = kind
    score_below = _int_or((request.POST.get("score_below") or "").strip())
    if score_below is not None:
        match["score_below"] = score_below
    severity = request.POST.get("severity")
    if severity not in _ALERT_SEVERITIES:
        severity = "warn"
    channels = [c for c, _ in _ALERT_CHANNELS if request.POST.get(f"channel_{c}") == "on"]
    escalate_channels = [c for c, _ in _ALERT_CHANNELS if request.POST.get(f"escalate_{c}") == "on"]
    rule: dict = {
        "key": key,
        "severity": severity,
        "match": match,
        "channels": channels,
        "cooldown_hours": max(0, _int_or(request.POST.get("cooldown_hours"), 24)),
    }
    escalate_after = _int_or(request.POST.get("escalate_after_hours"))
    if escalate_after and escalate_after > 0:
        rule["escalate_after_hours"] = escalate_after
        rule["escalate_channels"] = escalate_channels or channels
    return rule


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def readiness_alert_save(request: HttpRequest) -> HttpResponse:
    """Add or update one alert rule (upsert by key), preserving the others."""
    import re

    key = (request.POST.get("key") or "").strip().lower()
    original_key = (request.POST.get("original_key") or "").strip().lower()
    if not key or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,39}", key):
        messages.error(request, "Rule key must be 2–40 chars: lowercase letters, digits, '-' or '_'.")
        return redirect("admin_audit:readiness_alerts")
    rules = list(config.get("alerts").get("rules") or [])
    rule = _rule_from_post(request, key)
    # Upsert by key: replace the rule under edit (original_key) or any rule sharing the
    # new key; otherwise append. config.set rejects a duplicate key as a backstop.
    drop = {original_key, key}
    rules = [r for r in rules if (r.get("key") or "").strip().lower() not in drop]
    rules.append(rule)
    return _audited_set(request, "alerts", {"rules": rules},
                        ok_message=f"Alert rule '{key}' saved.",
                        back="admin_audit:readiness_alerts")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def readiness_alert_delete(request: HttpRequest, key: str) -> HttpResponse:
    rules = [r for r in (config.get("alerts").get("rules") or [])
             if (r.get("key") or "").strip().lower() != key.strip().lower()]
    return _audited_set(request, "alerts", {"rules": rules},
                        ok_message="Alert rule removed.",
                        back="admin_audit:readiness_alerts")


# --- KPI configuration (config domain "kpis"; doc 07 §3.2) -------------------
def readiness_kpi_groups(cfg: dict) -> list[dict]:
    """Each registered dimension's declared KPIs joined to their stored config.

    Driven off each provider's ``kpi_catalogue`` so the page is comprehensive and cheap
    (no provider re-run), and lists a dimension's KPIs even while the dimension is
    disabled. Dimensions with no configurable KPIs (doctrine/skill/stock/logistics) are
    skipped.
    """
    groups = []
    for provider in registry.providers():
        catalogue = getattr(provider, "kpi_catalogue", [])
        if not catalogue:
            continue
        rows = []
        for key, label in catalogue:
            entry = cfg.get(key, {})
            thr = entry.get("thresholds", {})
            rows.append({
                "key": key, "label": label,
                "enabled": entry.get("enabled", True),
                "weight": entry.get("weight", 1.0),
                "amber": thr.get("amber", ""),
                "red": thr.get("red", ""),
            })
        groups.append({
            "dimension": provider.key,
            "label": getattr(provider, "label", provider.key.title()),
            "rows": rows,
        })
    return groups


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def readiness_kpis(request: HttpRequest) -> HttpResponse:
    """Enable/disable, weight and re-band individual KPIs within each dimension.

    A disabled KPI is dropped from its dimension's score; a non-1.0 weight makes the
    dimension a weighted mean of its KPIs; thresholds override the KPI's status bands.
    Defaults (all enabled, weight 1.0, provider bands) reproduce the engine exactly.
    """
    if request.method == "POST":
        return _save_kpis(request)
    return render(request, "admin_audit/console/readiness/kpis.html", {
        "groups": readiness_kpi_groups(config.get("kpis")),
        "meta": config.meta("kpis"),
    })


def _save_kpis(request: HttpRequest) -> HttpResponse:
    cfg = config.get("kpis")
    doc: dict[str, dict] = {}
    for provider in registry.providers():
        for key, _label in getattr(provider, "kpi_catalogue", []):
            cur = cfg.get(key, {})
            entry: dict = {
                "enabled": request.POST.get(f"kpi_{key}_enabled") == "on",
                "weight": (request.POST.get(f"kpi_{key}_weight") or "").strip() or cur.get("weight", 1.0),
            }
            amber = (request.POST.get(f"kpi_{key}_amber") or "").strip()
            red = (request.POST.get(f"kpi_{key}_red") or "").strip()
            if amber and red:
                entry["thresholds"] = {"amber": amber, "red": red}
            doc[key] = entry
    return _audited_set(request, "kpis", doc,
                        ok_message="KPI configuration saved.",
                        back="admin_audit:readiness_kpis")


# --- Doctrine readiness classification (G5; doc 07 §3.3) ---------------------
@login_required
@role_required(rbac.ROLE_DIRECTOR)
def readiness_doctrines(request: HttpRequest) -> HttpResponse:
    """Classify doctrines: primary / mandatory / alliance, retirement date, min pilots.

    A mandatory doctrine that's under-crewed escalates to a high-severity finding; a
    doctrine past its retirement date raises a replace-it finding. Leaving a doctrine
    unclassified is the default (no effect on the index).
    """
    if request.method == "POST":
        return _save_doctrines(request)
    from apps.doctrines.models import Doctrine
    from apps.readiness.models import DoctrineReadinessConfig

    configs = {c.doctrine_id: c for c in DoctrineReadinessConfig.objects.all()}
    rows = []
    for d in Doctrine.objects.order_by("name"):
        c = configs.get(d.id)
        rows.append({
            "id": d.id, "name": d.name, "priority": d.priority,
            "is_primary": getattr(c, "is_primary", False),
            "is_mandatory": getattr(c, "is_mandatory", False),
            "is_alliance": getattr(c, "is_alliance", False),
            "is_upcoming": getattr(c, "is_upcoming", False),
            "retirement_date": c.retirement_date.isoformat() if c and c.retirement_date else "",
            "min_pilots": c.min_pilots if c and c.min_pilots is not None else "",
        })
    return render(request, "admin_audit/console/readiness/doctrines.html", {"rows": rows})


def _save_doctrines(request: HttpRequest) -> HttpResponse:
    import datetime as dt

    from apps.doctrines.models import Doctrine
    from apps.readiness.models import DoctrineReadinessConfig

    classified = 0
    for d in Doctrine.objects.all():
        is_primary = request.POST.get(f"doc_{d.id}_primary") == "on"
        is_mandatory = request.POST.get(f"doc_{d.id}_mandatory") == "on"
        is_alliance = request.POST.get(f"doc_{d.id}_alliance") == "on"
        is_upcoming = request.POST.get(f"doc_{d.id}_upcoming") == "on"
        ret_raw = (request.POST.get(f"doc_{d.id}_retire") or "").strip()
        retirement = None
        if ret_raw:
            try:
                retirement = dt.date.fromisoformat(ret_raw)
            except ValueError:
                messages.error(request, f"{d.name}: retirement date must be YYYY-MM-DD.")
                return redirect("admin_audit:readiness_doctrines")
        min_pilots = _int_or(request.POST.get(f"doc_{d.id}_min"))
        if min_pilots is not None and min_pilots < 0:
            min_pilots = None
        # An all-default doctrine stores no row, keeping the engine's fast no-config path.
        if not (is_primary or is_mandatory or is_alliance or is_upcoming or retirement or min_pilots):
            DoctrineReadinessConfig.objects.filter(doctrine=d).delete()
            continue
        DoctrineReadinessConfig.objects.update_or_create(
            doctrine=d, defaults={
                "is_primary": is_primary, "is_mandatory": is_mandatory, "is_alliance": is_alliance,
                "is_upcoming": is_upcoming, "retirement_date": retirement, "min_pilots": min_pilots,
            })
        classified += 1
    audit_log(request.user, "readiness.doctrine_config.update",
              target_type="readiness_config", target_id="doctrines", ip=client_ip(request))
    messages.success(request, f"Doctrine readiness saved ({classified} classified).")
    return redirect("admin_audit:readiness_doctrines")


# --- Fleet support skills (Gap B4 — Fleet Support dimension) ------------------
@login_required
@role_required(rbac.ROLE_DIRECTOR)
def readiness_support_skill_search(request: HttpRequest) -> JsonResponse:
    """Autocomplete for the fleet-support skill picker (in-game skills, director-only)."""
    from apps.sde.search import search_skills

    return JsonResponse(search_skills(request.GET.get("q", ""), limit=20), safe=False)


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def readiness_support(request: HttpRequest) -> HttpResponse:
    """Curate the fleet-support skill list that drives the Fleet Support dimension.

    With no active skills the dimension stays unavailable (disabled until configured).
    """
    from apps.readiness.models import FleetSupportSkill

    enabled = config.get("dimensions").get("support", {}).get("enabled", False)
    return render(request, "admin_audit/console/readiness/support.html", {
        "skills": list(FleetSupportSkill.objects.all()),
        "levels": [1, 2, 3, 4, 5],
        "dimension_enabled": enabled,
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def readiness_support_skill_save(request: HttpRequest, pk: int | None = None) -> HttpResponse:
    """Create or update one fleet-support skill from the inline form."""
    from apps.readiness.models import FleetSupportSkill
    from apps.sde.models import SdeType

    skill_type_id = _int_or(request.POST.get("skill_type_id"))
    if skill_type_id is None:
        messages.error(request, "Pick a skill from the search list.")
        return redirect("admin_audit:readiness_support")
    skill = SdeType.objects.filter(type_id=skill_type_id).first()
    if skill is None:
        messages.error(request, "That skill isn't in the SDE — pick one from the search list.")
        return redirect("admin_audit:readiness_support")
    min_level = _int_or(request.POST.get("min_level"), 5)
    min_level = min(5, max(1, min_level))
    fields = {
        "skill_type_id": skill_type_id,
        "skill_name": skill.name[:120],
        "min_level": min_level,
        "active": request.POST.get("active") == "on",
        "sort_order": _int_or(request.POST.get("sort_order"), 0),
    }
    if pk:
        obj = FleetSupportSkill.objects.filter(pk=pk).first()
        if obj is None:
            messages.error(request, "That support skill no longer exists.")
            return redirect("admin_audit:readiness_support")
        for key, value in fields.items():
            setattr(obj, key, value)
        obj.save()
        action = "update"
    else:
        if FleetSupportSkill.objects.filter(skill_type_id=skill_type_id).exists():
            messages.error(request, f"{skill.name} is already in the list — edit it instead.")
            return redirect("admin_audit:readiness_support")
        obj = FleetSupportSkill.objects.create(**fields)
        action = "create"
    audit_log(request.user, f"readiness.support_skill.{action}",
              target_type="fleet_support_skill", target_id=str(obj.pk), ip=client_ip(request))
    messages.success(request, f"Support skill {'updated' if pk else 'added'}: {obj.skill_name}.")
    return redirect("admin_audit:readiness_support")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def readiness_support_skill_delete(request: HttpRequest, pk: int) -> HttpResponse:
    from apps.readiness.models import FleetSupportSkill

    FleetSupportSkill.objects.filter(pk=pk).delete()
    audit_log(request.user, "readiness.support_skill.delete",
              target_type="fleet_support_skill", target_id=str(pk), ip=client_ip(request))
    messages.success(request, "Support skill removed.")
    return redirect("admin_audit:readiness_support")


# --- Staging system (Gap B5 — Asset Staging dimension) -----------------------
@login_required
@role_required(rbac.ROLE_DIRECTOR)
def readiness_staging(request: HttpRequest) -> HttpResponse:
    """Set the corp staging solar system that the Asset Staging dimension scores against.

    With no staging system the dimension stays unavailable (disabled until configured).
    """
    if request.method == "POST":
        return _save_staging(request)
    from apps.readiness.models import StagingSystem

    current = StagingSystem.objects.filter(active=True).first()
    enabled = config.get("dimensions").get("staging", {}).get("enabled", False)
    return render(request, "admin_audit/console/readiness/staging.html", {
        "current": current,
        "dimension_enabled": enabled,
    })


def _save_staging(request: HttpRequest) -> HttpResponse:
    from apps.readiness.models import StagingSystem
    from apps.sde.models import SdeSolarSystem

    system_id = _int_or(request.POST.get("system_id"))
    if system_id is None:
        messages.error(request, "Search for and pick a staging system.")
        return redirect("admin_audit:readiness_staging")
    system = SdeSolarSystem.objects.filter(system_id=system_id).first()
    if system is None:
        messages.error(request, "That system isn't in the SDE — pick one from the search list.")
        return redirect("admin_audit:readiness_staging")
    # Single active staging system: replace any previous one.
    StagingSystem.objects.all().delete()
    obj = StagingSystem.objects.create(
        system_id=system_id, system_name=system.name[:120], active=True)
    audit_log(request.user, "readiness.staging.set",
              target_type="staging_system", target_id=str(obj.system_id), ip=client_ip(request))
    messages.success(request, f"Staging system set to {obj.system_name}.")
    return redirect("admin_audit:readiness_staging")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def readiness_staging_clear(request: HttpRequest) -> HttpResponse:
    from apps.readiness.models import StagingSystem

    StagingSystem.objects.all().delete()
    audit_log(request.user, "readiness.staging.clear",
              target_type="staging_system", target_id="", ip=client_ip(request))
    messages.success(request, "Staging system cleared — the Asset Staging dimension is now unavailable.")
    return redirect("admin_audit:readiness_staging")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def readiness_wizard(request: HttpRequest) -> HttpResponse:
    """RDY-2 (2.6): guided activation — preview each disabled dimension's would-be live
    score + recommended starter weight, and enable it in one click."""
    from apps.readiness.services import activation_preview, enable_dimension

    if request.method == "POST":
        key = (request.POST.get("dimension") or "").strip()
        if enable_dimension(key):
            audit_log(request.user, "readiness.dimension.enabled",
                      target_type="readiness_dimension", target_id=key, ip=client_ip(request))
            messages.success(request, f"Enabled the “{key}” dimension — it now scores in the index.")
        else:
            messages.error(request, "That dimension could not be enabled (unknown or already on).")
        return redirect("admin_audit:readiness_wizard")

    return render(request, "admin_audit/console/readiness/wizard.html", {
        "rows": activation_preview(),
    })
