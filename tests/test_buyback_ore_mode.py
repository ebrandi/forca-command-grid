"""4.9 — Ore/mineral buyback mode with reprocessing yield.

Acceptance: in ore mode a reprocessable type (ore/ice) is valued by its refined mineral
output — (Σ mineral_qty × mineral_sell) / portion_size × reprocessing_pct per unit — while
non-reprocessable lines keep their own Jita sell price. Off unless the config enables it.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.buyback.appraisal import appraise
from apps.market.models import MarketPrice
from apps.sde.models import SdeCategory, SdeGroup, SdeType, SdeTypeMaterial

pytestmark = pytest.mark.django_db
VELDSPAR = 1230
TRITANIUM = 34
SCRAM = 448  # a module (non-Asteroid category) that ALSO has reprocessing yields


@pytest.fixture
def ore_world():
    mat = SdeCategory.objects.create(category_id=4, name="Material")
    ore_cat = SdeCategory.objects.create(category_id=25, name="Asteroid")
    mod_cat = SdeCategory.objects.create(category_id=7, name="Module")
    minerals = SdeGroup.objects.create(group_id=18, category=mat, name="Mineral")
    ores = SdeGroup.objects.create(group_id=450, category=ore_cat, name="Veldspar")
    mods = SdeGroup.objects.create(group_id=52, category=mod_cat, name="Warp Scrambler")
    SdeType.objects.create(type_id=TRITANIUM, group=minerals, name="Tritanium", volume=0.01)
    # Veldspar has its OWN sell price (15/unit) so ore-vs-jita is distinguishable.
    veld = SdeType.objects.create(type_id=VELDSPAR, group=ores, name="Veldspar",
                                  volume=0.1, portion_size=100)
    # A module that has reprocessing yields but is NOT ore — must never use ore valuation.
    scram = SdeType.objects.create(type_id=SCRAM, group=mods, name="Warp Scrambler II",
                                   volume=5.0, portion_size=1)
    MarketPrice.objects.create(type_id=TRITANIUM, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min=Decimal("5.00"))
    MarketPrice.objects.create(type_id=VELDSPAR, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min=Decimal("15.00"))
    MarketPrice.objects.create(type_id=SCRAM, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min=Decimal("1000000.00"))
    # Reprocessing 100 Veldspar → 400 Tritanium; the module reprocesses to 500 Tritanium.
    SdeTypeMaterial.objects.create(type=veld, material_type_id=TRITANIUM, quantity=400)
    SdeTypeMaterial.objects.create(type=scram, material_type_id=TRITANIUM, quantity=500)
    return veld


def test_non_ore_reprocessable_stays_jita_in_ore_mode(ore_world):
    # A module has invTypeMaterials but is category 7 (not Asteroid) → priced at its market
    # value (1M), never its mineral content (500 × 5 = 2500). This is review MED-1.
    r = appraise("Warp Scrambler II 1", sec_band="highsec", rate=Decimal("1.0"),
                 ore_mode=True, reprocessing_pct=Decimal("1.0"))
    assert r.lines[0].basis == "jita"
    assert r.lines[0].line_jita == Decimal("1000000.00")


def test_ore_mode_values_by_reprocessed_minerals(ore_world):
    # per portion = 400 trit × 5 = 2000; /100 portion × 1.0 yield = 20/unit; × 1000 = 20 000.
    r = appraise("Veldspar 1000", sec_band="highsec", rate=Decimal("1.0"),
                 ore_mode=True, reprocessing_pct=Decimal("1.0"))
    line = r.lines[0]
    assert line.basis == "reprocessed"
    assert line.line_jita == Decimal("20000.00")


def test_reprocessing_pct_scales_value(ore_world):
    r = appraise("Veldspar 1000", sec_band="highsec", rate=Decimal("1.0"),
                 ore_mode=True, reprocessing_pct=Decimal("0.5"))
    assert r.lines[0].line_jita == Decimal("10000.00")  # half the yield → half the value


def test_normal_mode_uses_ore_own_price(ore_world):
    # Without ore mode, Veldspar is valued at its own Jita sell (15/unit), not minerals.
    r = appraise("Veldspar 1000", sec_band="highsec", rate=Decimal("1.0"))
    assert r.lines[0].basis == "jita"
    assert r.lines[0].line_jita == Decimal("15000.00")


def test_non_ore_line_always_jita_even_in_ore_mode(ore_world):
    # Tritanium has no reprocessing materials → always its own price, even in ore mode.
    r = appraise("Tritanium 1000", sec_band="highsec", rate=Decimal("1.0"),
                 ore_mode=True, reprocessing_pct=Decimal("1.0"))
    assert r.lines[0].basis == "jita"
    assert r.lines[0].line_jita == Decimal("5000.00")


def test_offer_applies_location_rate_on_top(ore_world):
    # Ore mode value × the location haircut (0.9) → the offer.
    r = appraise("Veldspar 1000", sec_band="highsec", rate=Decimal("0.9"),
                 ore_mode=True, reprocessing_pct=Decimal("1.0"))
    assert r.lines[0].line_offer == Decimal("18000.00")  # 20 000 × 0.9
