"""SSO-2 (roadmap 2.1) — ingestion-token liveness alerts.

Acceptance: a permanently-dead corp-ingestion token triggers exactly one alert to
the owning director naming the character + data at risk; no repeat for the same
death; a healthy, non-corp, merely-transient, default-login, or still-covered
(superseded) token never alerts; a recovery re-arms so a later death alerts again;
leadership can switch it off.
"""
from __future__ import annotations

import pytest
from django.conf import settings
from django.utils import timezone

from apps.admin_audit.models import AppSetting
from apps.pingboard import config
from apps.pingboard.models import Alert
from apps.sso.models import AuthToken
from apps.sso.token_alerts import scan_ingestion_tokens
from core import rbac
from tests._raffle_utils import add_token, enrol_pilot

pytestmark = pytest.mark.django_db

CORP_SCOPE = "esi-assets.read_corporation_assets.v1"        # a real director ingestion scope
PERSONAL_SCOPE = "esi-skills.read_skills.v1"


@pytest.fixture(autouse=True)
def _reset_config():
    config.reset("notifications")
    config.reset("general")
    yield
    config.reset("notifications")
    config.reset("general")


def _director(django_user_model, cid, *, scopes=(CORP_SCOPE,)):
    user, char = enrol_pilot(
        django_user_model, cid, roles=(rbac.ROLE_DIRECTOR,), scopes=list(scopes)
    )
    return user, AuthToken.objects.get(character__character_id=cid)


def _kill_token(token, *, fail=3, revoked=False):
    token.refresh_fail_count = fail
    if revoked:
        token.revoked_at = timezone.now()
    token.save(update_fields=["refresh_fail_count", "revoked_at", "updated_at"])


def _alerts(user_id=None):
    qs = Alert.objects.filter(source_service="sso")
    return qs.filter(audience={"kind": "user", "id": user_id}) if user_id else qs


def test_dead_corp_token_alerts_owner_once(django_user_model):
    user, token = _director(django_user_model, 1001)
    _kill_token(token, fail=3)
    assert scan_ingestion_tokens()["alerted"] == 1
    assert _alerts(user.id).count() == 1
    body = _alerts(user.id).first().body.lower()
    assert "assets" in body and "re-auth" in body
    assert scan_ingestion_tokens()["alerted"] == 0  # no repeat for the same death
    assert _alerts(user.id).count() == 1


def test_revoked_token_alerts(django_user_model):
    user, token = _director(django_user_model, 1002)
    _kill_token(token, fail=0, revoked=True)
    assert scan_ingestion_tokens()["alerted"] == 1
    assert _alerts(user.id).count() == 1


def test_transient_failure_does_not_alert(django_user_model):
    _user, token = _director(django_user_model, 1003)
    _kill_token(token, fail=2)  # below the permanent-revoke threshold
    assert scan_ingestion_tokens()["alerted"] == 0
    assert not _alerts().exists()


def test_healthy_corp_token_does_not_alert(django_user_model):
    _director(django_user_model, 1004)  # valid token, fail=0
    assert scan_ingestion_tokens()["alerted"] == 0
    assert not _alerts().exists()


def test_dead_personal_only_token_does_not_alert(django_user_model):
    _user, token = _director(django_user_model, 1005, scopes=[PERSONAL_SCOPE])
    _kill_token(token, fail=5)
    assert scan_ingestion_tokens()["alerted"] == 0
    assert not _alerts().exists()


def test_default_login_token_is_not_corp_ingestion(django_user_model):
    # A member's real baseline login token carries a few `…corporation…` default scopes;
    # its death must NOT be read as a corp-ingestion failure (HIGH-2 regression).
    _user, token = _director(django_user_model, 1010, scopes=list(settings.EVE_SSO_DEFAULT_SCOPES))
    _kill_token(token, fail=3)
    assert scan_ingestion_tokens()["alerted"] == 0
    assert not _alerts().exists()


def test_superseded_token_still_covered_does_not_alert(django_user_model):
    # A revoked token whose ingestion scope is still covered by another live token is a
    # supersede, not a death — no alert (HIGH-1 regression).
    _user, token = _director(django_user_model, 1011)
    add_token(token.character, scopes=[CORP_SCOPE])  # a second, live token covering it
    _kill_token(token, revoked=True)
    assert scan_ingestion_tokens()["alerted"] == 0
    assert not _alerts().exists()


def test_recovery_rearms_and_redeath_realerts(django_user_model):
    user, token = _director(django_user_model, 1006)
    _kill_token(token, fail=3)
    scan_ingestion_tokens()
    assert _alerts(user.id).count() == 1
    # Recover: refresh succeeds again → marker reconciled away.
    token.refresh_fail_count = 0
    token.revoked_at = None
    token.save(update_fields=["refresh_fail_count", "revoked_at", "updated_at"])
    assert scan_ingestion_tokens()["alerted"] == 0
    assert not AppSetting.objects.filter(key=f"sso:tokdeath:{token.id}").exists()
    # Dies again later → alerts again.
    _kill_token(token, fail=3)
    assert scan_ingestion_tokens()["alerted"] == 1
    assert _alerts(user.id).count() == 2


def test_orphaned_marker_is_cleaned_up(django_user_model):
    _user, token = _director(django_user_model, 1012)
    _kill_token(token, fail=3)
    scan_ingestion_tokens()
    tid = token.id
    token.delete()  # token row goes away (e.g. member purge) — marker must not orphan
    r = scan_ingestion_tokens()
    assert r["cleared"] >= 1
    assert not AppSetting.objects.filter(key=f"sso:tokdeath:{tid}").exists()


def test_disabled_event_is_a_noop(django_user_model):
    _user, token = _director(django_user_model, 1007)
    _kill_token(token, fail=3)
    config.set("notifications", {"events": {"sso.ingestion_token_dead": {"enabled": False}}})
    assert scan_ingestion_tokens()["status"] == "disabled"
    assert not _alerts().exists()
