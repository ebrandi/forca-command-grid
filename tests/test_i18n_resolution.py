"""Locale-resolution precedence, allow-listing, and impersonation (D5-D7, D18)."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from apps.identity.models import User
from core.i18n import config
from core.i18n.resolver import resolve_language

pytestmark = pytest.mark.django_db


def _req(*, cookie=None, accept=None, user=None, real_user=None):
    request = RequestFactory().get("/")
    request.COOKIES = {}
    if cookie is not None:
        request.COOKIES["forca_language"] = cookie
    if accept is not None:
        request.META["HTTP_ACCEPT_LANGUAGE"] = accept
    request.user = user or AnonymousUser()
    request.real_user = real_user if real_user is not None else request.user
    return request


def test_kill_switch_returns_english(settings):
    config.set_i18n_config(locales={"de": True})
    settings.I18N_ENABLED = False
    assert resolve_language(_req(cookie="de")) == "en"


def test_profile_beats_cookie_and_browser():
    config.set_i18n_config(locales={"de": True, "ja": True})
    user = User.objects.create(username="eve:1", language="ja")
    request = _req(cookie="de", accept="de-DE,de;q=0.9", user=user, real_user=user)
    assert resolve_language(request) == "ja"


def test_cookie_when_no_profile():
    config.set_i18n_config(locales={"de": True})
    assert resolve_language(_req(cookie="de")) == "de"


def test_accept_language_when_no_cookie():
    config.set_i18n_config(locales={"de": True})
    assert resolve_language(_req(accept="de-DE,de;q=0.9,en;q=0.5")) == "de"


def test_disabled_locale_falls_back_to_english():
    # 'de' is not enabled in the default config → the cookie is ignored.
    assert resolve_language(_req(cookie="de")) == "en"


def test_malformed_locale_rejected():
    config.set_i18n_config(locales={"de": True})
    allowed = {"en", "de"}
    # Pure garbage / traversal / injection probes have no valid language subtag → English.
    assert resolve_language(_req(cookie="../../../etc/passwd")) == "en"
    assert resolve_language(_req(cookie="'; DROP TABLE users; --")) == "en"
    assert resolve_language(_req(cookie="de;q=1")) == "en"
    # The resolver NEVER returns anything outside the enabled allow-list, even when a
    # malformed value's base subtag is valid: Django extracts the clean canonical code
    # ("de") and discards the region/null-byte/junk — the raw string never propagates
    # (so a locale value can never reach the filesystem). That is the security property.
    for probe in ["de_DE\x00", "de-XX", "DE", "en-US-x-../etc"]:
        assert resolve_language(_req(cookie=probe)) in allowed


def test_impersonation_uses_operator_language():
    config.set_i18n_config(locales={"ru": True, "ja": True})
    director = User.objects.create(username="eve:dir", language="ru")
    pilot = User.objects.create(username="eve:pilot", language="ja")
    # request.user is the impersonated pilot; request.real_user is the director.
    assert resolve_language(_req(user=pilot, real_user=director)) == "ru"


def test_browser_detection_toggle_off():
    config.set_i18n_config(locales={"de": True}, browser_detection=False)
    assert resolve_language(_req(accept="de-DE,de;q=0.9")) == "en"


def test_configured_default_is_honoured_when_no_other_signal():
    # Leadership sets the corp default to German; a visitor with no preference, no
    # cookie, and no matching browser language gets German (not English).
    config.set_i18n_config(locales={"de": True}, default="de")
    assert resolve_language(_req()) == "de"


def test_browser_language_still_beats_the_configured_default():
    config.set_i18n_config(locales={"de": True, "fr": True}, default="de")
    assert resolve_language(_req(accept="fr-FR,fr;q=0.9")) == "fr"


def test_english_remains_the_ultimate_fallback():
    # Default config (English-only) → English.
    assert resolve_language(_req()) == "en"


def test_region_variant_folds_to_base():
    config.set_i18n_config(locales={"de": True})
    # A regional Accept-Language collapses to the supported base variant.
    assert resolve_language(_req(accept="de-AT")) == "de"
