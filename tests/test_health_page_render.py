"""The integration-health page must actually render — including the scanner panel.

Nothing rendered this page in the suite. That is a real gap for a template whose whole
job is to be looked at: `health.py` is well covered, but a typo'd variable or an unclosed
tag in `health.html` fails only at render time, so the tests could stay green while the
page 500s for the one person who visits it during an incident.

The vulnerability panel makes that worse. It is the surface that tells leadership whether
the CVE scanners are still running, so if it breaks, the failure mode is a page nobody can
open reporting on controls nobody can see — precisely the "unnoticed" problem the scanners
exist to prevent, one level up.
"""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

HEALTH_URL = "/ops/health/"


def _user(django_user_model, name, *roles):
    user = django_user_model.objects.create(username=name)
    for r in roles:
        RoleAssignment.objects.create(user=user, role=ensure_role(r))
    return user


def test_health_page_renders_for_a_director(client, django_user_model, sde):
    """The whole template compiles and renders with a real (empty) database."""
    client.force_login(_user(django_user_model, "ceo", rbac.ROLE_DIRECTOR))
    response = client.get(HEALTH_URL)
    assert response.status_code == 200


def test_the_scanner_panel_is_on_the_page(client, django_user_model, sde):
    """Both scanners are listed by name, so a missing one is visible as an absence."""
    client.force_login(_user(django_user_model, "ceo", rbac.ROLE_DIRECTOR))
    body = client.get(HEALTH_URL).content.decode()

    assert "Vulnerability scanning" in body
    assert "Python dependencies" in body
    assert "Container images" in body


def test_a_never_run_scanner_reads_as_unknown_not_clean(client, django_user_model, sde):
    """With no scan on record the panel must not imply health.

    This is the assertion that matters. An empty database has never run either scanner,
    which is indistinguishable from "the scanner stopped weeks ago" — and both must read
    as *unknown*. A panel that renders a never-scanned control as clean would be worse
    than no panel, because it manufactures the confidence it is supposed to earn.
    """
    client.force_login(_user(django_user_model, "ceo", rbac.ROLE_DIRECTOR))
    body = client.get(HEALTH_URL).content.decode()

    # Assert on the rendered CHIP, not on the bare words: the page's own explanatory
    # copy contains "clean" and "unknown" as prose, so a substring search over the whole
    # body would pass no matter what the scanners actually reported.
    assert '!text-gold">unknown<' in body, "a never-run scanner must render the unknown chip"
    assert '!text-kill">clean<' not in body, (
        "a scanner that has never run rendered as clean — the page is claiming health "
        "it has no evidence for"
    )


def test_the_panel_reports_a_real_finding(client, django_user_model, sde):
    """A stored vulnerable result renders as vulnerable, with its count."""
    from django.utils import timezone

    from apps.admin_audit.image_scan import IMAGE_SCAN_SETTING_KEY
    from apps.admin_audit.models import AppSetting

    AppSetting.objects.update_or_create(
        key=IMAGE_SCAN_SETTING_KEY,
        defaults={"value": {
            "status": "vulnerable",
            "as_of": timezone.now().isoformat(),
            "vuln_count": 3,
            "fixable_count": 3,
            "complete": True,
        }},
    )
    client.force_login(_user(django_user_model, "ceo", rbac.ROLE_DIRECTOR))
    body = client.get(HEALTH_URL).content.decode()

    assert "vulnerable" in body
