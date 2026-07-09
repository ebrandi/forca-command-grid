"""Job-management on the Industry Center Job Tracker: cancel / edit / convert, and
the creator/owner/officer permission model (can_manage)."""
from __future__ import annotations

import pytest

from apps.erp import services
from apps.erp.models import BuildJob
from apps.identity.models import RoleAssignment
from apps.industry.models import IndustryProject
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

RIFTER = 587


def _user(django_user_model, name, role=rbac.ROLE_MEMBER):
    u = django_user_model.objects.create(username=name)
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    return u


# --- permission model -------------------------------------------------------
def test_can_manage_matrix(django_user_model):
    creator = _user(django_user_model, "creator")
    other = _user(django_user_model, "other")
    builder = _user(django_user_model, "builder")
    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=1, created_by=creator)

    # Unclaimed: creator + officer can manage; a random member cannot.
    assert services.can_manage(creator, job, is_officer=False) is True
    assert services.can_manage(other, job, is_officer=False) is False
    assert services.can_manage(other, job, is_officer=True) is True

    # Once claimed by someone else, the creator loses control; the builder gains it.
    job.owner = builder
    job.save(update_fields=["owner"])
    assert services.can_manage(builder, job, is_officer=False) is True
    assert services.can_manage(creator, job, is_officer=False) is False


# --- cancel -----------------------------------------------------------------
def test_creator_cancels_own_unclaimed_job(client, django_user_model, sde):
    creator = _user(django_user_model, "creator")
    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=1, created_by=creator)
    client.force_login(creator)
    resp = client.post(f"/erp/jobs/{job.pk}/cancel/")
    assert resp.status_code == 302
    job.refresh_from_db()
    assert job.status == BuildJob.Status.CANCELLED


def test_member_cannot_cancel_others_job(client, django_user_model, sde):
    creator = _user(django_user_model, "creator")
    other = _user(django_user_model, "other")
    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=1, created_by=creator)
    client.force_login(other)
    client.post(f"/erp/jobs/{job.pk}/cancel/")
    job.refresh_from_db()
    assert job.status != BuildJob.Status.CANCELLED  # untouched


def test_officer_cancels_any_job(client, django_user_model, sde):
    creator = _user(django_user_model, "creator")
    officer = _user(django_user_model, "off", rbac.ROLE_OFFICER)
    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=1, created_by=creator)
    client.force_login(officer)
    client.post(f"/erp/jobs/{job.pk}/cancel/")
    job.refresh_from_db()
    assert job.status == BuildJob.Status.CANCELLED


# --- edit -------------------------------------------------------------------
def test_creator_edits_quantity(client, django_user_model, sde):
    creator = _user(django_user_model, "creator")
    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=1, created_by=creator)
    client.force_login(creator)
    client.post(f"/erp/jobs/{job.pk}/edit/", {"quantity": "5"})
    job.refresh_from_db()
    assert job.quantity == 5


def test_cannot_edit_claimed_job(client, django_user_model, sde):
    creator = _user(django_user_model, "creator")
    job = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=1, created_by=creator,
        owner=creator, status=BuildJob.Status.BUILDING,
    )
    client.force_login(creator)
    client.post(f"/erp/jobs/{job.pk}/edit/", {"quantity": "9"})
    job.refresh_from_db()
    assert job.quantity == 1  # building jobs aren't editable


def test_update_quantity_refuses_a_claimed_job(django_user_model, sde):
    """The atomic, lock-time guard: a job that's no longer queued can't be edited
    (closes the TOCTOU where a claim lands between the check and the save)."""
    job = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=1, status=BuildJob.Status.BUILDING
    )
    assert services.update_quantity(job, 9) is False
    job.refresh_from_db()
    assert job.quantity == 1


# --- mark built -------------------------------------------------------------
def test_owner_marks_built(client, django_user_model, sde):
    owner = _user(django_user_model, "owner")
    job = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=1, owner=owner, status=BuildJob.Status.BUILDING
    )
    client.force_login(owner)
    client.post(f"/erp/jobs/{job.pk}/status/", {"status": "built"})
    job.refresh_from_db()
    assert job.status == BuildJob.Status.BUILT


# --- state-machine safety (adversarial-review findings) ---------------------
def test_status_endpoint_cannot_reach_delivered(client, django_user_model, sde):
    """A member owner must NOT be able to POST status=delivered — delivery only via
    deliver() (which credits corp stock + the builder). Tampering is a silent no-op."""
    owner = _user(django_user_model, "owner")
    job = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=1, owner=owner, status=BuildJob.Status.BUILDING
    )
    client.force_login(owner)
    client.post(f"/erp/jobs/{job.pk}/status/", {"status": "delivered"})
    job.refresh_from_db()
    assert job.status == BuildJob.Status.BUILDING  # unchanged; not delivered
    from apps.stockpile.models import StockpileItem
    assert not StockpileItem.objects.filter(type_id=RIFTER).exists()  # no phantom stock


def test_cannot_deliver_a_cancelled_job(django_user_model, sde):
    owner = _user(django_user_model, "owner")
    job = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=5, owner=owner, status=BuildJob.Status.CANCELLED
    )
    assert services.deliver(job, owner) is None  # refused
    job.refresh_from_db()
    assert job.status == BuildJob.Status.CANCELLED
    from apps.stockpile.models import StockpileItem
    assert not StockpileItem.objects.filter(type_id=RIFTER).exists()


# --- convert imported ESI job to a plan ------------------------------------
def test_plan_from_job(client, django_user_model, priced_sde):
    user = _user(django_user_model, "pilot")
    client.force_login(user)
    resp = client.post("/industry/jobs/plan-from-job/", {"type_id": RIFTER, "quantity": 3})
    assert resp.status_code == 302
    plan = IndustryProject.objects.get(source=IndustryProject.Source.ESI_JOB)
    item = plan.items.get()
    assert item.type_id == RIFTER and item.quantity == 3  # Rifter yields 1/run → 3 units


def test_plan_from_job_scales_runs_by_output_per_run(client, django_user_model, priced_sde):
    # Reacted Alloy (800) is a reaction yielding 200 per run; 1 run → 200 units.
    client.force_login(_user(django_user_model, "pilot"))
    client.post("/industry/jobs/plan-from-job/", {"type_id": 800, "quantity": 1})
    plan = IndustryProject.objects.get(source=IndustryProject.Source.ESI_JOB)
    assert plan.items.get().quantity == 200


# --- the page surfaces management for the creator only ----------------------
def test_tracker_shows_manage_for_creator_only(client, django_user_model, priced_sde):
    creator = _user(django_user_model, "creator")
    other = _user(django_user_model, "other")
    BuildJob.objects.create(output_type_id=RIFTER, quantity=1, created_by=creator)

    client.force_login(creator)
    assert b"/cancel/" in client.get("/industry/jobs/").content  # creator sees Cancel
    client.force_login(other)
    assert b"/cancel/" not in client.get("/industry/jobs/").content  # others don't
