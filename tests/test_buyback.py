"""Buyback & appraisal: parsing, location haircuts, audience, and the flow."""
from __future__ import annotations

from decimal import Decimal

import pytest

from apps.buyback.appraisal import appraise, parse_lines
from apps.buyback.models import Audience, BuybackConfig, BuybackOffer, SecBand
from apps.buyback.services import active_config, can_access, invalidate_audience_cache
from apps.identity.models import RoleAssignment
from apps.market.models import MarketPrice
from apps.sde.models import SdeCategory, SdeGroup, SdeType
from apps.sso.services import ensure_role
from core import rbac

HOME_CORP = 98000001
HOME_ALLIANCE = 99000001


@pytest.fixture
def priced_items(db):
    cat = SdeCategory.objects.create(category_id=4, name="Material")
    grp = SdeGroup.objects.create(group_id=18, category=cat, name="Mineral")
    trit = SdeType.objects.create(type_id=34, group=grp, name="Tritanium", volume=0.01)
    pyer = SdeType.objects.create(type_id=35, group=grp, name="Pyerite", volume=0.01)
    for t, price in [(trit, "5.00"), (pyer, "10.00")]:
        MarketPrice.objects.create(
            type_id=t.type_id, profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal(price)
        )
    return {"trit": trit, "pyer": pyer}


# --- Parsing ----------------------------------------------------------------
def test_parse_tab_space_and_x_quantity():
    assert parse_lines("Tritanium\t1000") == [("Tritanium", 1000)]
    assert parse_lines("Ferox 3") == [("Ferox", 3)]
    assert parse_lines("Tritanium x500") == [("Tritanium", 500)]


def test_parse_merges_duplicates_and_defaults_qty_one():
    assert parse_lines("Tritanium 100\nTritanium 50") == [("Tritanium", 150)]
    assert parse_lines("Warp Scrambler II") == [("Warp Scrambler II", 1)]


def test_parse_handles_thousands_separators():
    assert parse_lines("Tritanium\t1,000,000") == [("Tritanium", 1000000)]


# --- Pricing: the location haircuts -----------------------------------------
@pytest.mark.django_db
def test_highsec_pays_90_percent(priced_items):
    # 1000 Tritanium @ 5 ISK = 5000 Jita; highsec 0.90 = 4500.
    a = appraise("Tritanium 1000", sec_band=SecBand.HIGHSEC, rate=Decimal("0.90"))
    assert a.jita_total == Decimal("5000.00")
    assert a.offer_total == Decimal("4500.00")
    assert not a.unknown


@pytest.mark.django_db
def test_lowsec_pays_85_and_nullsec_80(priced_items):
    low = appraise("Tritanium 1000", sec_band=SecBand.LOWSEC, rate=Decimal("0.85"))
    null = appraise("Tritanium 1000", sec_band=SecBand.NULLSEC, rate=Decimal("0.80"))
    assert low.offer_total == Decimal("4250.00")
    assert null.offer_total == Decimal("4000.00")


@pytest.mark.django_db
def test_multi_item_totals_and_volume(priced_items):
    a = appraise("Tritanium 1000\nPyerite 100", sec_band=SecBand.HIGHSEC, rate=Decimal("0.90"))
    # (1000×5 + 100×10) = 6000 Jita; ×0.90 = 5400.
    assert a.jita_total == Decimal("6000.00")
    assert a.offer_total == Decimal("5400.00")
    assert a.item_count == 1100
    assert round(a.volume_m3, 2) == 11.0  # 1100 × 0.01


@pytest.mark.django_db
def test_appraisal_reports_oldest_price_timestamp(priced_items):
    from datetime import timedelta

    from django.utils import timezone

    # Make Pyerite's price older than Tritanium's; the appraisal must surface the
    # oldest (most conservative) timestamp across the basket.
    old = timezone.now() - timedelta(hours=6)
    MarketPrice.objects.filter(type_id=35).update(as_of=old)
    a = appraise("Tritanium 10\nPyerite 10", sec_band=SecBand.HIGHSEC, rate=Decimal("0.90"))
    assert a.priced_as_of is not None
    assert abs((a.priced_as_of - old).total_seconds()) < 5


@pytest.mark.django_db
def test_appraisal_timestamp_none_when_only_base_price(db):
    # An item with an SDE base price but no market price → no timestamp to show.
    cat = SdeCategory.objects.create(category_id=9, name="Cat")
    grp = SdeGroup.objects.create(group_id=99, category=cat, name="Grp")
    SdeType.objects.create(type_id=999, group=grp, name="Untraded", volume=1.0, base_price=Decimal("100"))
    a = appraise("Untraded 1", sec_band=SecBand.HIGHSEC, rate=Decimal("0.90"))
    assert a.priced_as_of is None
    assert a.offer_total == Decimal("90.00")  # base price still valued


@pytest.mark.django_db
def test_unknown_items_reported_not_priced(priced_items):
    a = appraise("Tritanium 10\nNotARealItem 5", sec_band=SecBand.HIGHSEC, rate=Decimal("0.90"))
    assert a.unknown == ["NotARealItem"]
    assert len(a.lines) == 1


@pytest.mark.django_db
def test_config_rate_for_band_matches_requirement():
    cfg = active_config()
    assert cfg.rate_for(SecBand.HIGHSEC) == Decimal("0.900")
    assert cfg.rate_for(SecBand.LOWSEC) == Decimal("0.850")
    assert cfg.rate_for(SecBand.NULLSEC) == Decimal("0.800")


# --- Audience ---------------------------------------------------------------
def _set_audience(value):
    cfg = active_config()
    cfg.audience = value
    cfg.save(update_fields=["audience"])
    invalidate_audience_cache()


@pytest.mark.django_db
def test_default_audience_is_alliance():
    assert BuybackConfig._meta.get_field("audience").default == Audience.ALLIANCE


@pytest.mark.django_db
def test_corp_audience_blocks_outsider(django_user_model):
    _set_audience(Audience.CORP)
    from django.contrib.auth.models import AnonymousUser
    member = django_user_model.objects.create(username="eve:8001")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    outsider = django_user_model.objects.create(username="eve:8002")
    assert can_access(AnonymousUser()) is False
    assert can_access(member) is True
    assert can_access(outsider) is False


@pytest.mark.django_db
def test_public_audience_allows_anonymous():
    _set_audience(Audience.PUBLIC)
    from django.contrib.auth.models import AnonymousUser
    assert can_access(AnonymousUser()) is True
    _set_audience(Audience.ALLIANCE)  # restore


# --- View flow --------------------------------------------------------------
@pytest.mark.django_db
def test_anonymous_blocked_when_members_only(client):
    _set_audience(Audience.ALLIANCE)
    assert client.get("/buyback/").status_code == 403


@pytest.mark.django_db
def test_no_competitor_name_in_ui(client):
    _set_audience(Audience.PUBLIC)
    html = client.get("/buyback/").content.decode().lower()
    assert "jitarun" not in html and "jita run" not in html
    _set_audience(Audience.ALLIANCE)


@pytest.mark.django_db
def test_member_appraise_post_and_buy_flow(client, django_user_model, priced_items):
    _set_audience(Audience.CORP)
    from apps.sso.models import EveCharacter

    seller = django_user_model.objects.create(username="eve:8100")
    RoleAssignment.objects.create(user=seller, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=8100, user=seller, name="Seller", is_main=True, is_corp_member=True)
    client.force_login(seller)

    # Appraise stores a pending lot in the session.
    resp = client.post("/buyback/", {"items": "Tritanium 1000", "sec_band": "highsec"})
    assert resp.status_code == 200
    # Post it to the board.
    resp = client.post("/buyback/submit/")
    assert resp.status_code == 302
    offer = BuybackOffer.objects.get()
    assert offer.status == BuybackOffer.Status.OPEN
    assert offer.offer_total == Decimal("4500.00")
    assert offer.seller_id == seller.id

    # A corpmate buys the lot.
    buyer = django_user_model.objects.create(username="eve:8101")
    RoleAssignment.objects.create(user=buyer, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=8101, user=buyer, name="Buyer", is_main=True, is_corp_member=True)
    client.force_login(buyer)
    client.post(f"/buyback/offers/{offer.pk}/buy/")
    offer.refresh_from_db()
    assert offer.status == BuybackOffer.Status.PURCHASED
    assert offer.buyer_id == buyer.id

    # Settle.
    client.post(f"/buyback/offers/{offer.pk}/action/", {"action": "paid"})
    offer.refresh_from_db()
    assert offer.status == BuybackOffer.Status.PAID


@pytest.mark.django_db
def test_cannot_buy_own_lot(client, django_user_model, priced_items):
    _set_audience(Audience.CORP)
    from apps.sso.models import EveCharacter

    seller = django_user_model.objects.create(username="eve:8200")
    RoleAssignment.objects.create(user=seller, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=8200, user=seller, name="S", is_main=True, is_corp_member=True)
    client.force_login(seller)
    client.post("/buyback/", {"items": "Tritanium 10", "sec_band": "highsec"})
    client.post("/buyback/submit/")
    offer = BuybackOffer.objects.get()
    client.post(f"/buyback/offers/{offer.pk}/buy/")
    offer.refresh_from_db()
    assert offer.status == BuybackOffer.Status.OPEN  # unchanged — can't buy own lot
