"""Supply Command board — one officer-gated page; director sections stripped server-side.

The hub's ``is_director`` template guard is chrome; the authority is HERE — the view drops
every ``role="director"`` section from the payload BEFORE the template (and the CSV) can
see it, so a composed board enforces section gating internally (acceptance №9).
"""
from __future__ import annotations

import csv

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required

from . import providers
from .board import board_data


def _render_section(section) -> dict:
    return {
        "key": section.key,
        "title": providers.render_title(section),
        "role": section.role,
        "total": section.total,
        "shown": len(section.rows),
        "source_url": section.source_url,
        "freshness": section.freshness,
        "unavailable": section.total == -1,
        "rows": [
            {
                "severity": row.severity,
                "label": providers.render_label(row),
                "action": providers.render_action(row),
                "url": row.url,
            }
            for row in section.rows
        ],
    }


@login_required
@role_required(rbac.ROLE_OFFICER)
def board(request: HttpRequest) -> HttpResponse:
    """The Supply Command board — every phase's problems in one place, each deep-linked."""
    is_director = rbac.has_role(request.user, rbac.ROLE_DIRECTOR)
    data = board_data()
    # Server-side section gate: ISK-bearing (director) sections never reach a non-director's
    # response body — filtered here, not just hidden in the template.
    sections = [s for s in data["sections"] if s.role != "director" or is_director]

    if request.GET.get("export") == "csv":
        return _export_csv(sections)

    view_sections = [_render_section(s) for s in sections]
    total_reds = sum(
        1 for s in sections for r in s.rows if r.severity == "red"
    )
    return render(request, "supplyboard/board.html", {
        "sections": view_sections,
        "built_at": data["built_at"],
        "is_director": is_director,
        "total_reds": total_reds,
    })


def _export_csv(sections) -> HttpResponse:
    """Officer sections as CSV — column keys AND row keys stay machine-stable English."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="supply-board.csv"'
    writer = csv.writer(response)
    writer.writerow(["section", "severity", "key", "label_key", "url"])
    for s in sections:
        if s.role != "officer":  # margin/erosion stays out of the CSV entirely
            continue
        for r in s.rows:
            writer.writerow([s.key, r.severity, r.key, r.label_key, r.url])
    return response


@login_required
@role_required(rbac.ROLE_OFFICER)
@require_POST
def refresh(request: HttpRequest) -> HttpResponse:
    """Rebuild the board cache now (audited)."""
    board_data(refresh=True)
    audit_log(request.user, "supplyboard.refresh", ip=client_ip(request))
    messages.success(request, _("Supply Command board refreshed."))
    return redirect("supplyboard:board")
