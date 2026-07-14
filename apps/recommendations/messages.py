"""Code-defined, gettext-wrapped scaffolds for the recommendation engine's prose (Seam B).

``Recommendation.message`` and ``Recommendation.logic_summary`` are **persisted** sentences: the
engine beat (a Celery worker, which has no user and no locale) writes them, and *other* people —
officers of eight different locales — read them back off the row later, on ``_rec_card.html`` and
in the chat fan-out.

Wrapping the engine's f-strings in ``gettext``/``gettext_lazy`` therefore CANNOT work. Django
coerces a lazy proxy to ``str`` on ``.save()``, so the row would be frozen in whatever locale the
*writer* happened to have (the worker: English) and every reader would see that, forever — a naive
``_()`` at the write site passes ``makemessages`` and translates nothing. (And a proxy inside the
``inputs``/``suggested_action`` JSONFields is a hard ``TypeError`` at save time.)

So the sentence is split in two and the i18n boundary moves to *read* time:

* the **key + params** are persisted (``message_key``/``message_params``,
  ``logic_summary_key``/``logic_summary_params``) — plain JSON-safe values, never a proxy;
* the **sentence** lives here, in :data:`SCAFFOLDS`, as a real literal ``_("…")`` call with named
  ``%(param)s`` placeholders, so ``xgettext`` can see it (an ``_(f"…")`` or ``_(variable)`` would
  be a silent no-op), and it is resolved with ``str(...)`` under the *reader's* active locale by
  :func:`render` — never at write time;
* the English prose column is **still written**, by :func:`english`, and stays the audit record and
  the fallback. Legacy rows (written before this change, or by a caller outside the engine such as
  ``apps.admin_audit.tasks``) carry no key and simply render their stored English verbatim — they
  degrade to English, never to blank.

Contract for the placeholders: every ``%(param)s`` is a **raw** interpolation slot. The values are
EVE game data (ship/system/type names via ``engine._tname``), numbers, and decision verbs that are
*compared* elsewhere (``suggested_action["verb"]``) — none of them are ever passed through
``gettext``. A sentence that merely *contains* an EVE proper noun is still translatable prose; the
noun inside it is not (D13/D16, enforced by ``core/i18n/data/protected-terms.yml``).
"""
from __future__ import annotations

from django.utils import translation
from django.utils.translation import gettext_lazy as _

# Keyed by ``<evaluator>.<field>``. The key is a stable identifier — it is persisted on the row and
# looked up here, so it is NEVER translated and never derived from prose.
#
# Placeholder parity is load-bearing: a translator who drops or renames a ``%(param)s`` would make
# the interpolation raise, so :func:`render` falls back to the stored English rather than blanking a
# recommendation. Numbers are pre-formatted to strings by the engine (``_isk``, ``m³`` rounding) so
# that no locale-dependent number formatting can sneak into a persisted param.
SCAFFOLDS: dict[str, str] = {
    # --- stock_shortage -----------------------------------------------------------------------
    "stock_shortage.message": _(
        "Stock of %(type_name)s is %(current)s against a target of %(target)s. "
        "Build or buy %(deficit)s."
    ),
    "stock_shortage.logic": _("current < target by deficit"),
    # --- doctrine_readiness -------------------------------------------------------------------
    "doctrine_readiness.message": _("%(ready)s/%(total)s members can fly '%(doctrine)s'."),
    "doctrine_readiness.logic": _("count of members at viable+ readiness vs total members"),
    # --- build_vs_buy -------------------------------------------------------------------------
    # %(decision)s is the raw verb from ``bom.decide_build_or_buy`` ("build"/"buy"); it is also the
    # ``suggested_action["verb"]`` that the UI dispatches on, so it stays canonical English.
    "build_vs_buy.message": _(
        "For %(type_name)s ×%(quantity)s: %(decision)s "
        "(build %(build)s vs buy %(buy)s ISK)."
    ),
    "build_vs_buy.logic": _("compare build cost (BOM × prices) vs market buy"),
    # --- market_seeding -----------------------------------------------------------------------
    "market_seeding.message": _("Seed %(deficit)s × %(type_name)s at %(location)s."),
    "market_seeding.logic": _("target minus local sell-order volume"),
    # --- hauling ------------------------------------------------------------------------------
    "hauling.message": _("%(count)s hauling task(s) open, ~%(volume)s m³ to move."),
    "hauling.logic": _("sum of open hauling-task volumes"),
    # --- combat_loss --------------------------------------------------------------------------
    "combat_loss.message": _("Lost %(count)s × %(ship_name)s in the last %(window_days)sd."),
    "combat_loss.logic": _("victim ship recurs >= %(threshold)s times in window"),
}


def render(key: str, params: dict | None, *, fallback: str = "") -> str:
    """The read seam: ``key`` + ``params`` resolved under the **reader's** active locale.

    Returns ``fallback`` (the stored English prose) for an unknown/empty key — that is the legacy
    path, and it is why a row written before this change still reads correctly. Never raises and
    never returns blank while ``fallback`` is non-blank: a translator who mangles a ``%(param)s``
    breaks the interpolation, and a broken .po entry must degrade to English, not blank an
    officer's dashboard.
    """
    proxy = SCAFFOLDS.get(key or "")
    if proxy is None:
        return fallback
    try:
        # str() resolves the lazy proxy NOW, under the active locale — never before.
        return (str(proxy) % (params or {})) or fallback
    except (KeyError, IndexError, TypeError, ValueError):
        return fallback


def english(key: str, params: dict | None, *, fallback: str = "") -> str:
    """``render`` pinned to English — the value that is safe to **persist**.

    Mirrors ``notify._canonical_type_label``: the write side stores canonical English so the row is
    a stable audit record and a locale-independent fallback, and the *display* side stays free to
    translate. Deriving the prose column from the same scaffold the key points at is what keeps the
    two from drifting apart.
    """
    with translation.override("en"):
        return render(key, params, fallback=fallback)
