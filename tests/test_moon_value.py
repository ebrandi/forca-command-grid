"""4.13 — Moon chunk value & composition estimate.

Acceptance: estimate a moon structure's ore composition + ISK/m³ from its recent mining
ledger (observed history, not a chunk scan), so miners can self-select the richest chunk;
the extraction calendar shows it per structure.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.market.models import MarketPrice
from apps.mining.models import MiningLedgerEntry, MiningObserver
from apps.mining.moon_value import compositions_for_structures, structure_composition
from apps.sde.models import SdeCategory, SdeGroup, SdeType

pytestmark = pytest.mark.django_db
STRUCT = 60000001


@pytest.fixture
def ore_types():
    cat = SdeCategory.objects.create(category_id=25, name="Asteroid")
    grp = SdeGroup.objects.create(group_id=1884, category=cat, name="Moon Materials")
    for tid, price in ((45001, 100), (45002, 10)):  # both 10 m³/unit
        SdeType.objects.create(type_id=tid, group=grp, name=f"Ore {tid}", volume=10.0)
        MarketPrice.objects.create(type_id=tid, profile=MarketPrice.Profile.JITA_SELL,
                                   sell_min=Decimal(price))
    return grp


def _ledger(struct, type_id, qty, days_ago=1):
    obs, _ = MiningObserver.objects.get_or_create(observer_id=struct, defaults={"name": "Refinery"})
    MiningLedgerEntry.objects.create(observer=obs, character_id=1, type_id=type_id,
                                     quantity=qty, day=(timezone.now() - dt.timedelta(days=days_ago)).date())


def test_composition_and_value(ore_types):
    _ledger(STRUCT, 45001, 100)   # 100 × 100 ISK = 10000 ISK ; 100 × 10 = 1000 m³
    _ledger(STRUCT, 45002, 200)   # 200 × 10 ISK  =  2000 ISK ; 200 × 10 = 2000 m³
    c = structure_composition(STRUCT)
    assert c is not None
    assert c["total_value"] == Decimal("12000") and c["total_volume"] == 3000.0
    assert c["isk_per_m3"] == Decimal("4.00")            # 12000 / 3000
    assert c["rows"][0]["type_id"] == 45001              # ranked by value
    assert c["rows"][0]["value_share"] == round(10000 / 12000 * 100, 1)


def test_none_without_ledger(ore_types):
    assert structure_composition(99999999) is None


def test_zero_volume_ore_does_not_inflate_iskm3(ore_types):
    # A priced-but-volumeless ore (partial-SDE anomaly) must not overstate ISK/m³.
    SdeType.objects.create(type_id=45003, group=ore_types, name="Ore 45003", volume=0.0)
    MarketPrice.objects.create(type_id=45003, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min=Decimal(1000))
    _ledger(STRUCT, 45001, 100)   # 10000 ISK, 1000 m³
    _ledger(STRUCT, 45003, 50)    # 50000 ISK, 0 m³
    c = structure_composition(STRUCT)
    assert c["isk_per_m3"] == Decimal("10.00")  # 10000/1000, not (10000+50000)/1000


def test_all_unpriced_returns_none():
    cat = SdeCategory.objects.create(category_id=25, name="Asteroid")
    grp = SdeGroup.objects.create(group_id=1884, category=cat, name="Moon")
    SdeType.objects.create(type_id=46001, group=grp, name="Unpriced", volume=10.0)
    _ledger(STRUCT, 46001, 100)   # no MarketPrice → price_for == 0 → no value estimate
    assert structure_composition(STRUCT) is None


def test_staleness_window(ore_types):
    _ledger(STRUCT, 45001, 100, days_ago=200)            # outside a 90d window
    assert structure_composition(STRUCT, days=90) is None
    assert structure_composition(STRUCT, days=365) is not None


def test_batch_skips_ledgerless(ore_types):
    _ledger(STRUCT, 45001, 100)
    _ledger(60000002, 45002, 50)
    out = compositions_for_structures([STRUCT, 60000002, 60000003])
    assert set(out) == {STRUCT, 60000002}                # the 3rd has no ledger


def test_extraction_calendar_shows_estimate(client, django_user_model, ore_types):
    from apps.corporation.models import MoonExtraction
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac
    _ledger(STRUCT, 45001, 100)
    MoonExtraction.objects.create(structure_id=STRUCT, moon_name="Moon I",
                                  chunk_arrival=timezone.now() + dt.timedelta(days=2))
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    client.force_login(member)
    html = client.get(reverse("corporation:extractions")).content.decode()
    assert "/m³" in html and "Likely ore" in html         # ISK/m³ chip + composition line
