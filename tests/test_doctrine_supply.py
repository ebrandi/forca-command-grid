"""Doctrine supply chain: target vs on-hand, shortfall, buy-vs-build, task."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.conf import settings

from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.doctrines.supply import supply_plan
from apps.identity.models import RoleAssignment
from apps.market.models import MarketPrice
from apps.sso.services import ensure_role
from apps.stockpile.models import Asset
from apps.tasks.models import Task
from core import rbac

RIFTER = 587
AUTOCANNON = 484


def _doctrine():
    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    d = Doctrine.objects.create(name="Rifter Fleet", category=cat, priority=90)
    DoctrineFit.objects.create(
        doctrine=d, name="Rifter", ship_type_id=RIFTER,
        modules=[{"type_id": AUTOCANNON, "quantity": 2, "name": "200mm AutoCannon I"}],
    )
    return d


@pytest.mark.django_db
def test_supply_plan_nets_on_hand_against_target(sde):
    MarketPrice.objects.create(type_id=RIFTER, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal("500000"))
    MarketPrice.objects.create(type_id=AUTOCANNON, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal("100000"))
    d = _doctrine()
    # Corp already holds 3 hulls.
    Asset.objects.create(owner_type=Asset.Owner.CORPORATION,
                         owner_id=settings.FORCA_HOME_CORP_ID, type_id=RIFTER, quantity=3)

    plan = supply_plan(d, sets=10)
    by_type = {line["type_id"]: line for line in plan["lines"]}
    assert by_type[RIFTER]["target"] == 10
    assert by_type[RIFTER]["have"] == 3
    assert by_type[RIFTER]["need"] == 7          # 10 target − 3 on hand
    assert by_type[AUTOCANNON]["target"] == 20   # 2 per set × 10
    assert by_type[AUTOCANNON]["need"] == 20
    assert plan["total_buy"] > 0
    assert plan["ready"] is False


@pytest.mark.django_db
def test_supply_plan_ready_when_stocked(sde):
    d = _doctrine()
    Asset.objects.create(owner_type=Asset.Owner.CORPORATION,
                         owner_id=settings.FORCA_HOME_CORP_ID, type_id=RIFTER, quantity=100)
    Asset.objects.create(owner_type=Asset.Owner.CORPORATION,
                         owner_id=settings.FORCA_HOME_CORP_ID, type_id=AUTOCANNON, quantity=100)
    plan = supply_plan(d, sets=10)
    assert plan["ready"] is True
    assert plan["short"] == []


@pytest.mark.django_db
def test_supply_task_is_officer_only_and_idempotent(client, django_user_model, sde):
    officer = django_user_model.objects.create(username="fc")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    payload = {"type_id": str(RIFTER), "action": "buy", "title": "Buy 7 Rifters"}
    client.post("/doctrines/supply/task/", payload)
    client.post("/doctrines/supply/task/", payload)
    assert Task.objects.filter(related_type="supply", related_id=str(RIFTER)).count() == 1

    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    assert client.post("/doctrines/supply/task/", payload).status_code == 403
