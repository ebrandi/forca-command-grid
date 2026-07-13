"""The public features showcase (/features/) is fully localised.

The showcase copy lives in ``config.showcase_data`` as plain data, not in the template, so the
Phase-2 template sweep never saw it: the page rendered translated *chrome* around 205 strings of
English *content*. These tests pin the fix.

Two properties matter and are easy to regress:

* the copy must be wrapped in ``gettext_lazy``, never ``gettext`` — the module is evaluated once at
  import, so an eager call would freeze whichever locale was active at process start into every
  response for every visitor;
* the category *keys*, sprite ids and screenshot paths are identifiers, not copy. Translating them
  would break ``{% if %}`` filtering and 404 the screenshots.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import translation
from django.utils.functional import Promise
from django.utils.translation import gettext

from config.showcase_data import CATEGORIES, FEATURES, PRIVATE_FEATURES
from core.i18n import config

MO = Path(settings.BASE_DIR) / "locale" / "de" / "LC_MESSAGES" / "django.mo"
needs_catalogue = pytest.mark.skipif(
    not MO.exists(), reason="German catalogue not compiled — run `manage.py compilemessages`."
)


def _copy_strings():
    """Every visitor-facing string in the showcase data."""
    for _key, label in CATEGORIES:
        yield label
    for f in FEATURES:
        yield from (f["title"], f["lede"], f["alt"], *f["benefits"])
        for t in f.get("thumbs", []):
            yield t["alt"]
    for p in PRIVATE_FEATURES:
        yield from (p["tag"], p["title"], p["lede"], *p["benefits"])


def test_all_showcase_copy_is_lazily_translatable():
    strings = list(_copy_strings())
    assert len(strings) > 150, "showcase copy shrank unexpectedly — did an entry lose its wrapper?"
    plain = [s for s in strings if not isinstance(s, Promise)]
    assert not plain, f"showcase copy not wrapped in gettext_lazy: {plain[:5]}"


def test_identifiers_are_not_translated():
    """Keys, sprite ids and screenshot paths must stay plain — translating them breaks the page."""
    for key, _label in CATEGORIES:
        assert isinstance(key, str) and not isinstance(key, Promise)
    for f in FEATURES:
        assert not isinstance(f["cat"], Promise)
        assert f["shot"].startswith("showcase/"), "screenshot path must stay a real static path"
        for t in f.get("thumbs", []):
            assert t["src"].startswith("showcase/")
    for p in PRIVATE_FEATURES:
        assert not isinstance(p["icon"], Promise)


@needs_catalogue
def test_showcase_copy_actually_translates():
    """The lede of the first feature is real translated copy, not the English source."""
    english = "Your Command Center"
    with translation.override("de"):
        assert gettext(english) != english
    with translation.override("pt-br"):
        assert gettext(english) != english


@needs_catalogue
def test_protected_eve_terms_survive_translation():
    """A translated benefit that names a protected term keeps it in English (glossary D16)."""
    english = "Courses of action weighed against your real constraints and doctrine"
    with translation.override("de"):
        translated = gettext(english)
    assert translated != english, "string should be translated"
    assert "doctrine" in translated.lower(), "protected EVE term must survive translation"


@needs_catalogue
@pytest.mark.django_db
def test_features_page_body_renders_german_when_locale_enabled(client):
    """End to end: the feature *content* — not just the chrome — renders in German.

    This is the regression the page shipped with: the template was fully marked, so the chrome
    translated while every title, lede and benefit stayed English.
    """
    config.set_i18n_config(locales={"de": True})
    client.cookies["forca_language"] = "de"

    resp = client.get(reverse("showcase"))
    assert resp.status_code == 200
    html = resp.content.decode()

    with translation.override("de"):
        title = gettext("Your Command Center")
        benefit = gettext("Combat rank, 7-day kills/losses and ISK destroyed, at a glance")

    assert title in html, "feature title did not render in German"
    assert benefit in html, "benefit bullet did not render in German"
    assert "Your Command Center" not in html, "English showcase copy leaked into the German page"


@needs_catalogue
@pytest.mark.django_db
def test_features_page_stays_english_while_locale_disabled(client):
    """Ships dark: until leadership enables a locale, visitors get English (progressive reveal)."""
    client.cookies["forca_language"] = "de"
    resp = client.get(reverse("showcase"))
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Your Command Center" in html
    with translation.override("de"):
        assert gettext("Your Command Center") not in html
