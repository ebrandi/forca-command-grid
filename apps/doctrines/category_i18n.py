"""Render-time i18n seam for the seeded ``DoctrineCategory`` labels (Seam A).

``DoctrineCategory.label`` is **seeded into the database** — migration ``0003`` and
:func:`apps.doctrines.services.imported_category` both write the built-in "IMPORTED"
category. A ``gettext_lazy`` proxy cannot help here: Django coerces it to ``str`` on
``.save()``, so whatever locale happened to be active when the row was created would be
frozen into the column forever.

So the label stays plain ``str`` in the DB — canonical English, the audit record and the
fallback — marked for extraction with ``gettext_noop`` (Django's ``makemessages`` passes
``--keyword=gettext_noop``, so xgettext sees it exactly as it sees ``_()``), and it is
translated at *render* time by :func:`category_label`, keyed on the stable ``key`` column.

Leader-created categories, and built-in rows whose label a leader has since edited, are
corp content: rendered verbatim in every locale. The stored value is always the floor, so
this can never blank a category.

``key`` itself is an identifier (``get_or_create`` looks the row up by it) and is never
translated.
"""
from __future__ import annotations

from django.utils.translation import gettext, gettext_noop

IMPORTED_CATEGORY_KEY = "imported"

# {stable key column -> the shipped English label seeded for it}.
BUILTIN_CATEGORY_LABELS: dict[str, str] = {
    IMPORTED_CATEGORY_KEY: gettext_noop("IMPORTED"),
}


def category_label(key: str, stored: str) -> str:
    """The label to *display* for the ``DoctrineCategory`` row ``key``.

    Translated only while the row still holds the shipped English for a built-in key;
    an unknown key (a leader's own category) or an edited label is returned verbatim.
    """
    if stored and stored == BUILTIN_CATEGORY_LABELS.get(key or ""):
        return gettext(stored)
    return stored
