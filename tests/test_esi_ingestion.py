"""ESI ingestion: public market history + corp assets (director token).

Corp-asset reading requires a Director token with the corp-assets scope; the
ingestion must degrade gracefully (a status, not a crash) when none exists.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import responses

from apps.corporation.models import EveCorporation
from apps.market.models import MarketHistory
from apps.market.services import ingest_market_history, price_trend
from apps.sso.models import AuthToken, EveCharacter
from apps.stockpile.assets import CORP_ASSETS_SCOPE, import_corporation_assets
from apps.stockpile.models import Asset, AssetLocation

ESI = "https://esi.evetech.net"


def _corp_char(corp_id, char_id=7):
    EveCorporation.objects.get_or_create(corporation_id=corp_id)
    return EveCharacter.objects.create(
        character_id=char_id, name="CEO", corporation_id=corp_id, is_corp_member=True
    )


# --- Market history (public, no token) ---------------------------------------
@responses.activate
@pytest.mark.django_db
def test_ingest_market_history_and_trend(sde):
    responses.add(
        responses.GET,
        f"{ESI}/markets/10000002/history/",
        json=[
            {"date": "2026-05-01", "average": 5.0, "highest": 5.2, "lowest": 4.8, "volume": 1000, "order_count": 10},
            {"date": "2026-05-02", "average": 6.0, "highest": 6.1, "lowest": 5.9, "volume": 2000, "order_count": 20},
        ],
        status=200,
    )
    stored, skipped = ingest_market_history(10000002, [34], days=90)
    assert stored == 2 and skipped == 0
    assert MarketHistory.objects.filter(type_id=34, region_id=10000002).count() == 2

    trend = price_trend(34, 10000002, days=30)
    assert trend["latest"] == Decimal("6.00")
    assert round(trend["change_pct"]) == 20  # 5 -> 6 = +20%
    assert trend["avg_volume"] == 1500


@responses.activate
@pytest.mark.django_db
def test_ingest_market_history_skips_bad_types(sde):
    """One non-tradable type must not kill the run (it starved history for a week)."""
    responses.add(
        responses.GET,
        f"{ESI}/markets/10000002/history/",
        json={"error": "Type not tradable on market!"},
        status=400,
    )
    responses.add(
        responses.GET,
        f"{ESI}/markets/10000002/history/",
        json=[{"date": "2026-05-01", "average": 5.0, "highest": 5.2, "lowest": 4.8,
               "volume": 1000, "order_count": 10}],
        status=200,
    )
    stored, skipped = ingest_market_history(10000002, [20, 34], days=90)
    assert stored == 1 and skipped == 1
    assert MarketHistory.objects.filter(type_id=34).count() == 1


@pytest.mark.django_db
def test_tracked_history_type_ids_prefers_liquid_jita_types(sde):
    from apps.market.models import MarketPrice
    from apps.market.services import tracked_history_type_ids

    MarketPrice.objects.create(type_id=34, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min=Decimal("5"), volume=1000)
    MarketPrice.objects.create(type_id=587, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min=Decimal("400000"), volume=50000)
    # Adjusted-only rows (the ~19k-type reference table) must never be selected —
    # they include types ESI refuses history for.
    MarketPrice.objects.create(type_id=20, profile=MarketPrice.Profile.ADJUSTED,
                               sell_min=None)
    # A Jita row without a real sell price is not a tracked market type either.
    MarketPrice.objects.create(type_id=25, profile=MarketPrice.Profile.JITA_SELL,
                               sell_min=Decimal("0"))

    ids = tracked_history_type_ids(limit=10)
    assert ids == [587, 34]  # liquid first, adjusted-only and priceless excluded


@pytest.mark.django_db
def test_ensure_history_fresh_guard(sde, monkeypatch):
    from apps.admin_audit.health import record_sync
    from apps.market import tasks as market_tasks

    calls = []
    monkeypatch.setattr(market_tasks, "sync_market_history", lambda: calls.append(1) or 7)

    # No stamp at all → catch-up runs.
    assert market_tasks.ensure_history_fresh() == 7
    assert len(calls) == 1

    # Fresh stamp → no-op.
    record_sync("market_history", rows=1)
    assert market_tasks.ensure_history_fresh() == 0
    assert len(calls) == 1


# --- Corp assets: graceful degradation when no scoped token ------------------
@pytest.mark.django_db
def test_corp_assets_no_scope_degrades_gracefully(sde, settings):
    settings.FORCA_HOME_CORP_ID = 98000001
    _corp_char(98000001)
    # No token with the asset scope exists.
    result = import_corporation_assets(98000001)
    assert result["status"] == "no_scope"
    assert not Asset.objects.filter(owner_type=Asset.Owner.CORPORATION).exists()


def _director(corp_id, char_id=7):
    from django.utils import timezone
    char = _corp_char(corp_id, char_id)
    token = AuthToken(character=char, scopes=[CORP_ASSETS_SCOPE])
    token.refresh_token = "r"
    token.access_token = "live-access"
    token.access_expires_at = timezone.now() + timezone.timedelta(hours=1)
    token.save()
    return char


# --- Corp assets: happy path, grouped by location with rollup ----------------
@responses.activate
@pytest.mark.django_db
def test_corp_assets_imported_grouped_by_location(sde, settings):
    settings.FORCA_HOME_CORP_ID = 98000001
    _director(98000001)
    # Jita (30000142) holds Trit directly + a container (item 100) holding Trit.
    responses.add(
        responses.GET, f"{ESI}/corporations/98000001/assets/",
        json=[
            {"item_id": 1, "type_id": 34, "quantity": 1000, "location_id": 30000142, "location_type": "solar_system"},
            {"item_id": 100, "type_id": 587, "quantity": 1, "location_id": 30000142, "location_type": "solar_system"},
            {"item_id": 101, "type_id": 34, "quantity": 500, "location_id": 100, "location_type": "item"},
            {"item_id": 2, "type_id": 35, "quantity": 200, "location_id": 30002053, "location_type": "solar_system"},
        ],
        headers={"X-Pages": "1"}, status=200,
    )
    result = import_corporation_assets(98000001)
    assert result["status"] == "ok"
    assert result["locations"] == 2  # Jita + Otitoh

    jita = AssetLocation.objects.get(location_id=30000142)
    trit_jita = Asset.objects.get(owner_type=Asset.Owner.CORPORATION, location=jita, type_id=34)
    # 1000 direct + 500 nested in the container, both rolled up to Jita.
    assert trit_jita.quantity == 1500
    assert jita.kind == AssetLocation.Kind.SOLAR_SYSTEM


@responses.activate
@pytest.mark.django_db
def test_assets_by_location_values(priced_sde, settings):
    settings.FORCA_HOME_CORP_ID = 98000001
    _director(98000001)
    responses.add(
        responses.GET, f"{ESI}/corporations/98000001/assets/",
        json=[{
            "item_id": 1, "type_id": 34, "quantity": 1000,
            "location_id": 30000142, "location_type": "solar_system",
        }],
        headers={"X-Pages": "1"}, status=200,
    )
    import_corporation_assets(98000001)
    from apps.stockpile.assets import assets_by_location
    data = assets_by_location(Asset.Owner.CORPORATION, 98000001)
    # Trit base price 5 * 1000 = 5000 (no market price in fixture).
    assert data["total_value"] == Decimal("5000")
    assert data["locations"][0]["value"] == Decimal("5000")


# --- Personal assets: own token, private to the pilot ------------------------
@responses.activate
@pytest.mark.django_db
def test_personal_assets_imported_with_own_token(sde, django_user_model):
    from django.utils import timezone

    from apps.stockpile.assets import CHAR_ASSETS_SCOPE, import_character_assets
    user = django_user_model.objects.create(username="pilot")
    char = EveCharacter.objects.create(character_id=42, user=user, name="Pilot", is_main=True)
    token = AuthToken(character=char, scopes=[CHAR_ASSETS_SCOPE])
    token.refresh_token = "r"
    token.access_token = "a"
    token.access_expires_at = timezone.now() + timezone.timedelta(hours=1)
    token.save()
    responses.add(
        responses.GET, f"{ESI}/characters/42/assets/",
        json=[{"item_id": 1, "type_id": 34, "quantity": 7, "location_id": 30000142, "location_type": "solar_system"}],
        headers={"X-Pages": "1"}, status=200,
    )
    result = import_character_assets(char)
    assert result["status"] == "ok"
    assert Asset.objects.filter(owner_type=Asset.Owner.CHARACTER, owner_id=42, type_id=34).get().quantity == 7
    # Personal assets carry the character owner, never the corporation.
    assert not Asset.objects.filter(owner_type=Asset.Owner.CORPORATION).exists()


@pytest.mark.django_db
def test_personal_assets_no_scope_degrades(sde, django_user_model):
    from apps.stockpile.assets import import_character_assets
    user = django_user_model.objects.create(username="p2")
    char = EveCharacter.objects.create(character_id=43, user=user, name="P2", is_main=True)
    assert import_character_assets(char)["status"] == "no_scope"


@pytest.mark.django_db
def test_assets_page_corp_tab_officer_only(client, django_user_model, sde):
    from apps.identity.models import RoleAssignment
    from apps.sso.services import ensure_role
    from core import rbac
    member = django_user_model.objects.create(username="m")
    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_MEMBER))
    # The pilot must actually be in the corp for the account to hold the member role (LP-4).
    EveCharacter.objects.create(character_id=50, user=member, name="M", is_main=True,
                                is_corp_member=True)
    client.force_login(member)
    # Member sees their own assets; the corp tab silently falls back to 'mine'.
    assert client.get("/stockpile/assets/").status_code == 200
    resp = client.get("/stockpile/assets/?owner=corp")
    assert resp.status_code == 200 and resp.context["owner"] == "mine"


# --- Feature-scope login requests the extra scope ----------------------------
@pytest.mark.django_db
def test_login_feature_scope_requested(client, settings):
    settings.EVE_SSO_DEFAULT_SCOPES = ["publicData"]
    settings.EVE_SSO_FEATURE_SCOPES = {"corp_assets": [CORP_ASSETS_SCOPE]}
    resp = client.get("/auth/eve/login/?feature=corp_assets")
    assert resp.status_code == 302
    assert CORP_ASSETS_SCOPE.replace(".", "%2E") in resp["Location"] or CORP_ASSETS_SCOPE in resp["Location"] \
        or "read_corporation_assets" in resp["Location"]
    assert client.session["eve_sso_scopes"] == ["publicData", CORP_ASSETS_SCOPE]
