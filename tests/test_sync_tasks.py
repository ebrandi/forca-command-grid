"""Scheduled sync tasks: market history (public) and corp assets (Director token)."""
from __future__ import annotations

import pytest
import responses

from apps.market.models import MarketHistory, MarketLocation, MarketPrice
from apps.market.tasks import sync_market_history
from apps.stockpile.tasks import sync_corp_assets

ESI = "https://esi.evetech.net"


@responses.activate
@pytest.mark.django_db
def test_sync_market_history_task(sde):
    loc = MarketLocation.objects.create(name="Jita", location_type="system", region_id=10000002)
    MarketPrice.objects.create(type_id=34, location=loc, sell_min=5, buy_max=4,
                               profile=MarketPrice.Profile.JITA_SELL)
    responses.add(
        responses.GET, f"{ESI}/markets/10000002/history/",
        json=[{"date": "2026-05-01", "average": 5.0, "highest": 5.2, "lowest": 4.8,
               "volume": 1000, "order_count": 10}],
        status=200,
    )
    stored = sync_market_history()
    assert stored == 1
    assert MarketHistory.objects.filter(type_id=34).exists()


@pytest.mark.django_db
def test_sync_market_history_no_tracked_types_is_noop(sde):
    assert sync_market_history() == 0


@pytest.mark.django_db
def test_sync_corp_assets_task_degrades_without_scope(sde, settings):
    # No Director token with the asset scope -> task returns the status, no crash.
    settings.FORCA_HOME_CORP_ID = 0
    assert sync_corp_assets() == "no_corp"


@responses.activate
@pytest.mark.django_db
def test_sync_personal_assets_task_syncs_opted_in_pilots(sde, django_user_model):
    from django.utils import timezone

    from apps.sso.models import AuthToken, EveCharacter
    from apps.stockpile.assets import CHAR_ASSETS_SCOPE
    from apps.stockpile.models import Asset
    from apps.stockpile.tasks import sync_personal_assets

    user = django_user_model.objects.create(username="pilot")
    # Pilot A granted the asset scope; pilot B did not (gets skipped, no crash).
    a = EveCharacter.objects.create(character_id=61, user=user, name="A", is_main=True)
    EveCharacter.objects.create(character_id=62, user=user, name="B")
    tok = AuthToken(character=a, scopes=[CHAR_ASSETS_SCOPE])
    tok.refresh_token = "r"
    tok.access_token = "x"
    tok.access_expires_at = timezone.now() + timezone.timedelta(hours=1)
    tok.save()
    AuthToken.objects.create(character_id=62)  # B has a token but no asset scope

    responses.add(
        responses.GET, f"{ESI}/characters/61/assets/",
        json=[{"item_id": 1, "type_id": 34, "quantity": 9,
               "location_id": 30000142, "location_type": "solar_system"}],
        headers={"X-Pages": "1"}, status=200,
    )
    synced = sync_personal_assets()
    assert synced == 1  # only the opted-in pilot
    assert Asset.objects.filter(owner_type=Asset.Owner.CHARACTER, owner_id=61).exists()
    assert not Asset.objects.filter(owner_id=62).exists()
