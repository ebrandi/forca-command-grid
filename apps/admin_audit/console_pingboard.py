"""Admin Console — Pingboard configuration (Director-gated).

Config writes funnel through ``apps.pingboard.config`` (validate → persist → version
bump → cache bust) then ``audit_log``. Provider secrets are write-only: the form shows
only whether a secret is set and a "replace" input; the value is never rendered back.
"""
from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.pingboard import config
from apps.pingboard.models import (
    AlertCategory,
    AlertPriority,
    AlertTemplate,
    AutomationRule,
    ChannelKind,
    ChannelProvider,
)
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

# Classification tiers a channel may be capped at ("" = uncapped). Mirrors the
# Command-Intelligence vocabulary that pingboard.services enforces at the sink.
_ALLOWED_MAX_CLASSIFICATION = {
    "", "corp_internal", "high_command", "director_eyes_only", "alliance_command",
}


def _int(v, d):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _audited_set(request, domain, doc, *, ok, back):
    try:
        config.set(domain, doc, user=request.user)
    except config.ConfigError as exc:
        messages.error(request, str(exc))
        return redirect(back)
    audit_log(request.user, "pingboard.config.update", target_type="pingboard_config",
              target_id=domain, metadata={"domain": domain}, ip=client_ip(request))
    messages.success(request, ok)
    return redirect(back)


# --- settings (general / anti_abuse / calendar) ------------------------------
@login_required
@role_required(rbac.ROLE_DIRECTOR)
def pingboard_settings(request):
    back = "admin_audit:pingboard_settings"
    if request.method == "POST":
        domain = request.POST.get("domain")
        p = request.POST
        if domain == "general":
            doc = {
                "enabled": p.get("enabled") == "on",
                "manual_alerts_enabled": p.get("manual_alerts_enabled") == "on",
                "automated_alerts_enabled": p.get("automated_alerts_enabled") == "on",
                "urgent_alerts_enabled": p.get("urgent_alerts_enabled") == "on",
                "calendar_enabled": p.get("calendar_enabled") == "on",
                "default_expiry_minutes": _int(p.get("default_expiry_minutes"), 720),
                "site_url": p.get("site_url", "").strip(),
            }
            return _audited_set(request, "general", doc, ok=_("General settings saved."), back=back)
        if domain == "anti_abuse":
            doc = {
                "max_per_officer_per_hour": _int(p.get("max_per_officer_per_hour"), 20),
                "max_per_category_per_hour": _int(p.get("max_per_category_per_hour"), 30),
                "max_urgent_per_day": _int(p.get("max_urgent_per_day"), 10),
                "cooldown_minutes": _int(p.get("cooldown_minutes"), 15),
                "duplicate_window_minutes": _int(p.get("duplicate_window_minutes"), 10),
                "large_audience_threshold": _int(p.get("large_audience_threshold"), 50),
                "two_step_urgent": p.get("two_step_urgent") == "on",
                "suppress_duplicates": p.get("suppress_duplicates") == "on",
            }
            return _audited_set(request, "anti_abuse", doc, ok=_("Anti-abuse settings saved."), back=back)
        if domain == "calendar":
            doc = {
                "manual_entries_enabled": p.get("manual_entries_enabled") == "on",
                "automated_sync_enabled": p.get("automated_sync_enabled") == "on",
                "auto_alerts_mode": p.get("auto_alerts_mode", "draft_until_approved"),
                "pilot_visibility": p.get("pilot_visibility") == "on",
                "event_retention_days": _int(p.get("event_retention_days"), 90),
            }
            return _audited_set(request, "calendar", doc, ok=_("Calendar settings saved."), back=back)

    ctx = {
        "general": config.get("general"),
        "anti_abuse": config.get("anti_abuse"),
        "calendar": config.get("calendar"),
        "meta": config.meta("general"),
    }
    return render(request, "admin_audit/console/pingboard/settings.html", ctx)


# --- providers (channel config + test) ---------------------------------------
# Non-secret routing keys each kind addresses with (drives the form + list summary).
# WhatsApp/Telegram now carry their full credentials on the row so nothing needs the
# server env: the bot token / access token / auth token is the encrypted secret, and
# these plain ids/numbers live in routing.
_ROUTING_KEYS = {
    "eve_mail": ["sender_character_id"],
    "slack": ["channel"],
    "telegram": ["chat_id"],
    "whatsapp": ["to", "backend", "meta_phone_id", "meta_api_version", "twilio_sid", "twilio_from"],
}


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def pingboard_channels(request):
    """Channel list + lightweight row actions (test / enable / delete / legacy create).

    Full configuration and editing happen on the dedicated form
    (:func:`pingboard_channel_new` / :func:`pingboard_channel_edit`).
    """
    if request.method == "POST":
        return _channels_post(request)
    ctx = {"providers": ChannelProvider.objects.all(), "kinds": ChannelKind.choices}
    return render(request, "admin_audit/console/pingboard/channels.html", ctx)


def _channels_post(request):
    back = "admin_audit:pingboard_channels"
    p = request.POST
    action = p.get("action")
    if action == "create":
        # Kept for API/back-compat; the guided form posts to pingboard_channel_save.
        prov = ChannelProvider()
        _apply_provider_fields(request, prov, creating=True)
        prov.save()
        audit_log(request.user, "pingboard.channel.configured", target_type="pingboard_provider",
                  target_id=str(prov.id), metadata={"kind": prov.kind}, ip=client_ip(request))
        messages.success(request, _("Channel added."))
        return redirect(back)

    prov = get_object_or_404(ChannelProvider, pk=p.get("provider_id"))
    if action == "toggle":
        prov.enabled = not prov.enabled
        prov.save(update_fields=["enabled", "updated_at"])
        audit_log(request.user, "pingboard.channel.armed" if prov.enabled else "pingboard.channel.disabled",
                  target_type="pingboard_provider", target_id=str(prov.id), ip=client_ip(request))
        messages.success(request, _("Channel enabled.") if prov.enabled else _("Channel disabled."))
    elif action == "test":
        _run_test(request, prov)
    elif action == "delete":
        prov.delete()
        audit_log(request.user, "pingboard.channel.deleted", target_type="pingboard_provider",
                  target_id=str(p.get("provider_id")), ip=client_ip(request))
        messages.success(request, _("Channel deleted."))
    return redirect(back)


# --- guided add / edit -------------------------------------------------------
@login_required
@role_required(rbac.ROLE_DIRECTOR)
def pingboard_channel_new(request):
    return render(request, "admin_audit/console/pingboard/channel_form.html", _form_ctx(None))


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def pingboard_channel_edit(request, pk):
    prov = get_object_or_404(ChannelProvider, pk=pk)
    return render(request, "admin_audit/console/pingboard/channel_form.html", _form_ctx(prov))


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def pingboard_channel_save(request, pk=None):
    """Create (pk is None) or update an existing channel from the guided form."""
    if request.method != "POST":
        return redirect("admin_audit:pingboard_channels")
    creating = pk is None
    prov = ChannelProvider() if creating else get_object_or_404(ChannelProvider, pk=pk)
    _apply_provider_fields(request, prov, creating=creating)
    prov.save()
    audit_log(
        request.user,
        "pingboard.channel.configured" if creating else "pingboard.channel.updated",
        target_type="pingboard_provider", target_id=str(prov.id),
        metadata={"kind": prov.kind}, ip=client_ip(request),
    )
    ok, why = _provider_ready(prov)
    if ok:
        messages.success(request, _("Channel “%(label)s” saved and ready to send.") % {"label": prov.label})
    else:
        messages.warning(request, _("Channel “%(label)s” saved, but not operational yet: %(why)s") % {
            "label": prov.label, "why": why})
    return redirect("admin_audit:pingboard_channels")


def _form_ctx(prov) -> dict:
    from apps.readiness.mail import eligible_senders

    ctx = {"prov": prov, "kinds": ChannelKind.choices, "senders": eligible_senders()}
    if prov is not None:
        ok, why = _provider_ready(prov)
        ctx["ready_ok"], ctx["ready_why"] = ok, why
        ctx["current_sender"] = (prov.routing or {}).get("sender_character_id")
    return ctx


def _apply_provider_fields(request, prov, *, creating: bool) -> None:
    """Set every operational field on ``prov`` from the POST (never saves)."""
    p = request.POST
    prov.label = (p.get("label") or prov.label or "Channel").strip()[:100]
    if creating:
        prov.kind = p.get("kind", "discord")
    prov.routing = _routing_from(prov.kind, p)
    if p.get("secret", "").strip():
        prov.secret = p["secret"].strip()
    elif p.get("clear_secret") == "on":
        prov.secret = ""
    # Per-channel classification ceiling (broadcast chat channels). A broadcast whose
    # tier exceeds this is skipped at the sink — the Command-Intelligence guard, per channel.
    cls = (p.get("max_classification") or "").strip()
    prov.max_classification = cls if cls in _ALLOWED_MAX_CLASSIFICATION else ""
    prov.enabled = p.get("enabled") == "on"
    # Capability flags belong to the provider class, not to hand entry.
    from apps.pingboard.providers import provider_class

    cls = provider_class(prov.kind)
    if cls is not None:
        prov.supports_direct = bool(cls.supports_direct)
        prov.supports_group = bool(cls.supports_group)
        prov.supports_channel = bool(cls.supports_channel)


def _routing_from(kind: str, p) -> dict:
    """Non-secret addressing for the kind — only the keys that kind uses."""
    routing = {}
    for key in _ROUTING_KEYS.get(kind, []):
        val = p.get(f"routing_{key}", "").strip()
        if not val:
            continue
        routing[key] = int(val) if key == "sender_character_id" and val.isdigit() else val
    return routing


def _provider_ready(prov) -> tuple[bool, str]:
    """(ok, redacted reason) — is this channel configured well enough to send?"""
    from apps.pingboard.providers import provider_class

    cls = provider_class(prov.kind)
    if cls is None:
        return False, _("no provider implementation for this channel kind")
    try:
        return cls(prov).validate_configuration()
    except Exception:  # noqa: BLE001 - a config probe must never 500 the page
        return False, _("could not verify configuration")


def _test_recipient(user, kind):
    """A recipient so "Send test message" works for per-recipient channels.

    EVE-mail has no broadcast destination — a test mail needs a character to send TO — so we
    address it to the officer running the test (their main character); they confirm the
    channel by receiving it. Chat/webhook channels post to their configured destination and
    need no recipient (``None``); in-app trivially confirms with zero recipients.
    """
    if kind != "eve_mail":
        return None
    from apps.pingboard.providers.base import Recipient

    chars = list(user.characters.all())
    char = next((c for c in chars if getattr(c, "is_main", False)), None) or (chars[0] if chars else None)
    if char is None:
        return None
    return Recipient("eve_mail", "character", str(char.character_id), user.id, getattr(char, "name", ""))


def _run_test(request, prov):
    from apps.pingboard.providers import provider_class

    cls = provider_class(prov.kind)
    if cls is None:
        messages.error(request, _("No provider implementation for this channel."))
        return
    to = _test_recipient(request.user, prov.kind)
    if prov.kind == "eve_mail" and to is None:
        messages.error(
            request,
            _("Can't send a test EVE-mail — your account has no linked character to receive it. "
              "Link a character, or just send a real alert (delivery to the corp works independently)."),
        )
        return
    result = cls(prov).send_test(to=to)
    now = timezone.now()
    prov.last_test_at = now
    if result.ok:
        prov.last_ok_at = now
        prov.last_error = ""
        if prov.kind == "eve_mail":
            messages.success(request, _("Test mail sent to %(recipient)s — check your in-game inbox.") % {
                "recipient": to.display or _("your character")})
        else:
            messages.success(request, _("Test message sent."))
    else:
        prov.last_error = (result.error or "failed")[:300]
        prov.last_error_at = now
        messages.error(request, _("Test failed: %(error)s") % {"error": result.error or _("unknown error")})
    prov.save(update_fields=["last_test_at", "last_ok_at", "last_error", "last_error_at", "updated_at"])
    audit_log(request.user, "pingboard.channel.test", target_type="pingboard_provider",
              target_id=str(prov.id), metadata={"ok": result.ok}, ip=client_ip(request))


# --- automation rules --------------------------------------------------------
_RULE_INPUT = {"class": "input-field mt-1"}
# Known audience kinds/roles the dispatcher understands (must match dispatch.py); an
# unknown kind classifies as uncapped downstream, so reject it at the form.
_AUDIENCE_KINDS = {
    "corp", "public", "member", "officer", "director", "admin",
    "user", "users", "role", "context_user", "channel",
}
_AUDIENCE_ROLES = {"member", "officer", "director", "admin"}


class AutomationRuleForm(forms.ModelForm):
    """PNG-1 (2.9): full create/edit form for an automation rule — condition, template,
    audience/channels, cooldown, per-window cap, window, expiry and dry-run. ``enabled``
    is deliberately NOT a form field: rules always ship disabled and are armed via the
    explicit toggle, so a create/edit can never silently arm a fan-out."""

    class Meta:
        model = AutomationRule
        fields = [
            "key", "label", "trigger_source", "condition", "category", "template",
            "title", "body", "audience", "channels", "priority",
            "cooldown_minutes", "max_per_window", "window_minutes", "expires_at", "dry_run",
        ]
        widgets = {
            "key": forms.TextInput(attrs={**_RULE_INPUT, "placeholder": "srp-submitted"}),
            "label": forms.TextInput(attrs=_RULE_INPUT),
            "trigger_source": forms.TextInput(attrs={**_RULE_INPUT, "placeholder": "srp.submitted"}),
            "condition": forms.Textarea(attrs={**_RULE_INPUT, "rows": 2, "placeholder": '{"amount_gt": 1000000000}'}),
            "category": forms.Select(attrs=_RULE_INPUT),
            "template": forms.Select(attrs=_RULE_INPUT),
            "title": forms.TextInput(attrs=_RULE_INPUT),
            "body": forms.Textarea(attrs={**_RULE_INPUT, "rows": 2}),
            "audience": forms.Textarea(attrs={**_RULE_INPUT, "rows": 1, "placeholder": '{"kind": "officer"}'}),
            "channels": forms.Textarea(attrs={**_RULE_INPUT, "rows": 1, "placeholder": '["in_app", "discord"]'}),
            "priority": forms.Select(attrs=_RULE_INPUT),
            "cooldown_minutes": forms.NumberInput(attrs={**_RULE_INPUT, "min": 0}),
            "max_per_window": forms.NumberInput(attrs={**_RULE_INPUT, "min": 0}),
            "window_minutes": forms.NumberInput(attrs={**_RULE_INPUT, "min": 1}),
            "expires_at": forms.DateTimeInput(attrs={**_RULE_INPUT, "type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "dry_run": forms.CheckboxInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["expires_at"].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"]
        self.fields["template"].required = False
        self.fields["template"].empty_label = _("— none (use title/body below) —")

    def clean_condition(self):
        v = self.cleaned_data["condition"]
        if v in (None, ""):
            return {}
        if not isinstance(v, dict):
            raise forms.ValidationError(_("Condition must be a JSON object, e.g. {\"amount_gt\": 1000000000}."))
        return v

    def clean_audience(self):
        v = self.cleaned_data["audience"]
        if v in (None, ""):
            return {}
        if not isinstance(v, dict):
            raise forms.ValidationError(_("Audience must be a JSON object, e.g. {\"kind\": \"officer\"}."))
        # Reject an unknown `kind`: downstream classification defaults an unrecognised kind
        # to uncapped (corp-internal), so a typo like {"kind":"directors"} would fail OPEN
        # and could post a restricted alert to a corp-wide channel. Fail closed here instead.
        kind = v.get("kind")
        if kind not in _AUDIENCE_KINDS:
            raise forms.ValidationError(
                _("Unknown audience kind %(kind)r. Use one of: %(kinds)s.") % {
                    "kind": kind, "kinds": ', '.join(sorted(_AUDIENCE_KINDS))}
            )
        if kind == "role" and v.get("role") not in _AUDIENCE_ROLES:
            raise forms.ValidationError(
                _("Audience role must be one of: %(roles)s.") % {
                    "roles": ', '.join(sorted(_AUDIENCE_ROLES))}
            )
        return v

    def clean_window_minutes(self):
        v = self.cleaned_data["window_minutes"]
        if v < 1:
            raise forms.ValidationError(_("Window must be at least 1 minute."))
        return v

    def clean_cooldown_minutes(self):
        v = self.cleaned_data["cooldown_minutes"]
        if v < 0:
            raise forms.ValidationError(_("Cooldown can't be negative."))
        return v

    def clean_max_per_window(self):
        v = self.cleaned_data["max_per_window"]
        if v < 0:
            raise forms.ValidationError(_("Max per window can't be negative (0 = unlimited)."))
        return v

    def clean_channels(self):
        v = self.cleaned_data["channels"]
        if v in (None, ""):
            return []
        if not isinstance(v, list) or not all(isinstance(c, str) for c in v):
            raise forms.ValidationError(_("Channels must be a JSON list of channel keys, e.g. [\"in_app\", \"discord\"]."))
        return v


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def pingboard_automation(request):
    if request.method == "POST":
        p = request.POST
        action = p.get("action")
        if action in ("create", "edit"):
            instance = get_object_or_404(AutomationRule, pk=p.get("rule_id")) if action == "edit" else None
            form = AutomationRuleForm(p, instance=instance)
            if form.is_valid():
                rule = form.save()  # `enabled` untouched (not a form field) → stays disabled/current
                audit_log(request.user,
                          "pingboard.automation.updated" if action == "edit" else "pingboard.automation.created",
                          target_type="pingboard_automation_rule", target_id=rule.key, ip=client_ip(request))
                messages.success(request, _("Rule updated.") if action == "edit" else _("Rule created (disabled)."))
                return redirect("admin_audit:pingboard_automation")
            messages.error(request, _("Please correct the errors below."))
            return render(request, "admin_audit/console/pingboard/automation.html",
                          {"rules": AutomationRule.objects.select_related("template"),
                           "form": form, "editing": instance})
        rule = get_object_or_404(AutomationRule, pk=p.get("rule_id"))
        if action == "toggle":
            rule.enabled = not rule.enabled
            rule.save(update_fields=["enabled", "updated_at"])
            audit_log(request.user,
                      "pingboard.automation.enabled" if rule.enabled else "pingboard.automation.disabled",
                      target_type="pingboard_automation_rule", target_id=rule.key, ip=client_ip(request))
            messages.success(request, _("Rule enabled.") if rule.enabled else _("Rule disabled."))
        elif action == "delete":
            rule.delete()
            audit_log(request.user, "pingboard.automation.deleted",
                      target_type="pingboard_automation_rule", target_id=rule.key, ip=client_ip(request))
            messages.success(request, _("Rule deleted."))
        return redirect("admin_audit:pingboard_automation")

    edit_pk = request.GET.get("edit")
    editing = (
        AutomationRule.objects.filter(pk=edit_pk).first()
        if edit_pk and edit_pk.isdigit() else None
    )
    return render(request, "admin_audit/console/pingboard/automation.html", {
        "rules": AutomationRule.objects.select_related("template"),
        "form": AutomationRuleForm(instance=editing),
        "editing": editing,
    })


# --- templates ---------------------------------------------------------------
@login_required
@role_required(rbac.ROLE_DIRECTOR)
def pingboard_templates(request):
    if request.method == "POST":
        p = request.POST
        action = p.get("action")
        if action == "create":
            tpl = AlertTemplate(key=p.get("key", "")[:60], label=p.get("label", "Template")[:120],
                                category=p.get("category", ""), subject=p.get("subject", "")[:200],
                                body=p.get("body", ""), default_priority=p.get("default_priority", "normal"))
            tpl.save()
            audit_log(request.user, "pingboard.template.created", target_type="pingboard_template",
                      target_id=tpl.key, ip=client_ip(request))
            messages.success(request, _("Template created."))
        else:
            tpl = get_object_or_404(AlertTemplate, pk=p.get("template_id"))
            if action == "toggle":
                tpl.enabled = not tpl.enabled
                tpl.save(update_fields=["enabled", "updated_at"])
                messages.success(request, _("Template enabled.") if tpl.enabled else _("Template disabled."))
            elif action == "delete":
                tpl.delete()
                audit_log(request.user, "pingboard.template.deleted", target_type="pingboard_template",
                          target_id=str(p.get("template_id")), ip=client_ip(request))
                messages.success(request, _("Template deleted."))
        return redirect("admin_audit:pingboard_templates")
    ctx = {"templates": AlertTemplate.objects.all(), "categories": AlertCategory.choices,
           "priorities": AlertPriority.choices}
    return render(request, "admin_audit/console/pingboard/templates.html", ctx)
