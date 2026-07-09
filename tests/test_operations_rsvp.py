"""Operation RSVP/availability + time-aware gap urgency."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.identity.models import RoleAssignment
from apps.operations.models import Operation, OperationRsvp
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _member(django_user_model, cid, role=rbac.ROLE_MEMBER):
    user = django_user_model.objects.create(username=f"eve:{cid}")
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    EveCharacter.objects.create(character_id=cid, user=user, name=f"Pilot{cid}",
                                is_main=True, is_corp_member=True)
    return user


@pytest.mark.django_db
def test_rsvp_records_and_is_replaceable(client, django_user_model):
    member = _member(django_user_model, 8001)
    op = Operation.objects.create(name="Strat Op", target_at=timezone.now() + timedelta(days=3))
    client.force_login(member)

    assert client.post(f"/operations/{op.pk}/rsvp/", {"response": "yes"}).status_code == 302
    rsvp = OperationRsvp.objects.get(operation=op, user=member)
    assert rsvp.response == "yes" and rsvp.character_name == "Pilot8001"

    # Changing the answer updates the single row (no duplicates).
    client.post(f"/operations/{op.pk}/rsvp/", {"response": "maybe"})
    assert OperationRsvp.objects.filter(operation=op, user=member).count() == 1
    rsvp.refresh_from_db()
    assert rsvp.response == "maybe"

    # Bad value is rejected without changing the record.
    client.post(f"/operations/{op.pk}/rsvp/", {"response": "garbage"})
    rsvp.refresh_from_db()
    assert rsvp.response == "maybe"


@pytest.mark.django_db
def test_rsvp_summary_counts(client, django_user_model):
    from apps.operations.services import rsvp_summary

    op = Operation.objects.create(name="Fleet")
    for cid, resp in [(8011, "yes"), (8012, "yes"), (8013, "no")]:
        m = _member(django_user_model, cid)
        client.force_login(m)
        client.post(f"/operations/{op.pk}/rsvp/", {"response": resp})

    s = rsvp_summary(op)
    assert s["counts"] == {"yes": 2, "maybe": 0, "no": 1}
    assert s["committed"] == 2


@pytest.mark.django_db
def test_time_aware_urgency_and_at_risk(django_user_model):
    from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
    from apps.operations.models import OperationDoctrine
    from apps.operations.services import operation_readiness, urgency_for

    assert urgency_for(None) == "none"
    assert urgency_for(-1) == "overdue"
    assert urgency_for(1) == "critical"
    assert urgency_for(5) == "high"
    assert urgency_for(14) == "medium"
    assert urgency_for(40) == "low"

    # An op two days out with an unmet doctrine target → critical + at-risk gap.
    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    d = Doctrine.objects.create(name="Mega Fleet", category=cat)
    DoctrineFit.objects.create(doctrine=d, name="Megathron", ship_type_id=641)
    op = Operation.objects.create(name="Timer", target_at=timezone.now() + timedelta(days=2))
    OperationDoctrine.objects.create(operation=op, doctrine=d, target_count=10)

    r = operation_readiness(op)
    assert r["urgency"] == "critical"
    assert r["at_risk_gaps"] >= 1
    assert all(g["urgency"] == "critical" for g in r["gaps"])


@pytest.mark.django_db
def test_no_target_means_no_urgency(django_user_model):
    from apps.operations.services import operation_readiness

    op = Operation.objects.create(name="Open-ended")
    r = operation_readiness(op)
    assert r["urgency"] == "none" and r["days_until"] is None and r["at_risk_gaps"] == 0
