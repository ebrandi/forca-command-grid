"""Audit logging: a single helper that writes immutable AuditLog rows.

Used for every sensitive action — especially officer access to a member's
private data (see ADR-0001 / handbooks/contributor-handbook/security-guidelines.md).
"""
from __future__ import annotations

from typing import Any

from django.apps import apps


def audit_log(
    actor,
    action: str,
    *,
    target_type: str = "",
    target_id: str = "",
    metadata: dict[str, Any] | None = None,
    ip: str = "",
) -> None:
    """Write an audit record. Never raises into the caller's flow."""
    AuditLog = apps.get_model("admin_audit", "AuditLog")
    try:
        AuditLog.objects.create(
            actor=actor if getattr(actor, "pk", None) else None,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            metadata=metadata or {},
            ip=ip or "",
        )
    except Exception:  # noqa: BLE001 - auditing must not break the request
        import logging

        logging.getLogger("forca.audit").exception("Failed to write audit log: %s", action)


def client_ip(request) -> str:
    """Best-effort client IP for audit records.

    Behind a single trusted reverse proxy (our nginx appends REMOTE_ADDR to
    X-Forwarded-For), the right-most XFF entry is the real client; left-most
    entries are client-supplied and spoofable, so we never trust them.
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")
