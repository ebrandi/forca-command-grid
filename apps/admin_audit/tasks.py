"""Celery tasks: data-retention enforcement and the dependency-vulnerability scan."""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

from .services import enforce_member_leave, enforce_retention

log = logging.getLogger("forca.security")

# Stable identity for the single open "dependencies are vulnerable" finding, so the
# weekly scan updates one Recommendation rather than piling up duplicates.
_REC_SUBJECT_TYPE = "security"
_REC_SUBJECT_ID = "dependency_audit"


@shared_task(name="admin_audit.enforce_retention")
def run_retention() -> dict:
    return enforce_retention()


@shared_task(name="admin_audit.enforce_member_leave")
def run_member_leave() -> dict:
    """Apply the on-member-leave retention policy to departed members' data.

    Ships DISARMED: report-only (counts what it would delete, deletes nothing) until a
    Director arms it on the retention page, so leadership can review the impact first.
    """
    return enforce_member_leave()


@shared_task(name="admin_audit.audit_dependencies")
def audit_dependencies() -> dict:
    """Weekly dependency-vulnerability scan; raises a director Recommendation on
    findings and clears it once they're resolved."""
    from .dependency_audit import run_dependency_audit

    summary = run_dependency_audit()
    _sync_recommendation(summary)
    return summary


@shared_task(name="admin_audit.scan_integration_health")
def scan_integration_health() -> dict:
    """ADM-3 (2.2): fire one deduped director alert when a background sync stops, the
    SDE goes stale, or a dependency CVE appears. Deduped + no-op when disabled."""
    from .health_alert import scan_integration_health as _scan

    return _scan()


def _sync_recommendation(summary: dict) -> None:
    """Mirror the scan result into an idempotent, director-visible Recommendation.

    On findings: create/refresh one open finding. On a confirmed-clean scan: close
    it. On an *error* scan: leave any open finding as-is (a failed scan must never
    be mistaken for "all clear").
    """
    from apps.recommendations.models import Recommendation

    open_states = [Recommendation.State.NEW, Recommendation.State.ACKNOWLEDGED]
    existing = Recommendation.objects.filter(
        type=Recommendation.Type.OFFICER_ACTION,
        subject_type=_REC_SUBJECT_TYPE,
        subject_id=_REC_SUBJECT_ID,
        state__in=open_states,
    ).first()

    status = summary.get("status")
    if status == "vulnerable":
        vulns = summary.get("vulns", [])
        detail = "; ".join(
            f"{v['name']} {v['version']} → {v['id']}"
            + (f" (fix: {', '.join(v['fix_versions'])})" if v["fix_versions"] else " (no fix yet)")
            for v in vulns[:20]
        )
        n = summary.get("vuln_count", len(vulns))
        message = (
            f"{n} known vulnerabilit{'y' if n == 1 else 'ies'} in installed "
            f"dependencies. Bump the affected package(s) and redeploy. {detail}"
        )
        fields = {
            "message": message,
            "severity": 80,
            "confidence": Recommendation.Confidence.HIGH,
            "required_permission": "director",
            "logic_summary": "Weekly pip-audit scan of installed dependencies.",
            "inputs": {"vulns": vulns[:50], "as_of": summary.get("as_of")},
            "data_freshness": timezone.now(),
        }
        if existing:
            for key, value in fields.items():
                setattr(existing, key, value)
            existing.state = Recommendation.State.NEW  # re-surface if it was acknowledged
            existing.save()
        else:
            Recommendation.objects.create(
                type=Recommendation.Type.OFFICER_ACTION,
                subject_type=_REC_SUBJECT_TYPE,
                subject_id=_REC_SUBJECT_ID,
                **fields,
            )
    elif status == "ok" and existing:
        # Confirmed clean — retire the finding.
        existing.state = Recommendation.State.SUPERSEDED
        existing.closed_at = timezone.now()
        existing.save(update_fields=["state", "closed_at", "updated_at"])
