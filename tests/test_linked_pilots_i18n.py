"""Linked Pilots in every language, and the language surviving every pilot operation (LP-8).

Language belongs to the HUMAN, not to a pilot. ``core.i18n.resolver`` reads it from
``real_user.language`` → the ``forca_language`` cookie → ``Accept-Language`` → the corp default,
and never from a character. Switching pilots therefore *cannot* change it — but "cannot" is only
true until someone keys a preference off the active pilot, so these tests pin it.

The rendering tests are the other half: a page whose strings are marked but never translated
looks exactly like a page that works, because an unknown msgid silently falls back to English.
"""
from __future__ import annotations

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import translation

from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

LOCALES = [code for code, _name in settings.LANGUAGES]


@pytest.fixture(autouse=True)
def _all_locales_enabled(db):
    """Turn every supported locale on for these tests.

    The shipped default is English-only (``core.i18n.config._DEFAULTS``) — the third of the three
    rollout gates, so leadership opts each language in. Without this the resolver would refuse
    every locale we ask for and quietly serve English, and every assertion below would pass for
    the wrong reason.
    """
    from core.i18n import config

    config.set_i18n_config(locales={code: True for code in LOCALES})


def _account(django_user_model, username="eve:1", language=""):
    user = django_user_model.objects.create(username=username, language=language)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    return user


def _pilot(user, character_id, name, *, main=False):
    return EveCharacter.objects.create(
        character_id=character_id, user=user, name=name, is_main=main, is_corp_member=True,
    )


# --- language survives every pilot operation ----------------------------------------------
@pytest.mark.django_db
def test_switching_pilots_does_not_change_the_language(client, django_user_model):
    user = _account(django_user_model, language="pt-br")
    _pilot(user, 1, "Main", main=True)
    alt = _pilot(user, 2, "Alt")
    client.force_login(user)

    before = client.get(reverse("identity:linked_pilots"))["Content-Language"]
    client.post(reverse("identity:pilot_switch"), {"character_id": alt.character_id})
    after = client.get(reverse("identity:linked_pilots"))["Content-Language"]

    assert before == after == "pt-br"


@pytest.mark.django_db
def test_unlinking_and_promoting_do_not_change_the_language(client, django_user_model):
    user = _account(django_user_model, language="ja")
    _pilot(user, 1, "Main", main=True)
    alt = _pilot(user, 2, "Alt")
    client.force_login(user)

    client.post(reverse("identity:pilot_main"), {"character_id": alt.character_id})
    assert client.get(reverse("identity:linked_pilots"))["Content-Language"] == "ja"

    client.post(reverse("identity:pilot_unlink"), {"character_id": 1})
    assert client.get(reverse("identity:linked_pilots"))["Content-Language"] == "ja"


@pytest.mark.django_db
def test_the_language_preference_is_the_humans_not_the_pilots(django_user_model):
    """There is exactly one language column, and it is on the account. If a future change ever
    moves it onto EveCharacter, this test is the one that should stop it."""
    user = _account(django_user_model, language="de")
    assert user.language == "de"
    assert not hasattr(EveCharacter, "language")


@pytest.mark.django_db
def test_an_anonymous_language_cookie_survives_the_sso_round_trip(client, django_user_model):
    """The SSO callback cycles the session key. The language lives in a cookie (and, once you
    are signed in, in the account row), so the round trip through EVE cannot lose it."""
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "fr"
    client.get(reverse("sso:login"))
    # cycle_key() copies the session contents to a new key; the cookie is untouched by it.
    assert client.cookies[settings.LANGUAGE_COOKIE_NAME].value == "fr"


# --- the page renders, translated, in every supported locale ------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("locale", LOCALES)
def test_the_linked_pilots_page_renders_in_every_supported_locale(
    client, django_user_model, locale
):
    user = _account(django_user_model, language=locale)
    _pilot(user, 1, "Main", main=True)
    _pilot(user, 2, "Alt")
    client.force_login(user)

    resp = client.get(reverse("identity:linked_pilots"))
    assert resp.status_code == 200
    assert resp["Content-Language"] == locale
    html = resp.content.decode()
    assert f'lang="{locale}"' in html
    # The pilots themselves are always rendered — a missing catalogue must never blank the page.
    assert "Main" in html and "Alt" in html


@pytest.mark.django_db
@pytest.mark.parametrize("locale", [c for c in LOCALES if c != "en"])
def test_the_feature_is_actually_translated_not_falling_back_to_english(locale):
    """The failure this catches is the quiet one: strings marked for translation, shipped
    untranslated, rendering in English inside an otherwise-Portuguese page. English fallback is
    indistinguishable from success unless you look for it.

    A representative sample across the surfaces: the nav item, the page heading, a button, a
    status label, and the isolation promise that is the whole point of the feature.
    """
    from django.utils.translation import gettext

    samples = [
        "Linked Pilots",
        "Link Another Pilot",
        "Switch Pilot",
        "Current Pilot",
        "Main Pilot",
        "Reauthorise",
        "ESI authorisation required",
        "That pilot is not linked to your account.",
        (
            "Linking pilots lets you switch between them without logging out. Each pilot keeps "
            "their own data, permissions, corporation membership, ESI authorisation and dashboard."
        ),
    ]
    with translation.override(locale):
        untranslated = [s for s in samples if gettext(s) == s]
    assert not untranslated, (
        f"{locale}: {len(untranslated)} string(s) still render in English — "
        f"the catalogue is missing them: {untranslated}"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("locale", [c for c in LOCALES if c != "en"])
def test_the_pilot_count_plural_is_translated_for_every_form(locale):
    """Plurals are where a catalogue quietly half-works: ru/pl have three forms, ja/zh/ko one.
    A msgstr[1] left empty renders as an empty string, not as English — a blank in the UI."""
    from django.utils.translation import ngettext

    with translation.override(locale):
        for n in (1, 2, 5, 21):
            rendered = ngettext(
                "You have %(counter)s linked pilot.",
                "You have %(counter)s linked pilots.",
                n,
            ) % {"counter": n}
            assert rendered.strip(), f"{locale}: empty plural form at n={n}"
            assert str(n) in rendered, f"{locale}: the count vanished at n={n}: {rendered!r}"


@pytest.mark.django_db
@pytest.mark.parametrize("locale", [c for c in LOCALES if c != "en"])
def test_placeholders_survive_translation(locale):
    """A translator who renames %(pilot)s to %(piloto)s does not produce a typo — they produce a
    KeyError at render time, in production, in that locale only."""
    from django.utils.translation import gettext

    cases = {
        "You are now flying as %(pilot)s.": {"pilot": "Tester"},
        "%(pilot)s is now your main pilot.": {"pilot": "Tester"},
        "%(pilot)s is now linked to your account.": {"pilot": "Tester"},
        "%(pilot)s has been reauthorised.": {"pilot": "Tester"},
        "%(pilot)s has been unlinked from your account.": {"pilot": "Tester"},
        "%(pilot)s has been unlinked. You are now flying as %(replacement)s.":
            {"pilot": "A", "replacement": "B"},
    }
    with translation.override(locale):
        for msgid, params in cases.items():
            rendered = gettext(msgid) % params  # raises KeyError if a placeholder was renamed
            for value in params.values():
                assert value in rendered, f"{locale}: {msgid!r} dropped a placeholder"


@pytest.mark.django_db
@pytest.mark.parametrize("locale", [c for c in LOCALES if c != "en"])
def test_the_protected_eve_term_stays_english(locale):
    """`killboard` is community English that CCP has not officially translated, so the
    terminology policy keeps it verbatim in every locale (core/i18n/data/protected-terms.yml)."""
    from django.utils.translation import gettext

    msgid = (
        "Switching pilots changes the identity you are currently using in FORCA Command Grid. "
        "Assets, skills, wallets, readiness, killboard records, corporation roles and feature "
        "access are never combined across your pilots — each pilot sees only what that pilot "
        "may see."
    )
    with translation.override(locale):
        assert "killboard" in gettext(msgid).lower()
