"""Task management: claim, progress, complete → contribution; permissions."""
from __future__ import annotations

import pytest

from apps.identity.models import RoleAssignment
from apps.pilots.models import ContributionEvent
from apps.sso.services import ensure_role
from apps.tasks import services
from apps.tasks.models import Task, TaskEvent
from core import rbac


def _user(django_user_model, name, *roles):
    user = django_user_model.objects.create(username=name)
    for r in roles:
        RoleAssignment.objects.create(user=user, role=ensure_role(r))
    return user


@pytest.mark.django_db
def test_member_claims_open_task(django_user_model):
    member = _user(django_user_model, "pilot", rbac.ROLE_MEMBER)
    task = Task.objects.create(title="Haul Rifters", type=Task.Type.MOVE, is_open=True)

    assert services.claim(task, member) is True
    task.refresh_from_db()
    assert task.assignee_id == member.id
    assert task.status == Task.Status.CLAIMED
    assert not task.is_open
    # Second claim by someone else fails (already taken).
    other = _user(django_user_model, "other", rbac.ROLE_MEMBER)
    assert services.claim(task, other) is False


@pytest.mark.django_db
def test_completion_credits_contribution_once(django_user_model):
    member = _user(django_user_model, "builder", rbac.ROLE_MEMBER)
    task = Task.objects.create(
        title="Build 5 hulls", type=Task.Type.BUILD, assignee=member,
        is_open=False, status=Task.Status.CLAIMED,
    )
    assert services.set_status(task, member, Task.Status.DONE) is True
    assert ContributionEvent.objects.filter(
        user=member, kind="task", ref_type="task", ref_id=str(task.pk)
    ).count() == 1
    # Idempotent: re-completing (no real transition) doesn't double-credit.
    services.set_status(task, member, Task.Status.DONE)
    assert ContributionEvent.objects.filter(user=member, kind="task").count() == 1
    assert TaskEvent.objects.filter(task=task, to_status="done").exists()


@pytest.mark.django_db
def test_only_assignee_or_officer_can_act(django_user_model):
    owner = _user(django_user_model, "owner", rbac.ROLE_MEMBER)
    stranger = _user(django_user_model, "stranger", rbac.ROLE_MEMBER)
    officer = _user(django_user_model, "fc", rbac.ROLE_OFFICER)
    task = Task.objects.create(title="t", assignee=owner, is_open=False, status=Task.Status.CLAIMED)

    assert services.can_act(owner, task, is_officer=False) is True
    assert services.can_act(stranger, task, is_officer=False) is False
    assert services.can_act(officer, task, is_officer=True) is True


@pytest.mark.django_db
def test_board_and_claim_endpoints(client, django_user_model):
    member = _user(django_user_model, "m", rbac.ROLE_MEMBER)
    client.force_login(member)
    task = Task.objects.create(title="Open one", is_open=True)

    assert client.get("/tasks/").status_code == 200
    resp = client.post(f"/tasks/{task.pk}/claim/")
    assert resp.status_code == 302
    task.refresh_from_db()
    assert task.assignee_id == member.id


@pytest.mark.django_db
def test_member_cannot_create_or_cancel(client, django_user_model):
    member = _user(django_user_model, "m2", rbac.ROLE_MEMBER)
    client.force_login(member)
    # Create is officer-gated.
    assert client.post("/tasks/create/", {"title": "x"}).status_code == 403
    # Cancel is officer-only even for own task.
    task = Task.objects.create(title="t", assignee=member, is_open=False, status=Task.Status.CLAIMED)
    client.post(f"/tasks/{task.pk}/status/", {"status": "cancelled"})
    task.refresh_from_db()
    assert task.status == Task.Status.CLAIMED  # unchanged


# --- SDE-2 (3.7): lifecycle hardening -----------------------------------------
@pytest.mark.django_db
def test_illogical_transition_is_blocked(django_user_model):
    officer = _user(django_user_model, "fc3", rbac.ROLE_OFFICER)
    task = Task.objects.create(title="t", status=Task.Status.DONE, is_open=False)
    # DONE is terminal — can't reopen.
    assert services.set_status(task, officer, Task.Status.OPEN) is False
    task.refresh_from_db()
    assert task.status == Task.Status.DONE


@pytest.mark.django_db
def test_officer_completing_unassigned_credits_the_actor(django_user_model):
    officer = _user(django_user_model, "fc4", rbac.ROLE_OFFICER)
    task = Task.objects.create(title="Ad-hoc", is_open=True, status=Task.Status.OPEN)  # no assignee
    assert services.set_status(task, officer, Task.Status.DONE) is True
    # Credit goes to the actor (the officer), not nobody.
    assert ContributionEvent.objects.filter(
        user=officer, kind="task", ref_id=str(task.pk)
    ).count() == 1


@pytest.mark.django_db
def test_edit_updates_and_audits(django_user_model):
    officer = _user(django_user_model, "fc5", rbac.ROLE_OFFICER)
    task = Task.objects.create(title="Old", priority=0, is_open=True, created_by=officer)
    assert services.edit_task(task, officer, title="New", priority=5, due_at=None) is True
    task.refresh_from_db()
    assert task.title == "New" and task.priority == 5
    assert TaskEvent.objects.filter(task=task, note__startswith="Edited").exists()


@pytest.mark.django_db
def test_edit_frozen_on_terminal_task(django_user_model):
    officer = _user(django_user_model, "fc6", rbac.ROLE_OFFICER)
    task = Task.objects.create(title="Done", status=Task.Status.DONE, is_open=False, created_by=officer)
    assert services.edit_task(task, officer, title="Nope", priority=9, due_at=None) is False


@pytest.mark.django_db
def test_reassign_and_unassign(django_user_model):
    officer = _user(django_user_model, "fc7", rbac.ROLE_OFFICER)
    a = _user(django_user_model, "eve:7001", rbac.ROLE_MEMBER)
    task = Task.objects.create(title="t", is_open=True, status=Task.Status.OPEN)
    assert services.reassign(task, officer, a) is True
    task.refresh_from_db()
    assert task.assignee_id == a.id and task.status == Task.Status.CLAIMED and not task.is_open
    # Unassign → back to the open pool.
    assert services.reassign(task, officer, None) is True
    task.refresh_from_db()
    assert task.assignee_id is None and task.status == Task.Status.OPEN and task.is_open
    assert TaskEvent.objects.filter(task=task, note="Unassigned").exists()


@pytest.mark.django_db
def test_unassign_in_progress_returns_to_open(django_user_model):
    officer = _user(django_user_model, "fc10", rbac.ROLE_OFFICER)
    a = _user(django_user_model, "eve:7010", rbac.ROLE_MEMBER)
    task = Task.objects.create(title="t", assignee=a, is_open=False, status=Task.Status.IN_PROGRESS)
    assert services.reassign(task, officer, None) is True
    task.refresh_from_db()
    # Not orphaned: an unassigned in-progress task returns to the claimable OPEN pool.
    assert task.status == Task.Status.OPEN and task.is_open and task.assignee_id is None


@pytest.mark.django_db
def test_edit_view_assignee_cannot_change_priority(client, django_user_model):
    officer = _user(django_user_model, "fc11", rbac.ROLE_OFFICER)
    a = _user(django_user_model, "eve:7011", rbac.ROLE_MEMBER)
    task = Task.objects.create(title="t", priority=3, created_by=officer, assignee=a,
                               is_open=False, status=Task.Status.CLAIMED)
    client.force_login(a)
    client.post(f"/tasks/{task.pk}/edit/", {"title": "renamed", "priority": "999", "due_at": ""})
    task.refresh_from_db()
    assert task.title == "renamed" and task.priority == 3  # assignee can rename, not reprioritize


@pytest.mark.django_db
def test_edit_view_rejects_malformed_due(client, django_user_model):
    from datetime import UTC, datetime

    officer = _user(django_user_model, "fc12", rbac.ROLE_OFFICER)
    task = Task.objects.create(title="t", priority=3, created_by=officer,
                               due_at=datetime(2030, 1, 1, tzinfo=UTC), is_open=True)
    client.force_login(officer)
    client.post(f"/tasks/{task.pk}/edit/", {"title": "x", "priority": "5", "due_at": "not-a-date"})
    task.refresh_from_db()
    assert task.due_at is not None and task.priority == 3  # malformed due → whole edit rejected


@pytest.mark.django_db
def test_detail_view_and_reassign_permission(client, django_user_model):
    member = _user(django_user_model, "m9", rbac.ROLE_MEMBER)
    task = Task.objects.create(title="t", is_open=True, status=Task.Status.OPEN)
    # Detail renders for a member.
    client.force_login(member)
    assert client.get(f"/tasks/{task.pk}/").status_code == 200
    # Reassign is officer-only.
    assert client.post(f"/tasks/{task.pk}/reassign/", {"assignee": ""}).status_code == 403


# --- AUTHZ-01 regression: task detail must not be a member-to-member IDOR ------
@pytest.mark.django_db
def test_detail_hides_other_members_task(client, django_user_model):
    """A member must not read the detail (title, description, actor/history) of a task they
    are not a party to. The board never lists another member's assigned task, and the detail
    read must apply the same scoping — otherwise pk-enumeration leaks internal task notes."""
    owner = _user(django_user_model, "owner_d", rbac.ROLE_MEMBER)
    stranger = _user(django_user_model, "stranger_d", rbac.ROLE_MEMBER)
    officer = _user(django_user_model, "fc_d", rbac.ROLE_OFFICER)
    # Assigned to owner, NOT in the claimable pool → private to owner/creator/officers.
    task = Task.objects.create(
        title="Sensitive haul", description="stage at a secret POS",
        assignee=owner, is_open=False, status=Task.Status.CLAIMED, created_by=officer,
    )
    # A member who is not a party gets 404 (no existence oracle), not the content.
    client.force_login(stranger)
    assert client.get(f"/tasks/{task.pk}/").status_code == 404
    # The assignee can see their own task.
    client.force_login(owner)
    assert client.get(f"/tasks/{task.pk}/").status_code == 200
    # An officer sees everything.
    client.force_login(officer)
    assert client.get(f"/tasks/{task.pk}/").status_code == 200


@pytest.mark.django_db
def test_detail_claimable_task_stays_visible_to_any_member(client, django_user_model):
    """The fix must not over-restrict: a task in the claimable OPEN pool is shown on every
    member's board, so its detail must stay readable to any member."""
    stranger = _user(django_user_model, "stranger_c", rbac.ROLE_MEMBER)
    task = Task.objects.create(title="Open one", is_open=True, status=Task.Status.OPEN)
    client.force_login(stranger)
    assert client.get(f"/tasks/{task.pk}/").status_code == 200
