"""Render-time i18n seam for the seeded ``GuaranteedBuybackConfig.intro_text`` (Seam A).

``GuaranteedBuybackConfig.intro_text`` is **seeded into the database** — both the model field
default and migration ``0003`` write the same canonical English blurb shown to members before they
commit a guaranteed-buyback lot. A ``gettext_lazy`` proxy cannot help here: Django coerces it to
``str`` on ``.save()``, so whatever locale happened to be active when the singleton row was created
would be frozen into the column forever.

So the intro stays plain ``str`` in the DB — canonical English, the audit record and the fallback —
marked for extraction with ``gettext_noop`` (Django's ``makemessages`` passes
``--keyword=gettext_noop``, so xgettext sees it exactly as it sees ``_()``), and it is translated at
*render* time by :func:`intro_text_for`, keyed on the singleton's stable constant key.

An officer-edited intro is corp content: rendered verbatim in every locale. The stored value is
always the floor, so this can never blank the intro.
"""
from __future__ import annotations

from django.utils.translation import gettext, gettext_noop

# The guaranteed-buyback config is a singleton with no natural key column, so a stable module
# constant addresses its one seeded field.
GUARANTEED_CONFIG_KEY = "guaranteed"

# {stable key -> the shipped English seeded for it}. Copied VERBATIM from the
# ``GuaranteedBuybackConfig.intro_text`` field default (models.py) and migration 0003.
SEED: dict[str, str] = {
    GUARANTEED_CONFIG_KEY: gettext_noop(
        "The corp guarantees to buy your lot at the quoted price. Submit it, an officer "
        "approves, then the corp pays you in-game. No ISK moves through this app."
    ),
}


def intro_text_for(key: str, stored: str) -> str:
    """The intro to *display* for the guaranteed-buyback config ``key``.

    Translated only while the row still holds the shipped English for the built-in key;
    an edited intro (an officer's own words) is returned verbatim.
    """
    if stored and stored == SEED.get(key or ""):
        return gettext(stored)
    return stored
