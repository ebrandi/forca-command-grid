"""End-to-end proof of the catalogue pipeline (Phase 2a).

Marks → extract → translate → compile → resolve → render: a compiled German
catalogue renders in the UI, an untranslated string falls back to English (never
blank), and the middleware activates the locale from the language cookie.

Requires the compiled `.mo` (locale/de/LC_MESSAGES/django.mo). Skips if absent so a
checkout that hasn't run `compilemessages` stays green. The 17 marked strings are
shared chrome (base.html + _user_block.html); the German values are DRAFT
(machine-assisted, unreviewed) and exist to prove the pipeline, not to claim German
coverage.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext

from core.i18n import config

MO = Path(settings.BASE_DIR) / "locale" / "de" / "LC_MESSAGES" / "django.mo"
pytestmark = pytest.mark.skipif(
    not MO.exists(), reason="German catalogue not compiled — run `manage.py compilemessages`."
)


def test_compiled_german_chrome_translates():
    with translation.override("de"):
        assert gettext("Log in") == "Anmelden"
        assert gettext("Log in with EVE") == "Mit EVE anmelden"
        assert gettext("Skip to content") == "Zum Inhalt springen"
        assert gettext("No matches.") == "Keine Treffer."


def test_untranslated_entry_falls_back_to_english_not_blank():
    with translation.override("de"):
        # A msgid with no catalogue entry falls back to its English source under any
        # active locale — a missing translation never renders blank.
        src = "§ FORCA source string with no catalogue entry §"
        assert gettext(src) == src


def test_english_is_unchanged_under_default():
    with translation.override("en"):
        assert gettext("Log in") == "Log in"
        assert gettext("Skip to content") == "Skip to content"


@pytest.mark.django_db
def test_landing_page_renders_german_when_locale_enabled_and_cookied(client):
    config.set_i18n_config(locales={"de": True})
    client.cookies["forca_language"] = "de"
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Zum Inhalt springen" in html   # skip-link (base.html), translated
    assert "Anmelden" in html              # "Log in" button chrome, translated
    assert resp["Content-Language"] == "de"


@pytest.mark.django_db
def test_disabled_german_renders_english(client):
    # de present in LANGUAGES but NOT enabled in i18n.config → English (progressive reveal).
    client.cookies["forca_language"] = "de"
    resp = client.get("/")
    html = resp.content.decode()
    assert "Skip to content" in html and "Zum Inhalt springen" not in html
