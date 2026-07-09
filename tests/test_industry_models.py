"""Phase B: IndustryEconomyConfig singleton, assumption inheritance, plan fields."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.industry import services
from apps.industry.models import IndustryEconomyConfig, IndustryProject, IndustryProjectItem

pytestmark = pytest.mark.django_db


def test_config_active_is_singleton():
    a = IndustryEconomyConfig.active()
    b = IndustryEconomyConfig.active()
    assert a.pk == b.pk
    assert IndustryEconomyConfig.objects.count() == 1
    assert a.default_sales_tax == Decimal("0.0450")
    assert a.erp_redirects is True


def test_effective_rates_inherit_and_override():
    cfg = IndustryEconomyConfig.active()
    cfg.default_sales_tax = Decimal("0.0500")
    cfg.save()
    project = IndustryProject.objects.create(name="P", broker_fee=Decimal("0.0100"))
    rates = services.effective_rates(project)
    assert rates["sales_tax"] == Decimal("0.0500")   # inherited from config
    assert rates["broker_fee"] == Decimal("0.0100")  # per-plan override wins
    # No project -> pure config defaults.
    assert services.effective_rates(None)["broker_fee"] == cfg.default_broker_fee


def test_plan_defaults_are_safe():
    p = IndustryProject.objects.create(name="Fresh")
    assert p.is_archived is False and p.archived_at is None
    assert p.visibility == IndustryProject.Visibility.CORP
    assert p.source == IndustryProject.Source.MANUAL
    item = IndustryProjectItem.objects.create(project=p, type_id=587, quantity=1)
    assert item.blueprint_source == IndustryProjectItem.BlueprintSource.UNKNOWN
    assert item.invent_science_1 == 0 and item.invent_decryptor_type_id is None
