"""Report classification access control (design doc 14, ADR-0007).

Classification is ENFORCED against the RBAC rank ladder, copying the proven
``apps.kb`` visibility pattern: ``can_view_report`` (detail 403) + ``visible_reports``
(list filter), so a lower-clearance viewer cannot even enumerate a higher-classified
report. The banner in the UI is the visible half of this gate — both read this one
``tier_min_rank`` map.
"""
from __future__ import annotations

from django.db.models import Q

from core import rbac

from . import config
from .models import Classification, CourseOfAction, IntelligenceReport

# Design floor — a tier may be made *stricter* via config but never more permissive
# than this (also enforced in config._validate_classification).
_FLOOR = {
    Classification.CORP_INTERNAL: rbac.ROLE_MEMBER,
    Classification.HIGH_COMMAND: rbac.ROLE_OFFICER,
    Classification.DIRECTOR_EYES_ONLY: rbac.ROLE_DIRECTOR,
    Classification.ALLIANCE_COMMAND: rbac.ROLE_DIRECTOR,
}


def _min_rank_for(classification: str) -> int:
    role = config.get("classification").get("tier_min_rank", {}).get(classification)
    if role in rbac.ROLE_RANK:
        return rbac.ROLE_RANK[role]
    return rbac.ROLE_RANK[_FLOOR.get(classification, rbac.ROLE_DIRECTOR)]


def _alliance_audience(user) -> bool:
    # v1: directors are the alliance interface. Future: a configured alliance-officer
    # audience via apps.corporation.access (doc 14 §3).
    return rbac.has_role(user, rbac.ROLE_DIRECTOR)


def can_view_report(user, report) -> bool:
    """Whether ``user`` is cleared for ``report``'s classification (detail gate)."""
    if rbac.effective_rank(user) < _min_rank_for(report.classification):
        return False
    if report.classification == Classification.ALLIANCE_COMMAND:
        return _alliance_audience(user)
    return True


def visible_reports(user, qs=None):
    """Filter a report queryset to what ``user`` may see (list gate)."""
    qs = IntelligenceReport.objects.all() if qs is None else qs
    rank = rbac.effective_rank(user)
    allowed = [tier for tier, _label in Classification.choices if rank >= _min_rank_for(tier)]
    if Classification.ALLIANCE_COMMAND in allowed and not _alliance_audience(user):
        allowed = [t for t in allowed if t != Classification.ALLIANCE_COMMAND]
    return qs.filter(classification__in=allowed)


def can_view_coa(user, coa) -> bool:
    """Whether ``user`` may act on / read a Course of Action. A COA inherits its
    report's classification; a COA with no report carries no classified content and
    is visible. Mirrors ``can_view_report`` for the COA decision/compose surfaces,
    which previously trusted only the officer role."""
    if coa.report_id is None:
        return True
    return can_view_report(user, coa.report)


def visible_coas(user, qs=None):
    """Filter a COA queryset to what ``user`` is cleared for (report classification),
    keeping report-less COAs. The list-side counterpart of ``can_view_coa``."""
    qs = CourseOfAction.objects.all() if qs is None else qs
    return qs.filter(Q(report__isnull=True) | Q(report__classification__in=allowed_classifications(user)))


def allowed_classifications(user) -> list[str]:
    """The classification tiers ``user`` is cleared to read (for retrieval filtering)."""
    rank = rbac.effective_rank(user)
    allowed = [tier for tier, _label in Classification.choices if rank >= _min_rank_for(tier)]
    if Classification.ALLIANCE_COMMAND in allowed and not _alliance_audience(user):
        allowed = [t for t in allowed if t != Classification.ALLIANCE_COMMAND]
    return allowed


def max_clearance(user) -> str:
    """The most-sensitive classification tier ``user`` can read (audit of the ceiling)."""
    allowed = allowed_classifications(user)
    return max(allowed, key=_min_rank_for) if allowed else ""


def can_reclassify(user) -> bool:
    """Only directors may raise/lower a report's classification (doc 14 §5)."""
    return rbac.has_role(user, rbac.ROLE_DIRECTOR)
