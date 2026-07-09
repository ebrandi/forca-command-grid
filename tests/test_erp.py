"""Industrial ERP: claim, deliver→stock+credit, blueprint coverage."""
from __future__ import annotations

import pytest

from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.erp import services
from apps.erp.models import Blueprint, BuildJob
from apps.identity.models import RoleAssignment
from apps.pilots.models import ContributionEvent
from apps.sso.services import ensure_role
from apps.stockpile.models import Stockpile, StockpileItem
from core import rbac

RIFTER = 587


def _member(django_user_model, name, *roles):
    user = django_user_model.objects.create(username=name)
    for r in roles:
        RoleAssignment.objects.create(user=user, role=ensure_role(r))
    return user


@pytest.mark.django_db
def test_claim_then_deliver_updates_stock_and_credits(django_user_model, sde):
    builder = _member(django_user_model, "builder", rbac.ROLE_MEMBER)
    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=5)

    assert services.claim(job, builder) is True
    job.refresh_from_db()
    assert job.owner_id == builder.id and job.status == BuildJob.Status.BUILDING
    # Second claimer loses the race.
    assert services.claim(job, _member(django_user_model, "other", rbac.ROLE_MEMBER)) is False

    services.deliver(job, builder)
    job.refresh_from_db()
    assert job.status == BuildJob.Status.DELIVERED
    # Corp stock gained 5 hulls.
    item = StockpileItem.objects.get(stockpile__kind=Stockpile.Kind.CORP, type_id=RIFTER)
    assert item.quantity_current == 5
    # Builder credited once (idempotent).
    assert ContributionEvent.objects.filter(
        user=builder, kind="build", ref_type="build_job", ref_id=str(job.pk)
    ).count() == 1


@pytest.mark.django_db
def test_blueprint_coverage_flags_missing_hull(django_user_model, sde):
    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    d = Doctrine.objects.create(name="Rifter Fleet", category=cat)
    DoctrineFit.objects.create(doctrine=d, name="Rifter", ship_type_id=RIFTER)

    cov = services.blueprint_coverage()
    assert any(g["type_id"] == RIFTER for g in cov["gaps"])

    Blueprint.objects.create(
        owner_type=Blueprint.Owner.CORPORATION, type_id=999, product_type_id=RIFTER
    )
    cov = services.blueprint_coverage()
    assert any(c["type_id"] == RIFTER for c in cov["covered"])
    assert not any(g["type_id"] == RIFTER for g in cov["gaps"])


@pytest.mark.django_db
def test_create_job_is_officer_only(client, django_user_model, sde):
    client.force_login(_member(django_user_model, "m", rbac.ROLE_MEMBER))
    assert client.post("/erp/jobs/create/", {"output_type_id": RIFTER, "quantity": 2}).status_code == 403
    # /erp/ now consolidates into the Industry Center Job Tracker.
    assert client.get("/erp/").status_code == 302
    client.force_login(_member(django_user_model, "fc", rbac.ROLE_OFFICER))
    assert client.post("/erp/jobs/create/", {"output_type_id": RIFTER, "quantity": 2}).status_code == 302
    assert BuildJob.objects.filter(output_type_id=RIFTER).exists()


@pytest.mark.django_db
def test_job_blocks_when_materials_short_then_unblocks(django_user_model, sde):
    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=1)
    # No corp stock yet: a buildable job whose materials are short is flagged BLOCKED.
    services.recheck_block(job)
    job.refresh_from_db()
    assert job.status == BuildJob.Status.BLOCKED
    assert job.blocked_reason
    # A blocked job cannot be claimed.
    assert services.claim(job, _member(django_user_model, "b1", rbac.ROLE_MEMBER)) is False
    # Stock the full BOM into corp stock: the job returns to QUEUED and is claimable.
    sp = Stockpile.objects.create(name="Corp", kind=Stockpile.Kind.CORP)
    for line in services.job_materials(job)["lines"]:
        StockpileItem.objects.create(
            stockpile=sp, type_id=line["type_id"], quantity_current=line["need"]
        )
    services.recheck_block(job)
    job.refresh_from_db()
    assert job.status == BuildJob.Status.QUEUED
    assert job.blocked_reason == ""
    assert services.claim(job, _member(django_user_model, "b2", rbac.ROLE_MEMBER)) is True
