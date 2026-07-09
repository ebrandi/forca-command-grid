"""CORP-1 (roadmap 2.14) — reconcile manual stockpiles against live ESI assets.

Acceptance: a stockpile shows ESI on-hand vs target; manual entry is only meaningful for
locations with no ESI coverage.
"""
from __future__ import annotations

import pytest
from django.conf import settings
from django.urls import reverse

from apps.market.models import MarketLocation
from apps.stockpile.models import Asset, AssetLocation, Stockpile, StockpileItem
from apps.stockpile.services import reconcile_stockpile
from core import rbac
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db

SYSTEM = 30000142
RIFTER = 587


def _stockpile(current=10, target=50, *, system_id=SYSTEM) -> Stockpile:
    loc = MarketLocation.objects.create(name="Staging", location_type="system", system_id=system_id)
    sp = Stockpile.objects.create(name="Staging", location=loc, kind=Stockpile.Kind.CORP)
    StockpileItem.objects.create(
        stockpile=sp, type_id=RIFTER, quantity_current=current, quantity_target=target
    )
    return sp


def _corp_asset(qty=60, *, type_id=RIFTER, location_id=60003760, system_id=SYSTEM) -> Asset:
    al = AssetLocation.objects.get_or_create(
        location_id=location_id, defaults={"name": "Jita", "kind": "station", "system_id": system_id}
    )[0]
    return Asset.objects.create(
        owner_type=Asset.Owner.CORPORATION, owner_id=settings.FORCA_HOME_CORP_ID, location=al,
        type_id=type_id, quantity=qty,
    )


def test_covered_location_uses_esi_on_hand():
    sp = _stockpile(current=10, target=50)
    _corp_asset(qty=60)
    recon = reconcile_stockpile(sp)
    assert recon["covered"] is True
    row = recon["rows"][0]
    assert row["esi_on_hand"] == 60
    assert row["effective"] == 60
    assert row["shortfall"] == 0  # 50 target met by 60 on-hand


def test_esi_shortfall_when_below_target():
    sp = _stockpile(current=10, target=50)
    _corp_asset(qty=20)
    row = reconcile_stockpile(sp)["rows"][0]
    assert row["esi_on_hand"] == 20
    assert row["shortfall"] == 30  # 50 - 20 (ESI, not the manual 10)


def test_esi_aggregates_multiple_asset_rows_in_system():
    sp = _stockpile(current=0, target=100)
    _corp_asset(qty=30, location_id=60003760)
    _corp_asset(qty=25, location_id=60003761)  # another station in the same system
    row = reconcile_stockpile(sp)["rows"][0]
    assert row["esi_on_hand"] == 55


def test_uncovered_location_falls_back_to_manual():
    sp = _stockpile(current=15, target=50, system_id=31000005)  # no assets in this system
    recon = reconcile_stockpile(sp)
    assert recon["covered"] is False
    row = recon["rows"][0]
    assert row["esi_on_hand"] is None
    assert row["effective"] == 15  # manual count is the source of truth
    assert row["shortfall"] == 35  # 50 - 15


def test_personal_assets_are_not_corp_coverage():
    sp = _stockpile(current=0, target=50)
    al = AssetLocation.objects.create(location_id=60003760, name="Jita", kind="station", system_id=SYSTEM)
    Asset.objects.create(
        owner_type=Asset.Owner.CHARACTER, owner_id=95000001, location=al, type_id=RIFTER, quantity=40
    )
    recon = reconcile_stockpile(sp)
    assert recon["covered"] is False  # only corp assets count as ESI coverage
    assert recon["rows"][0]["esi_on_hand"] is None


def test_dashboard_renders_reconciliation(client, django_user_model):
    _stockpile()
    _corp_asset(qty=60)
    user, _ = enrol_pilot(django_user_model, 5500, roles=(rbac.ROLE_MEMBER,))
    client.force_login(user)
    resp = client.get(reverse("stockpile:dashboard"))
    assert resp.status_code == 200
    assert b"ESI on-hand" in resp.content
