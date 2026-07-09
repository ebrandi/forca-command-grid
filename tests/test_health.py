"""Integrations-health panel: token status, feed freshness, director gating."""
from __future__ import annotations

import pytest
from django.utils import timezone

from apps.admin_audit.health import integration_health, record_sync
from apps.corporation.models import EveCorporation
from apps.identity.models import RoleAssignment
from apps.sso.models import AuthToken, EveCharacter
from apps.sso.services import ensure_role
from apps.stockpile.assets import CORP_ASSETS_SCOPE
from core import rbac


def _user(django_user_model, username, role):
    user = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    return user


@pytest.mark.django_db
def test_record_sync_and_feed_freshness(sde):
    record_sync("corp_assets", character="Akusaa III", types=8)
    health = integration_health()
    assets = next(f for f in health["feeds"] if f["key"] == "corp_assets")
    assert assets["status"] == "ok"
    assert assets["by"] == "Akusaa III"
    # A feed never synced shows as missing.
    history = next(f for f in health["feeds"] if f["key"] == "market_history")
    assert history["status"] == "missing"


@pytest.mark.django_db
def test_token_health_flags_asset_scope(sde):
    EveCorporation.objects.get_or_create(corporation_id=98000001)
    char = EveCharacter.objects.create(
        character_id=5, name="Akusaa III", corporation_id=98000001, is_corp_member=True
    )
    tok = AuthToken(character=char, scopes=[CORP_ASSETS_SCOPE, "publicData"])
    tok.refresh_token = "r"
    tok.access_token = "a"
    tok.access_expires_at = timezone.now() + timezone.timedelta(hours=1)
    tok.save()

    health = integration_health()
    assert health["has_asset_token"] is True
    t = health["tokens"][0]
    assert t["scopes"]["corp_assets"] is True
    assert t["healthy"] is True


@pytest.mark.django_db
def test_sde_and_beat_health(sde):
    """0.10: the health payload surfaces the loaded SDE build + every per-beat
    last-success stamp (including ones feed_health doesn't cover, e.g. Jita prices)."""
    from apps.admin_audit.models import AppSetting

    AppSetting.objects.update_or_create(
        key="sde_version", defaults={"value": {"version": "fuzzwork-20260705"}}
    )
    record_sync("market_jita_prices", types=11532)

    health = integration_health()
    assert health["sde"]["version"] == "fuzzwork-20260705"
    assert health["sde"]["status"] == "ok"
    assert health["sde"]["loaded_at"] is not None

    jita = next(b for b in health["beats"] if b["key"] == "market_jita_prices")
    assert jita["label"] == "Market Jita prices"
    assert jita["status"] == "ok"
    assert jita["detail"]["types"] == 11532


@pytest.mark.django_db
def test_sde_health_missing_when_never_loaded(db):
    """With no SDE ever loaded the panel reports 'missing', not a stale/false value."""
    health = integration_health()
    assert health["sde"]["status"] == "missing"
    assert health["sde"]["version"] is None


@pytest.mark.django_db
def test_health_page_is_director_only(client, django_user_model, sde):
    # Member -> 403.
    member = _user(django_user_model, "m", rbac.ROLE_MEMBER)
    client.force_login(member)
    assert client.get("/ops/health/").status_code == 403

    # Director -> 200 and renders the panels (incl. the new SDE + syncs sections).
    director = _user(django_user_model, "d", rbac.ROLE_DIRECTOR)
    client.force_login(director)
    resp = client.get("/ops/health/")
    assert resp.status_code == 200
    assert b"Integrations health" in resp.content
    assert b"ESI tokens" in resp.content
    assert b"SDE static data" in resp.content
    assert b"Scheduled syncs" in resp.content
