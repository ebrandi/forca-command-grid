"""Admin Console: Combat Signatures health, settings, background curation and moderation.

The leadership surface for the pilot-authored public banner feature (WS-7, plan A12). Mirrors the
hand-wired console pattern (``apps/admin_audit/console_combat.py``): a views module here, routes in
``apps/admin_audit/urls.py``, a hub card, and per-screen role gates. Nothing auto-discovers.

Role split follows the existing console precedent:

* **Dashboard + search** are Officer-gated read/triage screens (mirroring the ``combat_rewards``
  review queue and the ``raffle_hub`` / ``planetary_hub`` overviews). The per-signature moderation
  actions (admin disable/enable, force re-render) live on the search screen at the same Officer tier
  the reward review actions use.
* **Settings + background curation** are Director-gated (leadership config, mirroring
  ``combat_reward_settings`` and ``combat_ranks``).
* **Maintenance** (re-render all, orphan cleanup) is the Director-gated ``_MAINTENANCE_TASKS``
  registry in ``console.py`` — surfaced here as convenience buttons that POST to the same endpoint.

Every mutation writes an immutable ``AuditLog`` row via ``core.audit``. Admin disable/enable route
through the system-level ``signatures.admin_disable`` / ``signatures.admin_enable`` helpers, which
skip the owner (LP-4) ceiling; the render_error a pilot never sees is admin-visible here.
"""
from __future__ import annotations

import hashlib
import os

from django import forms
from django.conf import settings as dj_settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Count, Min, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from . import signature_assets, signatures, tasks
from .models import CombatSignature, CombatSignatureSettings, SignatureBackground

_INPUT = {"class": "input-field"}
_NAV_CACHE_KEY = "kb:sig:nav_enabled"

# Activity windows offered as the default period (mirrors signatures.PERIODS; labels localised).
_PERIOD_CHOICES = [
    ("7d", _("Last 7 days")),
    ("30d", _("Last 30 days")),
    ("90d", _("Last 90 days")),
    ("month", _("This month")),
    ("lastmonth", _("Last month")),
    ("all", _("All time")),
]

# The settings fields whose old→new values are recorded verbatim in the audit trail (all bool/int,
# none sensitive). FK / list / enum changes are recorded by field name only.
_AUDITED_SCALARS = (
    "enabled", "max_active_per_pilot", "refresh_interval_hours",
    "snapshots_enabled", "revoke_on_leave", "max_featured_trophies",
)
_AUDITED_OTHER = ("default_layout", "default_period", "default_background_id", "allowed_size_presets")

# The per-signature moderation verbs the admin action endpoint accepts.
_ADMIN_ACTIONS = frozenset({"admin_disable", "admin_enable", "regenerate"})


# --------------------------------------------------------------------------- #
#  Settings form (Director)
# --------------------------------------------------------------------------- #
class SignatureSettingsForm(forms.ModelForm):
    """ModelForm over the ``CombatSignatureSettings`` singleton (plan A12 leadership options)."""

    default_period = forms.ChoiceField(choices=_PERIOD_CHOICES, widget=forms.Select(attrs=_INPUT))
    allowed_size_presets = forms.MultipleChoiceField(
        required=False,
        choices=CombatSignature.SizePreset.choices,
        widget=forms.CheckboxSelectMultiple,
        help_text=_("Which banner sizes pilots may choose. Leave all ticked to allow every size."),
    )

    class Meta:
        model = CombatSignatureSettings
        fields = [
            "enabled", "max_active_per_pilot", "refresh_interval_hours", "snapshots_enabled",
            "revoke_on_leave", "max_featured_trophies", "default_background", "default_layout",
            "default_period", "allowed_size_presets",
        ]
        widgets = {
            "max_active_per_pilot": forms.NumberInput(attrs={**_INPUT, "min": 1, "max": 50}),
            "refresh_interval_hours": forms.NumberInput(attrs={**_INPUT, "min": 1, "max": 168}),
            "max_featured_trophies": forms.NumberInput(attrs={**_INPUT, "min": 0, "max": 12}),
            "default_layout": forms.Select(attrs=_INPUT),
            "default_background": forms.Select(attrs=_INPUT),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_background"].required = False
        self.fields["default_background"].queryset = (
            SignatureBackground.objects.order_by("display_order", "key")
        )

    def clean_max_active_per_pilot(self) -> int:
        n = self.cleaned_data.get("max_active_per_pilot")
        if n is not None and n < 1:
            raise forms.ValidationError(_("Pilots must be allowed at least one signature."))
        return n

    def clean_refresh_interval_hours(self) -> int:
        n = self.cleaned_data.get("refresh_interval_hours")
        if n is not None and n < 1:
            raise forms.ValidationError(_("The refresh interval must be at least one hour."))
        return n


# --------------------------------------------------------------------------- #
#  Dashboard helpers (all read-only, bounded)
# --------------------------------------------------------------------------- #
def _max_failures() -> int:
    return int(getattr(dj_settings, "SIGNATURE_RENDER_MAX_FAILURES", 5))


def _storage_and_orphans() -> dict:
    """Walk ``MEDIA_ROOT/signatures`` once, defensively: total artifact bytes + count, and how many
    are orphans (a token with no ACTIVE/FROZEN signature — the read-only mirror of the cleanup
    janitor's predicate). Never raises on a missing dir or an unreadable file."""
    directory = os.path.join(dj_settings.MEDIA_ROOT, "signatures")
    present = os.path.isdir(directory)
    total_bytes = file_count = orphans = 0
    if present:
        keep = {CombatSignature.Status.ACTIVE, CombatSignature.Status.FROZEN}
        status_by_token = dict(CombatSignature.objects.values_list("public_token", "status"))
        for name in os.listdir(directory):
            if not name.endswith(".png"):
                continue
            try:
                total_bytes += os.path.getsize(os.path.join(directory, name))
            except OSError:
                continue
            file_count += 1
            token = name[:-4]
            if signatures.TOKEN_RE.match(token) and status_by_token.get(token) not in keep:
                orphans += 1
    return {"present": present, "bytes": total_bytes, "count": file_count, "orphans": orphans}


def _verify_committed_files(entry: dict) -> str | None:
    """Recompute each committed file's SHA-256 and compare to the manifest. Returns the first
    problem as a human string, or None when every file matches. Paths are repo-root-relative."""
    for name, meta in (entry.get("files") or {}).items():
        rel = (meta or {}).get("path")
        want = (meta or {}).get("sha256")
        if not rel or not want:
            continue
        path = os.path.join(dj_settings.BASE_DIR, rel)
        try:
            with open(path, "rb") as fh:
                got = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            return _("committed file “%(f)s” is missing") % {"f": name}
        if got != want:
            return _("committed file “%(f)s” does not match its recorded checksum") % {"f": name}
    return None


def _provenance_status() -> dict:
    """Prove the shipped background art is intact: the manifest loads, and every ENABLED background's
    committed files still hash to the manifest's recorded checksums. Mismatches are surfaced loudly
    on the dashboard (tamper / bad-deploy signal)."""
    try:
        manifest = signature_assets.load_manifest()
    except (OSError, ValueError) as exc:  # missing file or malformed JSON
        return {"ok": False, "manifest_error": str(exc)[:200], "checked": 0, "mismatches": []}
    entries = {b["key"]: b for b in manifest.get("backgrounds", [])}
    mismatches: list[dict] = []
    checked = 0
    for bg in SignatureBackground.objects.filter(enabled=True).order_by("display_order", "key"):
        checked += 1
        entry = entries.get(bg.key)
        if entry is None:
            mismatches.append({"key": bg.key, "reason": _("not present in the manifest")})
            continue
        if bg.checksum and entry.get("checksum") and bg.checksum != entry["checksum"]:
            mismatches.append({"key": bg.key,
                               "reason": _("database checksum is out of sync with the manifest")})
            continue
        problem = _verify_committed_files(entry)
        if problem:
            mismatches.append({"key": bg.key, "reason": problem})
    return {"ok": not mismatches, "manifest_error": None, "checked": checked, "mismatches": mismatches}


# --------------------------------------------------------------------------- #
#  Dashboard (Officer)
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_OFFICER)
def signatures_dashboard(request: HttpRequest) -> HttpResponse:
    """Render health: status/render-status counts, oldest pending, parked failures (with the
    admin-only render_error), recent failures, storage + orphan estimate, background catalogue and
    provenance status, and the current settings snapshot. Every query is aggregated or sliced."""
    RenderStatus = CombatSignature.RenderStatus
    status_counts = {
        row["status"]: row["n"]
        for row in CombatSignature.objects.values("status").annotate(n=Count("pk"))
    }
    render_counts = {
        row["render_status"]: row["n"]
        for row in CombatSignature.objects.values("render_status").annotate(n=Count("pk"))
    }
    max_failures = _max_failures()
    parked = list(
        CombatSignature.objects.filter(consecutive_failures__gte=max_failures)
        .select_related("character").order_by("-updated_at")[:50]
    )
    recent_failures = list(
        CombatSignature.objects.filter(render_status=RenderStatus.FAILED)
        .select_related("character").order_by("-updated_at")[:10]
    )
    oldest_pending = (
        CombatSignature.objects.filter(render_status=RenderStatus.PENDING)
        .aggregate(m=Min("updated_at"))["m"]
    )
    bg_summary = SignatureBackground.objects.aggregate(
        total=Count("pk"), enabled=Count("pk", filter=Q(enabled=True))
    )
    bg_summary["disabled"] = (bg_summary["total"] or 0) - (bg_summary["enabled"] or 0)
    ctx = {
        "cfg": CombatSignatureSettings.load(),
        "total": CombatSignature.objects.count(),
        "status_counts": status_counts,
        "render_counts": render_counts,
        "parked": parked,
        "parked_count": CombatSignature.objects.filter(consecutive_failures__gte=max_failures).count(),
        "max_failures": max_failures,
        "recent_failures": recent_failures,
        "oldest_pending": oldest_pending,
        "storage": _storage_and_orphans(),
        "bg_summary": bg_summary,
        "provenance": _provenance_status(),
    }
    return render(request, "admin_audit/console/signatures_dashboard.html", ctx)


# --------------------------------------------------------------------------- #
#  Settings (Director)
# --------------------------------------------------------------------------- #
def _canon(value):
    """A JSON-serialisable, canonical form of a settings value for the audit metadata."""
    if isinstance(value, list | tuple):
        return list(value)
    return value


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def signature_settings(request: HttpRequest) -> HttpResponse:
    """Edit the leadership singleton. On save: stamp ``updated_by``, bust the nav master-switch
    cache, and audit ``signatures.settings_update`` with the changed field names (old/new for the
    scalar knobs)."""
    cfg = CombatSignatureSettings.load()
    if request.method == "POST":
        before = {f: _canon(getattr(cfg, f)) for f in (*_AUDITED_SCALARS, *_AUDITED_OTHER)}
        form = SignatureSettingsForm(request.POST, instance=cfg)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.updated_by = request.user
            obj.save()
            # The nav template tag caches the master switch for 600s — drop it so a toggle surfaces
            # immediately rather than after the TTL.
            cache.delete(_NAV_CACHE_KEY)
            after = {f: _canon(getattr(obj, f)) for f in (*_AUDITED_SCALARS, *_AUDITED_OTHER)}
            changed = [f for f in (*_AUDITED_SCALARS, *_AUDITED_OTHER) if before[f] != after[f]]
            metadata = {"changed": changed}
            for f in _AUDITED_SCALARS:
                if before[f] != after[f]:
                    metadata[f] = {"old": before[f], "new": after[f]}
            audit_log(request.user, "signatures.settings_update",
                      target_type="combat_signature_settings", target_id=str(obj.pk),
                      metadata=metadata, ip=client_ip(request))
            messages.success(request, _("Combat Signature settings saved."))
            return redirect("admin_audit:signature_settings")
        messages.error(request, _("Please correct the errors below."))
    else:
        form = SignatureSettingsForm(instance=cfg)
    return render(request, "admin_audit/console/signatures_settings.html", {
        "form": form, "cfg": cfg,
    })


# --------------------------------------------------------------------------- #
#  Background curation (Director) — NO upload path of any kind
# --------------------------------------------------------------------------- #
@login_required
@role_required(rbac.ROLE_DIRECTOR)
def signature_backgrounds(request: HttpRequest) -> HttpResponse:
    """Enable/disable and reorder the seeded backgrounds (rows come from the committed manifest —
    there is no upload). A single POST saves every row; toggles and reorders are audited separately
    with the canonical set of changed keys."""
    if request.method == "POST":
        toggled: dict[str, bool] = {}
        reordered: dict[str, int] = {}
        for bg in SignatureBackground.objects.all():
            new_enabled = request.POST.get(f"enabled_{bg.pk}") == "1"
            raw_order = (request.POST.get(f"order_{bg.pk}") or "").strip()
            try:
                new_order = int(raw_order)
            except ValueError:
                new_order = bg.display_order
            update_fields = []
            if new_enabled != bg.enabled:
                bg.enabled = new_enabled
                toggled[bg.key] = new_enabled
                update_fields.append("enabled")
            if new_order != bg.display_order:
                bg.display_order = new_order
                reordered[bg.key] = new_order
                update_fields.append("display_order")
            if update_fields:
                bg.save(update_fields=[*update_fields, "updated_at"])
        if toggled:
            audit_log(request.user, "signatures.background_toggle", target_type="signature_background",
                      target_id="", metadata={"changed": toggled}, ip=client_ip(request))
        if reordered:
            audit_log(request.user, "signatures.background_reorder",
                      target_type="signature_background", target_id="",
                      metadata={"changed": reordered}, ip=client_ip(request))
        if toggled or reordered:
            messages.success(request, _("Background catalogue updated."))
        else:
            messages.info(request, _("No changes to save."))
        return redirect("admin_audit:signature_backgrounds")

    rows = [
        {
            "bg": bg,
            "thumb": f"killboard/sigbg/{bg.key}/thumb.png",
        }
        for bg in SignatureBackground.objects.order_by("display_order", "key")
    ]
    return render(request, "admin_audit/console/signatures_backgrounds.html", {
        "rows": rows,
        "enabled_count": sum(1 for r in rows if r["bg"].enabled),
    })


# --------------------------------------------------------------------------- #
#  Per-pilot search + moderation (Officer)
# --------------------------------------------------------------------------- #
def _admin_row(request: HttpRequest, sig: CombatSignature) -> dict:
    """The moderation view-model for one signature: public URL + admin-visible render state."""
    url = request.build_absolute_uri(reverse("signature_public", args=[sig.public_token]))
    return {
        "sig": sig,
        "public_url": url,
        "can_disable": sig.status != CombatSignature.Status.DISABLED,
        "can_enable": sig.status != CombatSignature.Status.ACTIVE,
    }


@login_required
@role_required(rbac.ROLE_OFFICER)
def signature_search(request: HttpRequest) -> HttpResponse:
    """Find a pilot's signatures by character-name substring or exact character id, with per-row
    moderation actions. Bounded to 100 rows; an empty query lists the most recent signatures."""
    q = (request.GET.get("q") or "").strip()
    qs = CombatSignature.objects.select_related("character", "background").order_by("-updated_at")
    if q:
        cond = Q(character__name__icontains=q)
        if q.isdigit():
            cond |= Q(character_id=int(q))
        qs = qs.filter(cond)
    rows = [_admin_row(request, sig) for sig in qs[:100]]
    return render(request, "admin_audit/console/signatures_search.html", {
        "rows": rows, "q": q, "total": qs.count(),
    })


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def signature_admin_action(request: HttpRequest, pk: int, action: str) -> HttpResponse:
    """One moderation POST endpoint: admin disable/enable (system helpers that skip the owner
    ceiling) or force a re-render. Each is audited (the domain helpers audit disable/enable; the
    re-render is audited here). Redirects back to the search screen, preserving the query."""
    if action not in _ADMIN_ACTIONS:
        raise Http404(_("Unknown action."))
    sig = get_object_or_404(CombatSignature, pk=pk)
    ip = client_ip(request)
    if action == "admin_disable":
        signatures.admin_disable(sig, actor=request.user, ip=ip)
        messages.success(request, _("Signature disabled."))
    elif action == "admin_enable":
        signatures.admin_enable(sig, actor=request.user, ip=ip)
        tasks.signature_render_task.delay(sig.pk)
        messages.success(request, _("Signature re-enabled."))
    else:  # regenerate
        tasks.signature_render_task.delay(sig.pk)
        audit_log(request.user, "signatures.admin_regenerate", target_type="combat_signature",
                  target_id=str(sig.pk), metadata={"character_id": sig.character_id}, ip=ip)
        messages.success(request, _("Re-render queued."))
    q = (request.POST.get("q") or "").strip()
    url = reverse("admin_audit:signature_search")
    return redirect(f"{url}?q={q}" if q else url)
