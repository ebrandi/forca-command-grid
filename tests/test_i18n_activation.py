"""Language activation + language-scoped cache keys (D17), and English-as-source."""
from __future__ import annotations

from django.utils import translation
from django.utils.translation import gettext

from core.i18n.cache import i18n_cache_key


def test_cache_key_carries_active_language():
    with translation.override("de"):
        assert i18n_cache_key("briefing:42") == "briefing:42:de"
    with translation.override("ja"):
        assert i18n_cache_key("briefing:42") == "briefing:42:ja"


def test_cache_keys_isolate_locales():
    with translation.override("de"):
        de_key = i18n_cache_key("readiness:overview")
    with translation.override("ru"):
        ru_key = i18n_cache_key("readiness:overview")
    assert de_key != ru_key


def test_no_active_language_defaults_to_en():
    with translation.override(None):
        # deactivate_all → get_language() is None → key falls back to 'en'.
        assert i18n_cache_key("x").endswith(":en")


def test_untranslated_string_falls_back_to_english():
    # A string with no catalogue entry returns its English source (msgid) under any
    # active locale. This is what keeps the suite's English content-assertions green
    # for the vast majority of not-yet-translated strings — and guarantees a missing
    # translation never renders blank.
    with translation.override("de"):
        assert translation.get_language() == "de"
        assert (
            gettext("This source string is intentionally never translated")
            == "This source string is intentionally never translated"
        )
