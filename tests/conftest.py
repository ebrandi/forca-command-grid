"""Shared pytest fixtures."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command

from apps.sso.models import EveCharacter


@pytest.fixture(autouse=True)
def _clear_cache():
    """Start every test with an empty cache.

    Several features memoize expensive aggregates (readiness, market signals,
    rankings, Hall of Fame). The locmem cache persists across tests, so without
    this a cached result from one test could leak into another.
    """
    from apps.industry.bom import reset_recipe_cache  # process-local recipe memo
    from apps.market.pricing import reset_price_cache  # process-local price snapshot
    from apps.sde.templatetags import eve as _eve  # process-local SDE name memo

    cache.clear()
    _eve._LOCAL.clear()
    reset_price_cache()
    reset_recipe_cache()
    yield
    cache.clear()
    _eve._LOCAL.clear()
    reset_price_cache()
    reset_recipe_cache()


@pytest.fixture
def sde(db):
    """Load the bundled SDE sample into the test database."""
    call_command("load_sde", sde_version="test")
    return True


@pytest.fixture
def priced_sde(sde):
    """SDE loaded, plus a Jita-sell MarketPrice for every test type mirroring its
    SDE base_price.

    ``price_for`` no longer trusts SDE ``base_price`` (it is not a market value —
    see apps/market/pricing.py), so cost/build-vs-buy/asset-value tests need a real
    market signal. Seeding sell_min == base_price reproduces the numbers those tests
    were written against, now via the supported price path.
    """
    from decimal import Decimal

    from apps.market.models import MarketPrice
    from apps.sde.models import SdeType

    MarketPrice.objects.bulk_create([
        MarketPrice(
            type_id=t.type_id, location=None,
            profile=MarketPrice.Profile.JITA_SELL, sell_min=Decimal(t.base_price),
        )
        for t in SdeType.objects.exclude(base_price__isnull=True)
    ])
    return True


@pytest.fixture
def user(db):
    User = get_user_model()
    u = User.objects.create(username="eve:1001", first_name="Test Pilot")
    u.set_unusable_password()
    u.save()
    return u


@pytest.fixture
def character(db, user):
    return EveCharacter.objects.create(
        character_id=1001, user=user, name="Test Pilot", is_main=True, is_corp_member=True
    )
