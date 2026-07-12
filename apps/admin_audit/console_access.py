"""Admin Console: Access governance.

Register partner alliances and friendly corporations that get the same
logistics / buyback / store access as the home alliance. Replaces the Django-admin-only
management of these access-policy rows. Director-gated and audit-logged; the access
effect is live (see apps.corporation.access.is_service_alliance_pilot).
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required


def _int_or(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _resolve_name(entity_id: int) -> str:
    """Best-effort ESI name for an alliance/corp id. Never raises — a failed lookup just
    leaves the name blank (the id alone grants access). Runs one synchronous ESI call, so
    it adds that round-trip's latency to a create-with-blank-name; acceptable for a
    Director-gated, single-id write."""
    try:
        from apps.corporation.models import EveName
        from core.esi.names import resolve_ids

        resolve_ids([entity_id])
        row = EveName.objects.filter(entity_id=entity_id).values_list("name", flat=True).first()
        return row or ""
    except Exception:  # noqa: BLE001 - the id alone grants access; the name is cosmetic
        return ""


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def access_governance(request: HttpRequest) -> HttpResponse:
    """List + manage partner alliances and friendly corporations."""
    from apps.corporation.models import FriendlyCorporation, PartnerAlliance

    return render(request, "admin_audit/console/access.html", {
        "partner_alliances": PartnerAlliance.objects.all(),
        "friendly_corps": FriendlyCorporation.objects.all(),
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def partner_alliance_save(request: HttpRequest, alliance_id: int | None = None) -> HttpResponse:
    from apps.corporation.models import PartnerAlliance

    note = (request.POST.get("note") or "").strip()
    active = request.POST.get("active") == "on"
    if alliance_id is None:
        alliance_id = _int_or(request.POST.get("entity_id"))
        if not alliance_id:
            messages.error(request, _("Enter a valid alliance id."))
            return redirect("admin_audit:access_governance")
        name = (request.POST.get("name") or "").strip() or _resolve_name(alliance_id)
        PartnerAlliance.objects.update_or_create(
            alliance_id=alliance_id,
            defaults={"name": name, "note": note, "active": active},
        )
        action = "create"
    else:
        row = get_object_or_404(PartnerAlliance, alliance_id=alliance_id)
        row.name = (request.POST.get("name") or "").strip()
        row.note = note
        row.active = active
        row.save(update_fields=["name", "note", "active"])
        action = "update"
    audit_log(request.user, f"access.partner_alliance.{action}", target_type="partner_alliance",
              target_id=str(alliance_id), ip=client_ip(request))
    messages.success(request, _("Partner alliance saved."))
    return redirect("admin_audit:access_governance")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def partner_alliance_delete(request: HttpRequest, alliance_id: int) -> HttpResponse:
    from apps.corporation.models import PartnerAlliance

    PartnerAlliance.objects.filter(alliance_id=alliance_id).delete()
    audit_log(request.user, "access.partner_alliance.delete", target_type="partner_alliance",
              target_id=str(alliance_id), ip=client_ip(request))
    messages.success(request, _("Partner alliance removed."))
    return redirect("admin_audit:access_governance")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def friendly_corp_save(request: HttpRequest, corporation_id: int | None = None) -> HttpResponse:
    from apps.corporation.models import FriendlyCorporation

    note = (request.POST.get("note") or "").strip()
    active = request.POST.get("active") == "on"
    if corporation_id is None:
        corporation_id = _int_or(request.POST.get("entity_id"))
        if not corporation_id:
            messages.error(request, _("Enter a valid corporation id."))
            return redirect("admin_audit:access_governance")
        name = (request.POST.get("name") or "").strip() or _resolve_name(corporation_id)
        FriendlyCorporation.objects.update_or_create(
            corporation_id=corporation_id,
            defaults={"name": name, "note": note, "active": active},
        )
        action = "create"
    else:
        row = get_object_or_404(FriendlyCorporation, corporation_id=corporation_id)
        row.name = (request.POST.get("name") or "").strip()
        row.note = note
        row.active = active
        row.save(update_fields=["name", "note", "active"])
        action = "update"
    audit_log(request.user, f"access.friendly_corp.{action}", target_type="friendly_corporation",
              target_id=str(corporation_id), ip=client_ip(request))
    messages.success(request, _("Friendly corporation saved."))
    return redirect("admin_audit:access_governance")


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def friendly_corp_delete(request: HttpRequest, corporation_id: int) -> HttpResponse:
    from apps.corporation.models import FriendlyCorporation

    FriendlyCorporation.objects.filter(corporation_id=corporation_id).delete()
    audit_log(request.user, "access.friendly_corp.delete", target_type="friendly_corporation",
              target_id=str(corporation_id), ip=client_ip(request))
    messages.success(request, _("Friendly corporation removed."))
    return redirect("admin_audit:access_governance")
