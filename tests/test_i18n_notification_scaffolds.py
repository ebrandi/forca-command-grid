"""Wave 4a — the migrated service call sites re-render per recipient locale (doc 08 §7.1).

The proof that migrating a call site from ``body=f"…"`` to ``template_key`` + ``context``
actually buys per-recipient localisation, without depending on a shipped ``.mo``:

* a MIGRATED call site (``killboard.rank_notify``) stores a scaffold key + a raw context, keeps
  ``Alert.body`` frozen in English (the audit column), and ``render_for(alert, "de")`` returns
  GERMAN chrome with the EVE/pilot values still raw;
* an UN-MIGRATED call site (an officer's free-text operation announcement) still delivers its
  English body verbatim in every locale — nothing regressed;
* every ``{slot}`` in every scaffold is a declared ``rendering.VARIABLE_CATALOGUE`` name, and no
  scaffold smuggles a lazy proxy anywhere a JSONField would choke on it.
"""
from __future__ import annotations

import string

import pytest
from django.utils import translation
from django.utils.translation import trans_real

from apps.killboard import rank_notify
from apps.operations import services as ops_services
from apps.operations.models import Operation
from apps.pingboard import messages, rendering
from apps.pingboard.models import Alert
from apps.pingboard.rendering_i18n import render_for
from core.i18n import config as i18n_config

pytestmark = pytest.mark.django_db

_RANK_KEY = "killboard.rank_up"


def _msgid(proxy) -> str:
    """The source (English) msgid behind a ``gettext_lazy`` proxy, with no catalogue active."""
    with translation.override(None):
        return str(proxy)


@pytest.fixture
def de_rank_up():
    """Enable German and inject a de translation for the ``killboard.rank_up`` scaffold msgids."""
    i18n_config.set_i18n_config(locales={"de": True})
    sc = messages.SCAFFOLDS[_RANK_KEY]
    subject_id, body_id = _msgid(sc.subject), _msgid(sc.body)
    de_subject = "Kampfrang erreicht: {rank_name}"
    de_body = (
        "Glückwunsch — du hast den Rang {rank_name} erreicht ({kill_count} Abschüsse "
        "insgesamt erfasst)."
    )
    t = trans_real.translation("de")
    t._catalog[subject_id] = de_subject
    t._catalog[body_id] = de_body
    try:
        yield {"subject": de_subject, "body": de_body}
    finally:
        # Drop the cached catalogue so the injected msgids never leak into another test.
        trans_real._translations.pop("de", None)


def _user(django_user_model, username="eve:4242"):
    return django_user_model.objects.create(username=username, language="de")


# --- 1. a MIGRATED call site localises per recipient --------------------------
def test_migrated_call_site_renders_in_the_recipients_language(django_user_model, de_rank_up):
    user = _user(django_user_model)

    assert rank_notify._send_rank_up(
        character_id=90001, user_id=user.id,
        cur={"name": "Corsair", "min_kills": 100}, kills=1234, note="",
    ) is True

    alert = Alert.objects.get(source_service="killboard")
    # The re-render inputs are persisted, and the context is plain JSON-safe scalars.
    assert alert.template_key == _RANK_KEY
    assert alert.custom_message is False
    assert alert.context == {"rank_name": "Corsair", "kill_count": "1,234"}

    # The stored body stays the FROZEN ENGLISH audit column — byte-identical to the legacy
    # f-string this call site used to pass.
    assert alert.body == (
        "Congratulations — you've reached the rank of Corsair (1,234 lifetime kills recorded). "
        "Ranks update from the nightly combat rollup, so this can reflect kills from the last "
        "day or two rather than the exact moment."
    )

    # …while a German recipient is re-rendered from template_key + context, in GERMAN.
    with translation.override("de"):
        subject_de, body_de = render_for(alert, "de")
    assert body_de == (
        "Glückwunsch — du hast den Rang Corsair erreicht (1,234 Abschüsse insgesamt erfasst)."
    )
    assert subject_de == "Kampfrang erreicht: Corsair"
    # The interpolated EVE/pilot values stay RAW in the translated render (D14.7).
    assert "Corsair" in body_de and "1,234" in body_de

    # English is unchanged, and the frozen audit body was NOT mutated by the German render.
    with translation.override("en"):
        _, body_en = render_for(alert, "en")
    assert body_en == alert.body
    alert.refresh_from_db()
    assert alert.body.startswith("Congratulations — you've reached the rank of Corsair")


# --- 2. an UN-MIGRATED call site is untouched ---------------------------------
def test_unmigrated_call_site_still_delivers_english_verbatim(de_rank_up):
    op = Operation.objects.create(name="Home Defence")
    english = "📣 **Home Defence** — hostiles in home. Undock now."

    alert = ops_services.notify_operation(
        op, title="Home Defence", body=english, source_suffix="announce",
    )

    assert alert is not None
    # No scaffold: an officer's free-text announcement is verbatim in every locale (D14.6).
    assert alert.template_key == ""
    assert alert.custom_message is True
    assert alert.body == english
    for lang in ("en", "de"):
        with translation.override(lang):
            _, body = render_for(alert, lang)
        assert body == english


# --- 3. the scaffold contract holds for EVERY registered key ------------------
def test_every_scaffold_slot_is_a_declared_catalogue_variable():
    catalogue = set(rendering.VARIABLE_CATALOGUE)
    offenders: list[str] = []
    for key, sc in messages.SCAFFOLDS.items():
        for text in (_msgid(sc.subject) if sc.subject else "", _msgid(sc.body)):
            for _lit, field, _spec, _conv in string.Formatter().parse(text):
                if field and field not in catalogue:
                    offenders.append(f"{key}: {{{field}}}")
    assert offenders == []


def test_scaffold_msgids_are_never_empty_bodies():
    for key, sc in messages.SCAFFOLDS.items():
        assert _msgid(sc.body).strip(), f"{key} has an empty body msgid"
