"""Admin & audit views: director-facing audit log review."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from core import rbac
from core.exporting import csv_safe as _csv_safe
from core.rbac import role_required

from .health import integration_health
from .models import AuditLog


def _audit_csv(rows) -> HttpResponse:
    import csv
    import json

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="forca-audit-log.csv"'
    writer = csv.writer(response)
    writer.writerow(["when", "actor", "action", "target_type", "target_id", "ip", "metadata"])
    for row in rows:
        actor = row.actor.display_name if row.actor else "system"
        writer.writerow([
            _csv_safe(row.created_at.isoformat()), _csv_safe(actor), _csv_safe(row.action),
            _csv_safe(row.target_type), _csv_safe(row.target_id), _csv_safe(row.ip or ""),
            _csv_safe(json.dumps(row.metadata, separators=(",", ":"))),
        ])
    return response


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def audit_log_view(request: HttpRequest) -> HttpResponse:
    """Investigable audit log: filter by who/what/target/when, and export to CSV.

    Director-gated. The export honours the same filters, capped at 5000 rows so an
    investigation gets a bounded, reviewable file.
    """
    from django.db.models import Q
    from django.utils.dateparse import parse_date

    f_actor = (request.GET.get("actor") or "").strip()
    f_action = (request.GET.get("action") or "").strip()
    f_target = (request.GET.get("target") or "").strip()
    f_from = (request.GET.get("from") or "").strip()
    f_to = (request.GET.get("to") or "").strip()

    qs = AuditLog.objects.select_related("actor")
    if f_actor:
        qs = qs.filter(
            Q(actor__username__icontains=f_actor)
            | Q(actor__first_name__icontains=f_actor)
            | Q(actor__characters__name__icontains=f_actor)
        ).distinct()
    if f_action:
        qs = qs.filter(action__icontains=f_action)
    if f_target:
        qs = qs.filter(Q(target_type__icontains=f_target) | Q(target_id__icontains=f_target))
    if (d_from := parse_date(f_from)) is not None:
        qs = qs.filter(created_at__date__gte=d_from)
    if (d_to := parse_date(f_to)) is not None:
        qs = qs.filter(created_at__date__lte=d_to)
    qs = qs.order_by("-created_at")

    if request.GET.get("export") == "csv":
        return _audit_csv(qs[:5000])

    filters = {"actor": f_actor, "action": f_action, "target": f_target, "from": f_from, "to": f_to}
    return render(request, "admin_audit/audit.html", {
        "logs": qs[:500],
        "filters": filters,
        "has_filters": any(filters.values()),
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def health_view(request: HttpRequest) -> HttpResponse:
    """Director view: ESI token status and per-feed sync freshness."""
    return render(request, "admin_audit/health.html", {"health": integration_health()})
