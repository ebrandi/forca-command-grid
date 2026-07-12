"""Foundation wiring: settings, middleware placement, context processors, i18n.config.

Grounded in docs/i18n/design/13-testing-strategy.md. Kept English-default so the
existing English content-assertions across the suite stay green.
"""
from __future__ import annotations

import pytest
from django.conf import settings

from core.i18n import config

LAUNCH = ["en", "pt-br", "es", "fr", "ru", "de", "zh-hans", "ko", "ja"]


def test_languages_and_default():
    codes = dict(settings.LANGUAGES)
    for code in LAUNCH:
        assert code in codes, f"{code} missing from LANGUAGES"
    assert settings.LANGUAGE_CODE == "en"
    assert settings.USE_I18N is True


def test_locale_paths_and_native_labels():
    assert any(str(p).replace("\\", "/").endswith("/locale") for p in settings.LOCALE_PATHS)
    assert settings.LANGUAGE_NATIVE_NAMES["de"] == "Deutsch"        # not "German"
    assert settings.LANGUAGE_NATIVE_NAMES["ja"] == "日本語"
    assert settings.LANGUAGE_NATIVE_NAMES["pt-br"] == "Português (Brasil)"


def test_middleware_placed_after_impersonation_before_gates():
    mw = list(settings.MIDDLEWARE)
    assert "core.i18n.LocaleMiddleware" in mw
    i_imp = mw.index("apps.impersonation.middleware.ImpersonationMiddleware")
    i_loc = mw.index("core.i18n.LocaleMiddleware")
    i_gate = mw.index("core.middleware.MembershipGateMiddleware")
    assert i_imp < i_loc < i_gate


def test_context_processors_wired():
    procs = settings.TEMPLATES[0]["OPTIONS"]["context_processors"]
    assert "django.template.context_processors.i18n" in procs
    assert "core.i18n.context.selector" in procs


@pytest.mark.django_db
def test_default_is_english_only():
    assert config.enabled_locales() == ["en"]
    assert config.is_i18n_enabled() is True


@pytest.mark.django_db
def test_enabling_locales_and_available_rows():
    config.set_i18n_config(locales={"de": True, "ja": True})
    assert set(config.enabled_locales()) >= {"en", "de", "ja"}
    rows = {r["code"]: r["native"] for r in config.available_locales()}
    assert rows["de"] == "Deutsch" and rows["ja"] == "日本語"


@pytest.mark.django_db
def test_english_cannot_be_disabled():
    config.set_i18n_config(locales={"en": False, "de": True})
    assert "en" in config.enabled_locales()


@pytest.mark.django_db
def test_kill_switch_forces_english(settings):
    config.set_i18n_config(locales={"de": True})
    settings.I18N_ENABLED = False
    assert config.enabled_locales() == ["en"]
    assert config.is_i18n_enabled() is False
