"""IND-1 (roadmap 3.3) — Plan-to-Job bridge.

A plan's buildable lines can be pushed to the job board as claimable BuildJobs; delivery
flows back to update the plan's status (and corp stock + builder credit via deliver()).
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.erp.models import BuildJob
from apps.erp.services import deliver
from apps.identity.models import RoleAssignment
from apps.industry.jobs_bridge import push_project_to_jobs
from apps.industry.models import IndustryProject, IndustryProjectItem
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

RIFTER = 587
SCOURGE = 2629
BUILD = IndustryProjectItem.BuildOrBuy.BUILD
BUY = IndustryProjectItem.BuildOrBuy.BUY


def _user(django_user_model, cid, role=rbac.ROLE_MEMBER):
    u = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    EveCharacter.objects.create(character_id=cid, user=u, name=f"P{cid}", is_main=True,
                                is_corp_member=True)
    return u


def _project(user, *items):
    p = IndustryProject.objects.create(name="Fleet resupply", created_by=user, assigned_to=user)
    for type_id, qty, bob in items:
        IndustryProjectItem.objects.create(project=p, type_id=type_id, quantity=qty, build_or_buy=bob)
    return p


def _deliver_job(job, user):
    job.status = BuildJob.Status.BUILT
    job.save(update_fields=["status"])
    deliver(job, user)


def test_push_creates_jobs_for_build_lines_only(django_user_model):
    user = _user(django_user_model, 8001)
    p = _project(user, (RIFTER, 5, BUILD), (SCOURGE, 1000, BUY))
    assert push_project_to_jobs(p, user) == 1  # only the BUILD line
    job = BuildJob.objects.get(source_item__project=p)
    assert job.output_type_id == RIFTER and job.quantity == 5
    p.refresh_from_db()
    assert p.status == IndustryProject.Status.ACTIVE  # DRAFT → ACTIVE on first push


def test_push_is_idempotent(django_user_model):
    user = _user(django_user_model, 8002)
    p = _project(user, (RIFTER, 5, BUILD))
    assert push_project_to_jobs(p, user) == 1
    assert push_project_to_jobs(p, user) == 0  # existing open job not duplicated
    assert BuildJob.objects.filter(source_item__project=p).count() == 1


def test_delivery_marks_plan_done(django_user_model):
    user = _user(django_user_model, 8003)
    p = _project(user, (RIFTER, 5, BUILD))
    push_project_to_jobs(p, user)
    _deliver_job(BuildJob.objects.get(source_item__project=p), user)
    p.refresh_from_db()
    assert p.status == IndustryProject.Status.DONE


def test_partial_delivery_keeps_plan_active(django_user_model):
    user = _user(django_user_model, 8004)
    p = _project(user, (RIFTER, 5, BUILD), (SCOURGE, 10, BUILD))
    push_project_to_jobs(p, user)
    jobs = list(BuildJob.objects.filter(source_item__project=p).order_by("id"))
    _deliver_job(jobs[0], user)
    p.refresh_from_db()
    assert p.status == IndustryProject.Status.ACTIVE  # one build line still undelivered
    _deliver_job(jobs[1], user)
    p.refresh_from_db()
    assert p.status == IndustryProject.Status.DONE


def test_cannot_push_closed_plan(client, django_user_model):
    owner = _user(django_user_model, 8007)
    p = _project(owner, (RIFTER, 5, BUILD))
    p.status = IndustryProject.Status.DONE
    p.save(update_fields=["status"])
    client.force_login(owner)
    assert client.post(reverse("industry:push_jobs", args=[p.pk])).status_code == 302
    assert BuildJob.objects.filter(source_item__project=p).count() == 0  # closed → nothing pushed


def test_push_jobs_view_requires_manage(client, django_user_model):
    owner = _user(django_user_model, 8005)
    other = _user(django_user_model, 8006)
    p = _project(owner, (RIFTER, 5, BUILD))
    url = reverse("industry:push_jobs", args=[p.pk])

    client.force_login(other)  # a non-manager member can't push
    assert client.post(url).status_code in (403, 404)
    assert BuildJob.objects.filter(source_item__project=p).count() == 0

    client.force_login(owner)  # the creator/lead can
    assert client.post(url).status_code == 302
    assert BuildJob.objects.filter(source_item__project=p).count() == 1
