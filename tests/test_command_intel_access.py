"""Command Intelligence — the classification access gate (doc 14).

Classification is enforced against the RBAC rank ladder with BOTH a per-object check
(``can_view_report``) and a queryset filter (``visible_reports``) — the proven
``apps.kb`` pattern — so a lower-clearance viewer can neither read nor enumerate a
higher-classified report.
"""
from __future__ import annotations

import pytest

from apps.command_intel import access
from apps.command_intel.models import Classification, IntelligenceReport
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, role):
    user = django_user_model.objects.create(username=f"ci-{role}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


@pytest.mark.django_db
def test_director_eyes_only_report_visible_to_directors_only(django_user_model):
    member = _user(django_user_model, rbac.ROLE_MEMBER)
    officer = _user(django_user_model, rbac.ROLE_OFFICER)
    director = _user(django_user_model, rbac.ROLE_DIRECTOR)
    dr = IntelligenceReport.objects.create(
        classification=Classification.DIRECTOR_EYES_ONLY, status=IntelligenceReport.Status.READY
    )

    assert dr in access.visible_reports(director)
    assert dr not in access.visible_reports(member)
    assert dr not in access.visible_reports(officer)


@pytest.mark.django_db
def test_can_view_report_gate_by_classification(django_user_model):
    member = _user(django_user_model, rbac.ROLE_MEMBER)
    director = _user(django_user_model, rbac.ROLE_DIRECTOR)
    dr = IntelligenceReport.objects.create(
        classification=Classification.DIRECTOR_EYES_ONLY, status=IntelligenceReport.Status.READY
    )

    assert access.can_view_report(member, dr) is False
    assert access.can_view_report(director, dr) is True


@pytest.mark.django_db
def test_corp_internal_report_visible_to_member(django_user_model):
    member = _user(django_user_model, rbac.ROLE_MEMBER)
    ci = IntelligenceReport.objects.create(
        classification=Classification.CORP_INTERNAL, status=IntelligenceReport.Status.READY
    )

    assert access.can_view_report(member, ci) is True
    assert ci in access.visible_reports(member)
