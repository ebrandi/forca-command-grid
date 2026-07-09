"""IND-2 (roadmap 3.4) — material consumption on delivery.

When enabled, delivering a build decrements its input materials from corp stock (clamped so
stock never goes negative, converting delivered units → blueprint runs) and records the burn
on the Delivery. Ships OFF.
"""
from __future__ import annotations

from collections import namedtuple

import pytest

from apps.erp.models import BuildJob, Delivery
from apps.erp.services import deliver
from apps.industry.models import IndustryEconomyConfig
from apps.stockpile.models import Stockpile, StockpileItem

pytestmark = pytest.mark.django_db

TRIT, PYE, RIFTER = 34, 35, 587
_Recipe = namedtuple("_Recipe", ["output_quantity"])


def _mock_bom(monkeypatch, per_run, *, output_quantity=1):
    """Mock the BOM: ``per_run`` is materials for ONE run; direct_materials scales by runs."""
    monkeypatch.setattr("apps.industry.bom.buildable_recipe",
                        lambda pid: _Recipe(output_quantity))
    monkeypatch.setattr("apps.industry.bom.direct_materials",
                        lambda pid, runs=1, me=0: {t: q * runs for t, q in per_run.items()})


def _no_recipe(monkeypatch):
    monkeypatch.setattr("apps.industry.bom.buildable_recipe", lambda pid: None)


def _corp_sp():
    return Stockpile.objects.create(name="Home", kind=Stockpile.Kind.CORP)


def _stock(sp, type_id, qty):
    return StockpileItem.objects.create(stockpile=sp, type_id=type_id, quantity_current=qty)


def _job(sp, output=RIFTER, qty=1):
    return BuildJob.objects.create(
        output_type_id=output, quantity=qty, status=BuildJob.Status.BUILT, deliver_to=sp
    )


def _enable(on=True):
    cfg = IndustryEconomyConfig.active()
    cfg.consume_materials_on_delivery = on
    cfg.save()


def _builder(django_user_model):
    return django_user_model.objects.create(username="builder")


def test_consumption_off_by_default(monkeypatch, django_user_model):
    _mock_bom(monkeypatch, {TRIT: 100})
    sp = _corp_sp()
    _stock(sp, TRIT, 500)
    deliver(_job(sp), _builder(django_user_model))
    assert StockpileItem.objects.get(stockpile=sp, type_id=TRIT).quantity_current == 500  # untouched
    assert Delivery.objects.get().consumed == {}


def test_consumption_decrements_inputs(monkeypatch, django_user_model):
    _mock_bom(monkeypatch, {TRIT: 100, PYE: 50})
    sp = _corp_sp()
    _stock(sp, TRIT, 500)
    _stock(sp, PYE, 80)
    _enable()
    deliver(_job(sp), _builder(django_user_model))
    assert StockpileItem.objects.get(stockpile=sp, type_id=TRIT).quantity_current == 400
    assert StockpileItem.objects.get(stockpile=sp, type_id=PYE).quantity_current == 30
    assert Delivery.objects.get().consumed == {str(TRIT): 100, str(PYE): 50}  # JSON keys are strings


def test_batch_recipe_converts_units_to_runs(monkeypatch, django_user_model):
    # A recipe yielding 100 units/run: delivering 100 units is 1 run, so burn one run's BOM
    # (50), NOT 100× (5000). Guards the units-as-runs over-burn.
    _mock_bom(monkeypatch, {TRIT: 50}, output_quantity=100)
    sp = _corp_sp()
    _stock(sp, TRIT, 1000)
    _enable()
    deliver(_job(sp, qty=100), _builder(django_user_model))
    assert StockpileItem.objects.get(stockpile=sp, type_id=TRIT).quantity_current == 950
    assert Delivery.objects.get().consumed == {str(TRIT): 50}


def test_consumption_clamps_at_zero(monkeypatch, django_user_model):
    _mock_bom(monkeypatch, {TRIT: 100})
    sp = _corp_sp()
    _stock(sp, TRIT, 30)  # short
    _enable()
    deliver(_job(sp), _builder(django_user_model))
    assert StockpileItem.objects.get(stockpile=sp, type_id=TRIT).quantity_current == 0  # never negative
    assert Delivery.objects.get().consumed == {str(TRIT): 30}  # only what was on hand


def test_output_still_added_when_consuming(monkeypatch, django_user_model):
    _mock_bom(monkeypatch, {TRIT: 10})
    sp = _corp_sp()
    _stock(sp, TRIT, 100)
    _enable()
    deliver(_job(sp, qty=3), _builder(django_user_model))
    assert StockpileItem.objects.get(stockpile=sp, type_id=RIFTER).quantity_current == 3  # output added


def test_unbuildable_is_a_noop(monkeypatch, django_user_model):
    _no_recipe(monkeypatch)  # no blueprint / not buildable
    sp = _corp_sp()
    _stock(sp, TRIT, 100)
    _enable()
    deliver(_job(sp), _builder(django_user_model))
    assert StockpileItem.objects.get(stockpile=sp, type_id=TRIT).quantity_current == 100
    assert Delivery.objects.get().consumed == {}
