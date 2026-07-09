"""Operational Campaign UI — the campaign planner web surface (doc 16 §2.6).

These pin the officer gate, the compose → milestone/COA wiring, the launch lifecycle,
and the member block, all through the real URLs the views are mounted at.
"""
from __future__ import annotations

import pytest

from apps.command_intel.models import (
    Campaign,
    CampaignMilestone,
    CourseOfAction,
    IntelligenceSnapshot,
)
from apps.identity.models import RoleAssignment
from apps.sso.services import ensure_role
from core import rbac


def _user(django_user_model, role: str, suffix: str = ""):
    user, _ = django_user_model.objects.get_or_create(username=f"camp-{role}{suffix}")
    RoleAssignment.objects.get_or_create(user=user, role=ensure_role(role))
    return user


def _coa(slug: str, *, delta=3, priority: int = 60) -> CourseOfAction:
    return CourseOfAction.objects.create(
        slug=slug, objective=f"Stage hulls for {slug}", readiness_delta=delta,
        confidence=0.7, priority=priority, state=CourseOfAction.State.PROPOSED,
    )


@pytest.mark.django_db
def test_officer_reaches_campaign_surfaces(client, django_user_model, sde):
    client.force_login(_user(django_user_model, rbac.ROLE_OFFICER))
    assert client.get("/command/campaigns/").status_code == 200
    assert client.get("/command/campaigns/new/").status_code == 200


@pytest.mark.django_db
def test_member_is_blocked_from_campaigns(client, django_user_model, sde):
    client.force_login(_user(django_user_model, rbac.ROLE_MEMBER, suffix="-m"))
    # role_required raises PermissionDenied (403); a login redirect is also acceptable.
    assert client.get("/command/campaigns/").status_code in (403, 302)


@pytest.mark.django_db
def test_compose_creates_campaign_with_milestones_and_links_coas(client, django_user_model, sde):
    IntelligenceSnapshot.objects.create(slices={"readiness": {"overall_index": 80}})
    officer = _user(django_user_model, rbac.ROLE_OFFICER, suffix="-c")
    a, b = _coa("ui/a"), _coa("ui/b")
    client.force_login(officer)

    resp = client.post(
        "/command/campaigns/compose/",
        {
            "objective": "Raise Combat posture 80 → 90 before deploy",
            "target_metric": "readiness.overall",
            "target_value": "90",
            "coa_ids": [str(a.pk), str(b.pk)],
        },
    )
    assert resp.status_code == 302

    campaign = Campaign.objects.get()
    assert campaign.status == Campaign.Status.DRAFT
    assert campaign.milestones.count() == 2
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.campaign_id == campaign.pk and b.campaign_id == campaign.pk

    # The redirect lands on the planner and it renders end-to-end.
    detail = client.get(resp.url)
    assert detail.status_code == 200
    assert b"Stage hulls for ui/a" in detail.content


@pytest.mark.django_db
def test_compose_then_launch_activates(client, django_user_model, sde):
    IntelligenceSnapshot.objects.create(slices={"readiness": {"overall_index": 82}})
    officer = _user(django_user_model, rbac.ROLE_OFFICER, suffix="-l")
    a = _coa("ui/launch")
    client.force_login(officer)

    client.post(
        "/command/campaigns/compose/",
        {"objective": "Launch me", "target_metric": "readiness.overall", "coa_ids": [str(a.pk)]},
    )
    campaign = Campaign.objects.get()
    assert campaign.status == Campaign.Status.DRAFT

    resp = client.post(f"/command/campaigns/{campaign.pk}/launch/")
    assert resp.status_code in (302, 303)
    campaign.refresh_from_db()
    assert campaign.status == Campaign.Status.ACTIVE
    assert campaign.start_at is not None
    assert campaign.milestones.first().status == CampaignMilestone.Status.PENDING
