"""Admin Console — External comms access sync (Director-gated).

Config writes funnel through ``apps.comms_access.config`` (validate → persist → version
bump → cache bust) then ``audit_log``. Mappings are the **managed-set boundary**: only a
role/group that appears here is ever touched by the reconcile. Everything ships in dry-run.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.comms_access import config, credentials
from apps.comms_access.entitlements import ENTITLEMENTS
from apps.comms_access.models import (
    AccessSyncLedger,
    CommsAccount,
    EntitlementMapping,
    MappingMode,
    Platform,
    PlatformCredential,
)
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

_BACK = "admin_audit:comms_access_settings"


def _int(v, d):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _save_discord_credentials(request) -> None:
    """Upsert the Discord PlatformCredential from the form.

    Secret fields (bot token, OAuth client secret) are **write-only**: a blank submission
    keeps the stored value untouched, and an explicit "clear" checkbox wipes it. Non-secret
    fields (client id, callback URL) are set verbatim. No secret is ever logged.
    """
    p = request.POST
    cred, _ = PlatformCredential.objects.get_or_create(platform=Platform.DISCORD)
    cred.oauth_client_id = p.get("discord_client_id", "").strip()
    cred.oauth_callback_url = p.get("discord_callback_url", "").strip()

    if p.get("clear_bot_token") == "on":
        cred.bot_token = ""
    elif p.get("discord_bot_token", "").strip():
        cred.bot_token = p.get("discord_bot_token", "").strip()

    if p.get("clear_client_secret") == "on":
        cred.oauth_client_secret = ""
    elif p.get("discord_client_secret", "").strip():
        cred.oauth_client_secret = p.get("discord_client_secret", "").strip()

    cred.save()


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def comms_access_settings(request):
    """General switches + per-platform arming."""
    if request.method == "POST":
        p = request.POST
        domain = p.get("domain")
        try:
            if domain == "general":
                config.set("general", {
                    "enabled": p.get("enabled") == "on",
                    "global_dry_run": p.get("global_dry_run") == "on",
                    "revoke_grace_minutes": _int(p.get("revoke_grace_minutes"), 0),
                }, user=request.user)
            elif domain == "platforms":
                platforms = config.get("platforms")
                for name in config.PLATFORMS:
                    row = dict(platforms.get(name, {}))
                    row["armed"] = p.get(f"{name}_armed") == "on"
                    row["kick_enabled"] = p.get(f"{name}_kick") == "on"
                    id_field = {"discord": "guild_id", "slack": "workspace_id", "mumble": "server_id"}[name]
                    row[id_field] = p.get(f"{name}_id", "").strip()
                    platforms[name] = row
                config.set("platforms", platforms, user=request.user)
            elif domain == "credentials":
                _save_discord_credentials(request)
                audit_log(request.user, "comms_access.credentials.update",
                          target_type="comms_access_config", target_id="discord",
                          metadata={"platform": "discord"}, ip=client_ip(request))
                messages.success(request, "Discord credentials saved.")
                return redirect(_BACK)
            else:
                messages.error(request, "Unknown settings section.")
                return redirect(_BACK)
        except config.ConfigError as exc:
            messages.error(request, str(exc))
            return redirect(_BACK)
        audit_log(request.user, "comms_access.config.update", target_type="comms_access_config",
                  target_id=domain, metadata={"domain": domain}, ip=client_ip(request))
        messages.success(request, "Settings saved.")
        return redirect(_BACK)

    platforms = config.get("platforms")
    id_field = {"discord": "guild_id", "slack": "workspace_id", "mumble": "server_id"}
    platform_rows = []
    for name in config.PLATFORMS:
        row = platforms.get(name, {})
        platform_rows.append({
            "name": name,
            "armed": bool(row.get("armed")),
            "kick_enabled": bool(row.get("kick_enabled")),
            "id_label": id_field[name],
            "id_value": row.get(id_field[name], ""),
        })
    cred = PlatformCredential.objects.filter(platform=Platform.DISCORD).first()
    discord_credentials = {
        "oauth_client_id": cred.oauth_client_id if cred else "",
        "oauth_callback_url": cred.oauth_callback_url if cred else "",
        "has_bot_token": credentials.discord_bot_token_configured(),
        "has_client_secret": bool(cred and cred.has_oauth_client_secret)
        or bool(getattr(settings, "DISCORD_OAUTH_CLIENT_SECRET", "")),
        "oauth_enabled": credentials.discord_oauth_enabled(),
        "bot_token_from_env": not (cred and cred.has_bot_token)
        and bool(getattr(settings, "DISCORD_BOT_TOKEN", "")),
        "suggested_callback": request.build_absolute_uri(
            reverse("comms_access:discord_callback")
        ),
    }
    ctx = {
        "general": config.get("general"),
        "platform_rows": platform_rows,
        "discord_credentials": discord_credentials,
        "meta": config.meta("general"),
    }
    return render(request, "admin_audit/console/comms_access/settings.html", ctx)


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def comms_access_mappings(request):
    """Entitlement → platform-role mappings (the managed set)."""
    ctx = {
        "mappings": EntitlementMapping.objects.all(),
        "entitlements": ENTITLEMENTS,
        "platforms": Platform.choices,
        "modes": MappingMode.choices,
    }
    return render(request, "admin_audit/console/comms_access/mappings.html", ctx)


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def comms_access_mapping_save(request, pk=None):
    if request.method != "POST":
        return redirect("admin_audit:comms_access_mappings")
    p = request.POST
    platform = p.get("platform", "")
    entitlement_key = p.get("entitlement_key", "")
    target_ref = p.get("target_ref", "").strip()

    valid_platforms = {c[0] for c in Platform.choices}
    if platform not in valid_platforms or entitlement_key not in ENTITLEMENTS or not target_ref:
        messages.error(request, "Platform, entitlement and target are required.")
        return redirect("admin_audit:comms_access_mappings")

    fields = {
        "platform": platform,
        "entitlement_key": entitlement_key,
        "target_type": p.get("target_type", "role").strip() or "role",
        "target_ref": target_ref,
        "target_label": p.get("target_label", "").strip(),
        "mode": p.get("mode") if p.get("mode") in {m[0] for m in MappingMode.choices} else MappingMode.ADDITIVE,
        "dry_run": p.get("dry_run") == "on",
        "enabled": p.get("enabled") == "on",
    }
    if pk:
        mapping = get_object_or_404(EntitlementMapping, pk=pk)
        for k, v in fields.items():
            setattr(mapping, k, v)
        try:
            mapping.save()
        except Exception:  # noqa: BLE001 - unique-constraint clash
            messages.error(request, "A mapping for that platform + entitlement + target already exists.")
            return redirect("admin_audit:comms_access_mappings")
        action = "comms_access.mapping.update"
    else:
        mapping, created = EntitlementMapping.objects.get_or_create(
            platform=platform, entitlement_key=entitlement_key, target_ref=target_ref,
            defaults=fields,
        )
        if not created:
            messages.error(request, "That mapping already exists.")
            return redirect("admin_audit:comms_access_mappings")
        action = "comms_access.mapping.create"
    audit_log(request.user, action, target_type="comms_mapping", target_id=mapping.pk,
              metadata={"platform": platform, "entitlement": entitlement_key, "mode": fields["mode"]},
              ip=client_ip(request))
    messages.success(request, "Mapping saved.")
    return redirect("admin_audit:comms_access_mappings")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def comms_access_mapping_delete(request, pk):
    if request.method != "POST":
        return redirect("admin_audit:comms_access_mappings")
    mapping = get_object_or_404(EntitlementMapping, pk=pk)
    mid = mapping.pk
    mapping.delete()
    audit_log(request.user, "comms_access.mapping.delete", target_type="comms_mapping",
              target_id=mid, ip=client_ip(request))
    messages.success(request, "Mapping removed.")
    return redirect("admin_audit:comms_access_mappings")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def comms_access_status(request):
    """Linked accounts + the recent sync ledger (audit of what changed / would change)."""
    ctx = {
        "accounts": CommsAccount.objects.select_related("user").all()[:200],
        "ledger": AccessSyncLedger.objects.select_related("account", "account__user").all()[:100],
    }
    return render(request, "admin_audit/console/comms_access/status.html", ctx)
