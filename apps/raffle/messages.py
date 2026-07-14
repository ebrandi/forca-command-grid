"""Code-defined, gettext-wrapped scaffolds for the raffle's **persisted** prose (Seam B).

Two raffle columns are prose that a **Celery beat worker** writes into the database and a
*different pilot* later reads:

* ``RaffleTicketLedgerEntry.reason`` — "why you got these tickets", shown on ``raffle/me.html``.
  Persisted by :mod:`apps.raffle.engine`; the sentence itself is composed by the ticket
  *sources* (:mod:`apps.raffle.sources.pvp` / :mod:`apps.raffle.sources.pve`).
* ``RaffleSuspiciousActivityFlag.detail`` — "why this was flagged", shown on the officer
  ``raffle_flags`` console. Persisted by :mod:`apps.raffle.integrity`.

Wrapping those f-strings in ``gettext``/``gettext_lazy`` DOES NOT WORK and is the trap this
module exists to avoid:

* The writer is a beat worker. It has no request, no user and no locale — it runs under
  ``settings.LANGUAGE_CODE`` (English). Django coerces a ``gettext_lazy`` proxy to ``str`` on
  ``.save()``/``bulk_create()``, so the row would be frozen in the *writer's* locale and every
  reader, in every language, would see that same frozen string forever. ``makemessages`` would
  happily extract the msgid, the .po would fill up, and nothing would ever translate.
* ``_(f"…")`` is worse: xgettext cannot see inside an f-string, so it is a silent no-op.
* A lazy proxy stored in a ``JSONField`` is a hard ``TypeError`` at save time.

So the English prose stays in the database — it is the audit record *and* the fallback for
legacy rows written before this change — and alongside it we persist a stable ``*_key`` plus a
JSON-safe ``*_params`` dict. The sentence is re-resolved **per reader, at render time**, under
the reader's active locale (:mod:`apps.raffle.rendering_i18n`, and the ``reason_i18n`` /
``detail_i18n`` model properties).

Contract for everything below:

* The msgid is a real literal inside ``_()`` so xgettext sees it. Placeholders are **named**
  ``%(param)s`` — never positional ``%s`` (translators reorder), never an f-string.
* Every param value written at the call site must be **plain JSON-safe** (``int`` / ``str``):
  no lazy proxies, no ``Decimal``, no ``datetime``. Params are interpolated **raw and are never
  translated** — that is deliberate. They carry EVE game data and corp/user content (system
  names, operation titles, directive titles, an officer's free-text reversal note), which stays
  in its source language per the protected-terms policy.
* Anything English *inside* the sentence must live in the msgid, not in a param. That is why
  mining and mentorship each have two keys (``m³``/``units``, ``mentee``/``mentor``) instead of
  one key with the word passed in as a param — a param would never translate.
* Numbers that the English formats with thousands separators are pre-formatted at the write
  site and passed as strings, so English output is byte-identical to before.

Keys are stable identifiers: they are persisted in a database column, so **never rename one**
without leaving the old key resolvable (an unknown key degrades to the stored English prose).
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

# key -> gettext msgid with named %(param)s placeholders.
#
# The (N) tail in the ledger reasons is the ticket count, kept in the msgid so a translator can
# move it; ``tickets`` is always an int.
SCAFFOLDS: dict[str, str] = {
    # --- RaffleTicketLedgerEntry.reason -------------------------------------------------- #
    # PvP (apps/raffle/sources/pvp.py). "Solo kill" / "final blow" are EVE jargon that stays
    # canonical English *inside* the translatable sentence.
    "pvp.solo_kill": _("Solo kill (%(tickets)s)"),
    "pvp.final_blow": _("Final blow on shared kill (%(tickets)s)"),
    "pvp.participation": _("Kill participation (%(tickets)s)"),
    # Mining (apps/raffle/sources/pve.py). Two keys, one per basis: the unit word must be part
    # of the msgid to be translatable ("units" is prose; "m³" is a symbol but the sentence
    # around it still changes shape per language).
    "mining.m3": _("Mined %(volume)s m³ (%(tickets)s tickets)"),
    "mining.units": _("Mined %(quantity)s units (%(tickets)s tickets)"),
    # Fleet. %(operation)s is a corp-authored operation title -> raw, never translated.
    "fleet.attendance": _("Fleet attendance: %(operation)s (%(tickets)s)"),
    # Logistics. %(origin)s / %(dest)s are EVE solar-system names -> raw, never translated.
    "logistics.courier": _("Courier delivered: %(origin)s → %(dest)s (%(tickets)s)"),
    # Mentorship. Two keys so the role word itself translates (it would be frozen as a param).
    "mentorship.mentee": _("Mentorship task completed (mentee, %(tickets)s)"),
    "mentorship.mentor": _("Mentorship task completed (mentor, %(tickets)s)"),
    # Directives. %(title)s is a corp-authored directive title -> raw, never translated.
    "directive.completed": _("Completed “%(title)s”"),
    # Officer reversal (apps/raffle/services.py). %(reason)s is the officer's free-text note:
    # human content, rendered verbatim in every locale (never translated).
    "ledger.reversal": _("Reversal: %(reason)s"),

    # --- RaffleSuspiciousActivityFlag.detail ---------------------------------------------- #
    # %(value)s / %(limit)s arrive pre-formatted as strings so English is byte-identical.
    "integrity.low_value": _("Kill worth %(value)s ISK (< %(limit)s)."),
    "integrity.repeated_victim": _("Same victim killed %(count)s× (limit %(limit)s)."),
}


def render_scaffold(key: str, params: dict | None, fallback: str) -> str:
    """Resolve ``key`` under the **currently active** locale and interpolate ``params``.

    ``fallback`` is the stored English prose. It is returned whenever the scaffold cannot be
    resolved — no key (a legacy row written before this change), an unknown/renamed key, or a
    params dict that does not satisfy the msgid's placeholders (a translator who mangled a
    ``%(name)s``, or a key/params pair that drifted apart across a deploy).

    This function NEVER returns blank and NEVER raises: a broken translation must degrade to the
    stored English, not to an empty ticket reason or a 500 on the officer console.
    """
    msgid = SCAFFOLDS.get(key or "")
    if msgid is None:
        return fallback
    try:
        # str() forces the gettext_lazy proxy to resolve NOW, under the reader's active locale.
        text = str(msgid) % (params or {})
    except (KeyError, TypeError, ValueError):
        return fallback
    return text or fallback
