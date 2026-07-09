"""EVE-client XML doctrine import (Director-gated).

A second bulk-import method alongside the ESI saved-fits importer: upload an XML
file exported from the EVE client, review a per-fitting classification, resolve
conflicts, then commit only the confirmed actions.

Flow: ``start`` (upload form) → ``upload`` (parse + classify into a
DoctrineImportBatch) → ``preview`` (review + resolve) → ``commit`` (apply) →
``result``. The batch is the single source of truth for the parsed fits; the
browser only ever carries per-fitting *decisions*, so a tampered form can change
what action is taken but never the fit that is written. The preview never
modifies the doctrine tables.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.doctrines import xml_import
from apps.doctrines.models import DoctrineImportBatch, DoctrineImportConfig
from apps.doctrines.xml_parser import (
    LIMITS,
    MAX_FITTINGS_CEILING,
    XmlImportError,
    clamp_max_fittings,
    parse_fittings_xml,
)
from core import rbac
from core.audit import audit_log, client_ip
from core.rbac import role_required


def _get_batch(request: HttpRequest, pk: int) -> DoctrineImportBatch:
    """Owner-scoped fetch — a director can only ever touch their OWN staging
    batch. Another director's (or a missing) batch is a 404 (IDOR guard)."""
    return get_object_or_404(DoctrineImportBatch, pk=pk, owner=request.user)


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def xml_import_start(request: HttpRequest) -> HttpResponse:
    config = DoctrineImportConfig.active()
    return render(request, "admin_audit/console/doctrine_xml/upload.html", {
        "limits": LIMITS,
        "max_mb": LIMITS["max_file_bytes"] // (1024 * 1024),
        "max_fittings": config.effective_max_fittings(),
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def xml_import_upload(request: HttpRequest) -> HttpResponse:
    uploaded = request.FILES.get("xml")
    config = DoctrineImportConfig.active()
    try:
        data, filename = xml_import.read_upload(uploaded)
        raw = parse_fittings_xml(data, max_fittings=config.effective_max_fittings())
        entries, counts = xml_import.classify_fittings(raw)
    except XmlImportError as exc:
        # ``exc`` is a safe, curated message — never raw upload content.
        messages.error(request, f"Import rejected: {exc}")
        return redirect("admin_audit:doctrine_xml_import")

    batch = xml_import.create_batch(request.user, filename, len(data), entries, counts)
    audit_log(
        request.user, "doctrine.xml_import.upload",
        target_type="doctrine_import_batch", target_id=str(batch.id),
        metadata={"filename": filename, "size": len(data), "counts": counts},
        ip=client_ip(request),
    )
    return redirect("admin_audit:doctrine_xml_preview", pk=batch.id)


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def xml_import_preview(request: HttpRequest, pk: int) -> HttpResponse:
    batch = _get_batch(request, pk)
    if batch.status == DoctrineImportBatch.Status.COMMITTED:
        return redirect("admin_audit:doctrine_xml_result", pk=batch.id)

    rows = [
        {
            **entry,
            "status_label": xml_import.STATUS_LABELS.get(entry["status"], entry["status"]),
            "actionable": xml_import.is_actionable(entry["status"]),
        }
        for entry in batch.payload
    ]
    return render(request, "admin_audit/console/doctrine_xml/preview.html", {
        "batch": batch,
        "rows": rows,
        "counts": batch.counts,
        "S": {
            "new": xml_import.STATUS_NEW,
            "identical": xml_import.STATUS_IDENTICAL,
            "conflict": xml_import.STATUS_CONFLICT,
            "duplicate_fit": xml_import.STATUS_DUPLICATE_FIT,
            "hull_conflict": xml_import.STATUS_HULL_CONFLICT,
            "invalid": xml_import.STATUS_INVALID,
        },
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
@require_POST
def xml_import_commit(request: HttpRequest, pk: int) -> HttpResponse:
    batch = _get_batch(request, pk)
    if batch.status != DoctrineImportBatch.Status.PREVIEW:
        return redirect("admin_audit:doctrine_xml_result", pk=batch.id)

    # A single fallback decision applied to any conflict left on its default.
    bulk_conflict = (request.POST.get("bulk_conflict") or "").strip().lower()
    decisions: dict[str, dict] = {}
    for entry in batch.payload:
        idx = str(entry["index"])
        action = (request.POST.get(f"action:{idx}") or "").strip().lower()
        if not action and bulk_conflict and entry["status"] == xml_import.STATUS_CONFLICT:
            action = bulk_conflict
        decisions[idx] = {"action": action, "new_name": request.POST.get(f"name:{idx}", "")}

    result = xml_import.commit_batch(batch, decisions, request.user)
    audit_log(
        request.user, "doctrine.xml_import.commit",
        target_type="doctrine_import_batch", target_id=str(batch.id),
        metadata={
            key: result[key]
            for key in ("created", "renamed", "replaced", "skipped", "identical", "rejected")
        },
        ip=client_ip(request),
    )
    messages.success(request, "XML import applied.")
    return redirect("admin_audit:doctrine_xml_result", pk=batch.id)


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def xml_import_result(request: HttpRequest, pk: int) -> HttpResponse:
    batch = _get_batch(request, pk)
    if batch.status != DoctrineImportBatch.Status.COMMITTED:
        return redirect("admin_audit:doctrine_xml_preview", pk=batch.id)
    return render(request, "admin_audit/console/doctrine_xml/result.html", {
        "batch": batch,
        "result": batch.result or {},
    })


@login_required
@role_required(rbac.ROLE_DIRECTOR)
def xml_import_settings(request: HttpRequest) -> HttpResponse:
    """Leadership settings for the XML importer — currently the per-import fitting
    cap (1…ceiling). Saved value is clamped into the safe range server-side."""
    config = DoctrineImportConfig.active()
    if request.method == "POST":
        raw = (request.POST.get("max_fittings_per_import") or "").strip()
        try:
            requested = int(raw)
        except ValueError:
            messages.error(request, "Enter a whole number.")
            return redirect("admin_audit:doctrine_xml_settings")
        clamped = clamp_max_fittings(requested)
        config.max_fittings_per_import = clamped
        config.save(update_fields=["max_fittings_per_import", "updated_at"])
        audit_log(
            request.user, "doctrine.xml_import.config",
            target_type="doctrine_import_config", target_id=str(config.id),
            metadata={"max_fittings_per_import": clamped}, ip=client_ip(request),
        )
        if clamped != requested:
            messages.info(
                request,
                f"Limit set to {clamped} (values are kept within 1–{MAX_FITTINGS_CEILING}).",
            )
        else:
            messages.success(request, f"Import limit set to {clamped} fittings.")
        return redirect("admin_audit:doctrine_xml_settings")

    return render(request, "admin_audit/console/doctrine_xml/settings.html", {
        "config": config,
        "ceiling": MAX_FITTINGS_CEILING,
    })
