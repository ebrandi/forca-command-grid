"""Admin Console — Notifications governance (Director-gated).

The one page that concentrates every application notification: what fires it, who it is
for, and whether it may reach a mass chat channel. Leadership tunes each event
(enable/disable, audience, severity floor) and sets who counts as corp leadership.
Writes funnel through ``apps.pingboard.config`` (validate → persist → version bump →
cache bust) then ``audit_log`` — the same contract as the rest of the Pingboard console.

The guarantee this page makes visible: a leadership-audience event resolves to a
leadership *classification*, and the Pingboard sink refuses to post it to any chat
channel whose ceiling is below that tier. So sensitive traffic reaches leadership-cleared
channels (and the addressed pilots in-app / by EVE-mail) but never a corp-wide channel.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _

from apps.pingboard import config, notifications
from apps.pingboard.models import ChannelProvider
from apps.pingboard.services import _CLASSIFICATION_RANK
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

# A channel is "leadership-cleared" when its classification ceiling clears high_command
# (officer tier); _CLASSIFICATION_RANK is pingboard's shared tier ordering.
_LEADERSHIP_TIER_RANK = 1


def _int(v, d):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _leadership_channels() -> list[dict]:
    """Enabled broadcast chat channels split by whether they may carry leadership tiers."""
    out = []
    for row in ChannelProvider.objects.filter(
        kind__in=("discord", "slack", "telegram", "whatsapp"), enabled=True
    ):
        rank = _CLASSIFICATION_RANK.get(row.max_classification or "corp_internal", 0)
        out.append({
            "label": row.label, "kind": row.get_kind_display(),
            "ceiling": row.max_classification or "corp_internal",
            "leadership_ok": rank >= _LEADERSHIP_TIER_RANK,
        })
    return out


def _event_rows() -> list[dict]:
    """The catalogue with each event's effective policy, grouped for display."""
    rows = []
    for ev in notifications.REGISTRY:
        pol = notifications.resolve(ev.key)
        rows.append({
            "key": ev.key, "label": ev.label, "description": ev.description,
            "group": ev.group, "group_label": notifications.GROUP_LABELS.get(ev.group, ev.group),
            "source_service": ev.source_service, "triggers": ev.triggers,
            "sensitive": ev.sensitive, "has_severity": ev.min_severity is not None,
            "enabled": pol["enabled"], "audience": pol["audience"],
            "classification": pol["classification"] or "corp_internal",
            "min_severity": pol.get("min_severity") or ev.min_severity or 0,
            "reaches_mass": pol["classification"] is None,  # corp-internal → any channel
        })
    return rows


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def notifications_console(request):
    back = "admin_audit:notifications"
    if request.method == "POST":
        domain = request.POST.get("form")
        if domain == "leadership":
            return _save_leadership(request, back)
        if domain == "events":
            return _save_events(request, back)
        messages.error(request, _("Unknown form."))
        return redirect(back)

    User = get_user_model()
    members = list(
        User.objects.filter(characters__is_corp_member=True).distinct().order_by("username")
    )
    selected = set(notifications.leadership_user_ids())
    ctx = {
        "events": _event_rows(),
        "group_order": [
            (g, notifications.GROUP_LABELS[g]) for g in (
                notifications.GROUP_LEADERSHIP,
                notifications.GROUP_OPERATIONS,
                notifications.GROUP_MEMBER,
            )
        ],
        "audience_choices": notifications.AUDIENCE_CHOICES,
        "leadership_role": notifications.leadership_role(),
        "role_choices": ("member", "officer", "director", "admin"),
        "members": [
            {"id": u.id, "name": getattr(u, "display_name", "") or u.get_username(),
             "selected": u.id in selected}
            for u in members
        ],
        "channels": _leadership_channels(),
        "meta": config.meta("notifications"),
    }
    return render(request, "admin_audit/console/notifications.html", ctx)


def _save_leadership(request, back):
    p = request.POST
    role = p.get("leadership_role", "officer")
    if role not in ("member", "officer", "director", "admin"):
        role = "officer"
    user_ids = [_int(v, None) for v in p.getlist("leadership_user_ids")]
    user_ids = sorted({i for i in user_ids if i is not None})
    doc = dict(config.get("notifications"))
    doc["leadership_role"] = role
    doc["leadership_user_ids"] = user_ids
    return _audited_set(request, doc, ok=_("Leadership distribution saved."), back=back)


def _save_events(request, back):
    p = request.POST
    events: dict[str, dict] = {}
    for ev in notifications.REGISTRY:
        key = ev.key
        entry: dict = {"enabled": p.get(f"enabled__{key}") == "on"}
        aud = p.get(f"audience__{key}")
        if aud in notifications.AUDIENCE_CHOICES:
            entry["audience"] = aud
        if ev.min_severity is not None:
            entry["min_severity"] = max(0, min(100, _int(p.get(f"severity__{key}"), ev.min_severity)))
        events[key] = entry
    doc = dict(config.get("notifications"))
    doc["events"] = events
    return _audited_set(request, doc, ok=_("Notification settings saved."), back=back)


def _audited_set(request, doc, *, ok, back):
    try:
        config.set("notifications", doc, user=request.user)
    except config.ConfigError as exc:
        messages.error(request, str(exc))
        return redirect(back)
    audit_log(request.user, "pingboard.config.update", target_type="pingboard_config",
              target_id="notifications", metadata={"domain": "notifications"}, ip=client_ip(request))
    messages.success(request, ok)
    return redirect(back)
