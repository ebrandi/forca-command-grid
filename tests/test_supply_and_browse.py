"""Doctrine Ship Finder (browse + filter) and the store Supply Forecast."""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.doctrines.hulls import hull_class_for_group
from apps.doctrines.models import Doctrine, DoctrineFit
from apps.identity.models import RoleAssignment
from apps.killboard.models import Killmail
from apps.market.models import MarketPrice
from apps.sde.models import SdeCategory, SdeGroup, SdeType
from apps.sso.services import ensure_role
from apps.store.models import StoreConfig
from core import rbac


@pytest.fixture
def doctrine_world(db):
    ship_cat = SdeCategory.objects.create(category_id=6, name="Ship")
    mat_cat = SdeCategory.objects.create(category_id=4, name="Material")
    cruiser = SdeGroup.objects.create(group_id=26, category=ship_cat, name="Cruiser")
    frig = SdeGroup.objects.create(group_id=25, category=ship_cat, name="Frigate")
    modgrp = SdeGroup.objects.create(group_id=60, category=mat_cat, name="Module")

    SdeType.objects.create(type_id=16227, group=cruiser, name="Ferox", volume=101000.0)
    SdeType.objects.create(type_id=587, group=frig, name="Rifter", volume=2500.0)
    SdeType.objects.create(type_id=593, group=frig, name="Tristan", volume=2500.0)  # not a doctrine hull
    SdeType.objects.create(type_id=1234, group=modgrp, name="Blaster", volume=5.0)
    for tid, price in [(16227, "39000000"), (587, "5000000"), (593, "6000000"), (1234, "1000000")]:
        MarketPrice.objects.create(
            type_id=tid, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal(price)
        )

    StoreConfig.objects.create(is_active=True, doctrine_markup=Decimal("1.100"))
    doc = Doctrine.objects.create(name="Shield Gang", status=Doctrine.Status.ACTIVE)
    ferox = DoctrineFit.objects.create(
        doctrine=doc, name="Ferox", ship_type_id=16227, role="DPS",
        modules=[{"type_id": 1234, "quantity": 1}],
    )
    rifter = DoctrineFit.objects.create(
        doctrine=doc, name="Rifter", ship_type_id=587, role="Tackle", modules=[],
    )
    return {"doc": doc, "ferox": ferox, "rifter": rifter}


def _member(django_user_model, username="eve:sb1"):
    m = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=m, role=ensure_role(rbac.ROLE_MEMBER))
    return m


@pytest.fixture(autouse=True)
def _stub_everef(monkeypatch):
    """Keep forecast tests hermetic: no real EVE Ref industry-cost calls (→ local path)."""
    monkeypatch.setattr(
        "apps.industry.everef_cost.manufacturing_cost_per_unit", lambda *a, **k: None
    )


# --- Hull classifier --------------------------------------------------------
def test_hull_class_for_group():
    assert hull_class_for_group(26) == "Cruiser"
    assert hull_class_for_group(25) == "Frigate"
    assert hull_class_for_group(547) == "Capital"
    assert hull_class_for_group(902) == "Freighter"
    assert hull_class_for_group(999999) == "Other"


# --- Browse / Ship Finder ---------------------------------------------------
@pytest.mark.django_db
def test_enriched_fits(doctrine_world):
    from apps.doctrines.browse import enriched_fits, filter_options

    rows = enriched_fits(None)
    by_ship = {r["ship_name"]: r for r in rows}
    assert by_ship["Ferox"]["hull_class"] == "Cruiser" and by_ship["Ferox"]["role"] == "DPS"
    assert by_ship["Rifter"]["hull_class"] == "Frigate"
    assert all(r["status"] == "unknown" for r in rows)  # no character → unknown

    opts = filter_options(rows)
    assert "Cruiser" in opts["hull_classes"] and "Frigate" in opts["hull_classes"]
    assert opts["roles"] == ["DPS", "Tackle"]


@pytest.mark.django_db
def test_ship_finder_view_filters(client, django_user_model, doctrine_world):
    client.force_login(_member(django_user_model))
    resp = client.get("/doctrines/ships/?hull=Cruiser")
    assert resp.status_code == 200
    names = {r["ship_name"] for r in resp.context["rows"]}
    assert names == {"Ferox"}  # frigate filtered out

    resp = client.get("/doctrines/ships/?role=Tackle")
    assert {r["ship_name"] for r in resp.context["rows"]} == {"Rifter"}


# --- Shipyard consolidation (Ship Finder is the one place to browse + buy) ---
def _enable_store(audience):
    from django.core.cache import cache

    from apps.store.models import StoreConfig
    from apps.store.services import invalidate_audience_cache

    cache.clear()
    cfg = StoreConfig.objects.filter(is_active=True).first()
    cfg.audience = audience
    cfg.save(update_fields=["audience"])
    invalidate_audience_cache()


@pytest.mark.django_db
def test_shipyard_prices_fits_when_store_enabled(client, django_user_model, doctrine_world):
    from apps.store.models import Audience

    _enable_store(Audience.ALLIANCE)
    client.force_login(_member(django_user_model))
    resp = client.get("/doctrines/ships/")
    assert resp.status_code == 200
    assert resp.context["store_priced"] is True
    rows = {r["ship_name"]: r for r in resp.context["rows"]}
    # Ferox = (39M hull + 1M module) × 1.1 markup = 44M.
    assert rows["Ferox"]["unit_price"] == Decimal("44000000.00")
    html = resp.content.decode()
    assert "Shipyard" in html and "Ship Finder" not in html
    assert "Made to order" in html  # pointer to the store's unique surface


@pytest.mark.django_db
def test_shipyard_has_no_price_when_store_disabled(client, django_user_model, doctrine_world):
    from apps.store.models import Audience

    _enable_store(Audience.DISABLED)
    client.force_login(_member(django_user_model))
    resp = client.get("/doctrines/ships/")
    # The readiness browser stays fully available with the store off — just no price.
    assert resp.status_code == 200
    assert resp.context["store_priced"] is False
    assert all(r["unit_price"] is None for r in resp.context["rows"])
    assert {r["ship_name"] for r in resp.context["rows"]} == {"Ferox", "Rifter"}


@pytest.mark.django_db
def test_storefront_drops_fit_catalog_and_links_to_shipyard(client, django_user_model, doctrine_world):
    from apps.store.models import Audience

    _enable_store(Audience.ALLIANCE)
    client.force_login(_member(django_user_model))
    html = client.get("/store/").content.decode()
    # No duplicate ready-to-fly fit grid here any more…
    assert 'name="fit_id"' not in html
    assert "doctrine_fit" not in html
    # …it points at the Shipyard, and keeps its unique made-to-order form.
    assert "/doctrines/ships/" in html
    assert 'action="/store/order/hull/"' in html or "order/hull" in html


# --- Shipyard is a Corp Store surface: open to the store audience -----------
def _set_home_alliance(settings):
    from apps.corporation.models import EveAlliance, EveCorporation

    settings.FORCA_HOME_CORP_ID = 98000001
    alliance = EveAlliance.objects.create(alliance_id=99000001, name="Home Alliance")
    EveCorporation.objects.create(corporation_id=98000001, name="Home", alliance=alliance)


def _alliance_pilot(django_user_model, cid=8800, alliance_id=99000001):
    from apps.sso.models import EveCharacter

    user = django_user_model.objects.create(username=f"eve:{cid}")
    EveCharacter.objects.create(character_id=cid, user=user, name=f"Ally{cid}", alliance_id=alliance_id)
    return user


@pytest.mark.django_db
def test_alliance_pilot_can_browse_and_order_shipyard(client, django_user_model, settings, doctrine_world):
    # The Shipyard follows the "Ships & doctrines" audience for ACCESS; the Corp Store
    # being enabled is what makes the Order form render. Open both to the alliance.
    from apps.store.models import Audience
    from core.features import set_feature_audiences

    _set_home_alliance(settings)
    set_feature_audiences({"doctrines": "alliance"})   # access for the alliance pilot
    _enable_store(Audience.ALLIANCE)                    # store on → the Order form renders
    client.force_login(_alliance_pilot(django_user_model))
    resp = client.get("/doctrines/ships/")
    assert resp.status_code == 200                       # reached it (not redirect/403)
    html = resp.content.decode()
    assert 'name="fit_id"' in html                       # the Order form renders → can purchase
    # Member-only management links are hidden for the alliance shopper.
    assert "By doctrine" not in html and "Best next to unlock" not in html
    assert f'href="/doctrines/{doctrine_world["doc"].pk}/"' not in html  # no library detail link

    # A member, by contrast, keeps the doctrine-management shortcuts.
    client.force_login(_member(django_user_model, username="eve:mbr-sy"))
    mhtml = client.get("/doctrines/ships/").content.decode()
    assert "By doctrine" in mhtml


@pytest.mark.django_db
def test_shipyard_denies_outsiders_and_corp_only_audience(client, django_user_model, settings, doctrine_world):
    from core.features import set_feature_audiences

    _set_home_alliance(settings)
    # Alliance audience: a pilot from OUTSIDE the alliance is refused (audience 404)…
    set_feature_audiences({"doctrines": "alliance"})
    client.force_login(_alliance_pilot(django_user_model, cid=8801, alliance_id=42))
    assert client.get("/doctrines/ships/").status_code == 404
    # …corp-only: even a real alliance pilot is refused…
    set_feature_audiences({"doctrines": "corp"})
    client.force_login(_alliance_pilot(django_user_model, cid=8802))
    assert client.get("/doctrines/ships/").status_code == 404
    # …but a corp member still gets the browser (corp audience includes members).
    client.force_login(_member(django_user_model, username="eve:mbr-off"))
    assert client.get("/doctrines/ships/").status_code == 200


@pytest.mark.django_db
def test_alliance_pilot_can_copy_shipyard_fit_eft(client, django_user_model, settings, doctrine_world):
    from core.features import set_feature_audiences

    _set_home_alliance(settings)
    set_feature_audiences({"doctrines": "alliance"})
    fit_id = doctrine_world["ferox"].pk
    client.force_login(_alliance_pilot(django_user_model, cid=8804))
    assert client.get(f"/doctrines/fits/{fit_id}/export/").status_code == 200
    client.force_login(_alliance_pilot(django_user_model, cid=8805, alliance_id=42))  # outsider
    assert client.get(f"/doctrines/fits/{fit_id}/export/").status_code == 404


# --- Supply forecast --------------------------------------------------------
def _loss(km_id: int, ship_type_id: int, when):
    return Killmail.objects.create(
        killmail_id=km_id, killmail_hash=f"h{km_id}", killmail_time=when,
        solar_system_id=30000142, victim_ship_type_id=ship_type_id,
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
    )


@pytest.mark.django_db
def test_recent_losses(doctrine_world):
    from apps.store.forecast import recent_losses

    now = timezone.now()
    for i in range(4):
        _loss(100 + i, 16227, now - timezone.timedelta(days=i))
    _loss(200, 587, now)
    _loss(300, 16227, now - timezone.timedelta(days=90))  # outside a 28d window
    losses = recent_losses(28)
    assert losses[16227] == 4 and losses[587] == 1


@pytest.mark.django_db
def test_supply_forecast_math(doctrine_world):
    from datetime import datetime, timedelta
    from datetime import time as dt_time

    from apps.store.forecast import supply_forecast

    # P2: demand for doctrine hulls is the composed per-fit rate over COMPLETE
    # ISO weeks (the current partial week is excluded) — seed one loss into each
    # of the last four complete weeks so the mean is exactly 1/wk regardless of
    # which weekday the suite runs on.
    today = timezone.localdate()
    week_start = timezone.make_aware(
        datetime.combine(today - timedelta(days=today.weekday()), dt_time.min)
    )
    for i in range(4):
        _loss(100 + i, 16227, week_start - timedelta(weeks=i + 1) + timedelta(hours=12))
    # 60-day window so the raw `losses` count always sees all four seeds (the
    # oldest sits up to ~34 days back depending on the weekday the suite runs).
    data = supply_forecast(window_days=60, staging_system_id=0, limit=10)
    rows = {r.ship_name: r for r in data["rows"]}
    assert "Ferox" in rows
    fx = rows["Ferox"]
    # Fit = 39M hull + 1M module = 40M Jita; sell = ×1.10 = 44M; margin = 4M.
    assert fx.jita_unit == Decimal("40000000.00")
    assert fx.sell_unit == Decimal("44000000.00")
    assert fx.method == "import"                 # no blueprint → can't build
    assert fx.freight_unit == Decimal("0")       # no staging set
    assert fx.margin_unit == Decimal("4000000.00")
    assert fx.losses == 4 and fx.per_week == 1.0
    # Honest rounding, no floors: round(1) = 1/wk, round(1 × 4.345) = 4/month.
    assert fx.forecast_week == 1 and fx.forecast_month == 4
    assert fx.profit_month == Decimal("16000000.00")


@pytest.mark.django_db
def test_supply_forecast_uses_everef_build_cost(doctrine_world, monkeypatch):
    """When EVE Ref prices the build cheaper than importing, build wins and is sourced."""
    from apps.store.forecast import supply_forecast

    monkeypatch.setattr(
        "apps.industry.everef_cost.manufacturing_cost_per_unit",
        lambda tid, **k: Decimal("20000000") if tid == 16227 else None,
    )
    _loss(101, 16227, timezone.now())
    data = supply_forecast(window_days=28, staging_system_id=0, limit=10)
    fx = {r.ship_name: r for r in data["rows"]}["Ferox"]
    # build = 20M hull + 1M modules + 0 freight = 21M, beats the 40M import.
    assert fx.method == "build" and fx.build_source == "everef"
    assert fx.supply_cost == Decimal("21000000.00")
    assert fx.margin_unit == Decimal("23000000.00")  # 44M sell − 21M supply


@pytest.mark.django_db
def test_supply_forecast_keeps_build_priced_capitals_without_jita_reference(doctrine_world, monkeypatch):
    """A lost capital with NO market reference at all (no Jita sell, no CCP adjusted —
    exactly the hulls that never trade in Jita) still forecasts off its build-cost
    store price; the import lane simply doesn't compete."""
    from apps.store.forecast import supply_forecast

    ship_cat = SdeCategory.objects.get(category_id=6)
    dread = SdeGroup.objects.create(group_id=485, category=ship_cat, name="Dreadnought")
    SdeType.objects.create(type_id=19720, group=dread, name="Naglfar", volume=18500000.0)
    monkeypatch.setattr(
        "apps.industry.everef_cost.manufacturing_cost_per_unit",
        lambda tid, **k: Decimal("2000000000") if tid == 19720 else None,
    )
    _loss(401, 19720, timezone.now())
    data = supply_forecast(window_days=28, staging_system_id=0, limit=10)
    nag = {r.ship_name: r for r in data["rows"]}["Naglfar"]
    assert nag.jita_unit == Decimal("0.00")             # no reference price exists
    assert nag.sell_unit == Decimal("2200000000.00")    # build 2B × 1.10 capital markup
    assert nag.method == "build" and nag.import_cost is None
    assert nag.supply_cost == Decimal("2000000000.00")
    assert nag.margin_unit == Decimal("200000000.00")


@pytest.mark.django_db
def test_supply_forecast_includes_non_doctrine_hulls(doctrine_world):
    """A lost hull with no doctrine fit is valued as a bare hull to import-and-sell."""
    from apps.store.forecast import supply_forecast

    now = timezone.now()
    _loss(400, 593, now)            # Tristan — not in any doctrine
    data = supply_forecast(window_days=28, staging_system_id=0, limit=10)
    rows = {r.ship_name: r for r in data["rows"]}
    assert "Tristan" in rows
    tr = rows["Tristan"]
    assert tr.is_doctrine is False and tr.fit_name == "hull only"
    assert tr.jita_unit == Decimal("6000000.00")          # bare hull Jita
    assert tr.sell_unit == Decimal("6600000.00")          # × 1.10 hull markup
    assert tr.margin_unit == Decimal("600000.00")


@pytest.mark.django_db
def test_supply_forecast_excludes_pods(doctrine_world):
    """Capsule losses never become a supply suggestion."""
    from apps.sde.models import SdeGroup, SdeType
    from apps.store.forecast import supply_forecast

    cap_grp = SdeGroup.objects.create(group_id=29, category_id=6, name="Capsule")
    SdeType.objects.create(type_id=670, group=cap_grp, name="Capsule", volume=1.0)
    now = timezone.now()
    for i in range(20):
        _loss(500 + i, 670, now)    # a pile of pod losses
    data = supply_forecast(window_days=28, staging_system_id=0, limit=10)
    assert all(r.ship_type_id != 670 for r in data["rows"])


@pytest.mark.django_db
def test_supply_forecast_attaches_price_trend(doctrine_world):
    import datetime

    from apps.market.models import MarketHistory
    from apps.store.forecast import supply_forecast

    base = timezone.now().date()
    for i in range(30):  # rising Jita price 100 → 129 over the window
        MarketHistory.objects.create(
            type_id=16227, region_id=10000002,
            date=base - datetime.timedelta(days=29 - i), average=Decimal(100 + i),
        )
    _loss(101, 16227, timezone.now())
    data = supply_forecast(window_days=28, staging_system_id=0, limit=10)
    fx = {r.ship_name: r for r in data["rows"]}["Ferox"]
    assert fx.trend_pct is not None and fx.trend_pct > 0


@pytest.mark.django_db
def test_supply_forecast_view(client, django_user_model, doctrine_world):
    now = timezone.now()
    _loss(101, 16227, now)
    client.force_login(_member(django_user_model, "eve:sb2"))
    resp = client.get("/store/supply/forecast/?window=30")
    assert resp.status_code == 200
    assert "Ferox" in resp.content.decode() and "Supply Forecast" in resp.content.decode()
