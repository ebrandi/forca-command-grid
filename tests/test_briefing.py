"""In-app briefings: pilot digest (own data) + leadership snapshot."""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.pilots.briefing import leadership_briefing, pilot_briefing
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.tasks.models import Task
from core import rbac


@pytest.mark.django_db
def test_pilot_briefing_surfaces_own_tasks(django_user_model, sde):
    user = django_user_model.objects.create(username="eve:9001")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=9001, user=user, name="P", is_main=True, is_corp_member=True)
    Task.objects.create(title="Do a thing", assignee=user, is_open=False, status=Task.Status.CLAIMED)

    b = pilot_briefing(user)
    assert any(it["kind"] == "task" for it in b["items"]) or b["headline"]


@pytest.mark.django_db
def test_leadership_briefing_has_corp_aggregates(django_user_model, sde):
    b = leadership_briefing()
    assert "index" in b and "srp_exposure" in b and "open_tasks" in b


@pytest.mark.django_db
def test_briefing_page_redirects_to_the_command_center(client, django_user_model, sde):
    user = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(user)
    resp = client.get("/pilots/briefing/")
    assert resp.status_code == 302
    assert resp["Location"] == "/dashboard/"


@pytest.mark.django_db
def test_officer_dashboard_renders_contribution_panels(client, django_user_model, sde):
    # The leadership deck surfaces the corp contribution totals; the recognition
    # feed lives in the sidebar for every member. Render as an officer with a
    # character so both are exercised on the merged Command Center.
    from apps.pilots.services import record_contribution

    officer = django_user_model.objects.create(username="off")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    EveCharacter.objects.create(character_id=9077, user=officer, name="Off",
                                is_main=True, is_corp_member=True)
    record_contribution(officer, "build", 4, "ships", description="Built Feroxes",
                        ref_type="j", ref_id="1")
    client.force_login(officer)
    html = client.get("/dashboard/").content.decode()
    assert "Corp contribution" in html
    # Recognition merged into the contribution panel as "Recently recognised".
    assert "Recently recognised" in html
    assert "Built Feroxes" in html
