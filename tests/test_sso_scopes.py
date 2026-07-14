"""ESI Scopes page — every grantable feature must be surfaced with its grant state.

Regression guard for the bug where the page hardcoded only 3 of the 9 feature
scopes, so most features could never be granted through the UI.
"""
from __future__ import annotations

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model

from apps.sso import scopes as scope_catalog
from apps.sso.models import EveCharacter, EveScopeGrant

SCOPES_URL = "/auth/eve/scopes/"


# --- catalog stays in sync with the allowlist --------------------------------
def test_every_settings_feature_has_a_catalog_entry():
    settings_keys = set(settings.EVE_SSO_FEATURE_SCOPES)
    catalog_keys = set(scope_catalog.FEATURES_BY_KEY)
    assert settings_keys == catalog_keys, (
        f"catalog/allowlist drift — only in settings: {settings_keys - catalog_keys}; "
        f"only in catalog: {catalog_keys - settings_keys}"
    )


def test_every_feature_resolves_real_scopes():
    for f in scope_catalog.FEATURES:
        assert f.scopes, f"feature {f.key} resolves no scopes"
        assert f.audience in (scope_catalog.PILOT, scope_catalog.DIRECTOR)


# --- feature_states grant logic ----------------------------------------------
def test_feature_state_granted_only_when_all_scopes_present():
    corp_assets = scope_catalog.FEATURES_BY_KEY["corp_assets"].scopes
    # All scopes present -> granted.
    full = scope_catalog.feature_states(set(corp_assets))
    assert next(s for s in full if s["feature"].key == "corp_assets")["granted"] is True
    # Missing one scope -> not granted, and it's reported as missing.
    partial = scope_catalog.feature_states({corp_assets[0]})
    state = next(s for s in partial if s["feature"].key == "corp_assets")
    assert state["granted"] is False
    assert corp_assets[1] in state["missing"]


def test_feature_states_can_filter_by_audience():
    pilot = scope_catalog.feature_states(set(), scope_catalog.PILOT)
    director = scope_catalog.feature_states(set(), scope_catalog.DIRECTOR)
    assert {s["feature"].key for s in pilot} == {
        "personal_assets", "my_industry", "freight_search", "my_contracts", "mail_relay",
        "fleet_tracking", "mentorship_presence", "planetary_industry",
    }
    assert "corp_assets" in {s["feature"].key for s in director}
    assert "corp_contracts" in {s["feature"].key for s in director}
    assert "personal_assets" not in {s["feature"].key for s in director}


# --- the page itself ---------------------------------------------------------
@pytest.fixture
def _login(db, client):
    User = get_user_model()
    user = User.objects.create(username="eve:7000", first_name="Pilot")
    user.set_unusable_password()
    user.save()
    client.force_login(user)
    return user, client


@pytest.mark.django_db
def test_corp_member_sees_every_feature(_login):
    user, client = _login
    EveCharacter.objects.create(character_id=7001, user=user, name="Director Pilot",
                                is_main=True, is_corp_member=True)
    html = client.get(SCOPES_URL).content.decode()
    # All 9 feature labels are present (pilot + director).
    for feature in scope_catalog.FEATURES:
        # ``label`` is a gettext_lazy proxy — coerce before the substring check.
        assert str(feature.label) in html, f"missing feature on page: {feature.label}"
    # Each ungranted feature offers a grant link with its key.
    for feature in scope_catalog.FEATURES:
        assert f"?feature={feature.key}" in html


@pytest.mark.django_db
def test_non_corp_member_hides_director_features(_login):
    user, client = _login
    EveCharacter.objects.create(character_id=7002, user=user, name="Alliance Pilot",
                                is_main=True, is_corp_member=False)
    html = client.get(SCOPES_URL).content.decode()
    # Pilot features show…
    assert "Track my assets" in html
    assert "?feature=personal_assets" in html
    # …director ones do not.
    assert "?feature=corp_assets" not in html
    assert "Member tracking" not in html


@pytest.mark.django_db
def test_granted_feature_shows_granted_not_a_button(_login):
    user, client = _login
    char = EveCharacter.objects.create(character_id=7003, user=user, name="Pilot",
                                       is_main=True, is_corp_member=True)
    for scope in scope_catalog.FEATURES_BY_KEY["personal_assets"].scopes:
        EveScopeGrant.objects.create(character=char, scope=scope, active=True)
    html = client.get(SCOPES_URL).content.decode()
    assert "Granted" in html
    # The granted feature no longer offers its grant link…
    assert "?feature=personal_assets" not in html
    # …but an ungranted one still does.
    assert "?feature=freight_search" in html


@pytest.mark.django_db
def test_revoked_grant_does_not_count_as_granted(_login):
    user, client = _login
    char = EveCharacter.objects.create(character_id=7004, user=user, name="Pilot",
                                       is_main=True, is_corp_member=True)
    for scope in scope_catalog.FEATURES_BY_KEY["personal_assets"].scopes:
        EveScopeGrant.objects.create(character=char, scope=scope, active=False)
    html = client.get(SCOPES_URL).content.decode()
    # Inactive grants must re-prompt, not show as granted.
    assert "?feature=personal_assets" in html
