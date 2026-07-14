"""Code-defined, gettext-wrapped scaffolds for the ERP prose that is PERSISTED.

Seam B. A ``BuildJob.blocked_reason`` is written by :func:`apps.erp.services.recheck_block`,
which runs from the job board *and* from the Plan → Job bridge; a ``BuildJob.note`` pushed from
an industry plan is written by that bridge. Neither writer is the reader: the row is read back
by every other pilot on ``erp/board.html``, in whatever language *they* chose. A Celery worker
(or any writer) has no reader and no locale, so whatever locale is active at ``.save()`` would be
frozen into the row and shown to everyone, forever.

Which is why ``gettext``/``gettext_lazy`` at the *write* site is a trap here, not a fix:

* Django coerces a lazy proxy to ``str`` on ``.save()``. Wrapping the sentence would only change
  *which* single language gets frozen (the writer's: English). It sails through ``makemessages``
  and translates nothing for the reader.
* A lazy proxy stored into a ``JSONField`` is a hard ``TypeError`` at save time.

So the sentence is never persisted in a translated form. What is persisted is a **key** into this
registry plus a **params** dict of plain JSON-safe values (ints / strings — never a proxy, never a
pre-translated word). The reader's locale is applied at *read* time by :func:`render_text`, which
resolves the scaffold under the currently-active catalogue and interpolates the raw params.

Contract:

* Every msgid is a real literal ``_("…")`` so ``xgettext`` can see it. ``_(f"…")`` is a silent
  no-op and must never appear.
* Placeholders are **named** ``%(name)s`` — positional/``{}``/f-string interpolation is banned
  because translators reorder clauses.
* The interpolated values stay RAW. EVE game data (the short material names) and corp-authored
  content (an ``IndustryProject.name``) pass through untranslated by policy; a sentence that
  merely *contains* one is still prose and is marked here.
* The English prose column is still written at the write site, from :func:`english_text` — which
  resolves the very same msgid with translations *deactivated*, so the stored English is the msgid
  verbatim no matter what locale the writer happened to be in. A job queued from a German
  officer's browser therefore still stores English.
* Nothing is backfilled. A legacy row (written before this landed) simply carries no key and
  :func:`render_text` returns its stored English verbatim. The render can never blank.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

# key -> gettext msgid with named %(param)s placeholders.
#
# A key is an identifier: it is persisted, compared and looked up, so it is NEVER translated and
# never reworded — changing a key orphans every row that carries it (which degrades safely to the
# stored English, but loses the translation).
SCAFFOLDS: dict[str, str] = {
    # --- BuildJob.blocked_reason (services.recheck_block) --------------------
    # ``%(materials)s`` is a comma-joined list of SdeType names — EVE game data, kept raw and
    # English by policy (core/i18n/data/protected-terms.yml). The sentence around it is chrome.
    # Two keys rather than one so the "more short than shown" ellipsis lives inside the msgid
    # instead of being smuggled through a param a translator can never reach.
    "job.blocked_short": _("Short: %(materials)s"),
    "job.blocked_short_truncated": _("Short: %(materials)s…"),
    # --- BuildJob.note (the Plan → Job bridge) ------------------------------
    # ``%(plan)s`` is the corp-authored IndustryProject name: raw, never translated.
    "job.from_plan": _("From plan: %(plan)s"),
    # A non-corp plan's name is deliberately not surfaced on the corp-visible board, so this
    # variant has no slot at all.
    "job.from_leadership_plan": _("From a leadership plan"),
}


def render_text(key: str, params: dict | None, fallback: str) -> str:
    """The sentence for ``key`` under the READER's active locale — or ``fallback``.

    This is the read side of the seam. ``str(...)`` forces the ``gettext_lazy`` proxy to resolve
    *now*, against whatever catalogue is active for the reader (their request's language).

    Never blanks and never raises: an unknown key, a params dict missing a placeholder, or any
    interpolation error all fall back to the stored English prose, which every row has.
    """
    if not key:
        return fallback
    scaffold = SCAFFOLDS.get(key)
    if scaffold is None:
        return fallback  # key from a newer/older deploy — degrade to the stored English
    try:
        return str(scaffold) % (params or {})
    except (KeyError, TypeError, ValueError):
        return fallback


def english_text(key: str, params: dict | None = None) -> str:
    """The msgid for ``key`` interpolated with ``params``, in **source English**.

    ``translation.override(None)`` deactivates translation entirely, so ``str(proxy)`` returns the
    msgid verbatim rather than the active catalogue's msgstr. That is what makes the persisted
    prose column locale-independent: a job queued from a German officer's request stores the same
    English bytes the Celery worker does.
    """
    from django.utils import translation

    scaffold = SCAFFOLDS.get(key)
    if scaffold is None:
        return ""
    with translation.override(None):
        return str(scaffold) % (params or {})
