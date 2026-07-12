"""Code-defined, gettext-wrapped message scaffolds (design doc 08 §7.1).

This is the **single seam** where a service notification's sentence becomes
translatable. A DB ``AlertTemplate.body`` is corp-generated content (verbatim in every
locale, D14.8); a custom officer alert is human free-text (verbatim, D14.6). Only the
scaffolds here carry a ``gettext`` msgid, so only they localise per recipient.

Contract (enforced by review + the terminology linter, doc 08 §11):

* The msgid text is category-C **chrome** (translatable). Jargon inside it — PvP,
  gatecamp, sov, ADM, moon extraction, killmail, FC, logi, doctrine — stays canonical
  English inside the msgid unless CCP publishes an official localisation (D16).
* Every ``{slot}`` is a **protected interpolation slot**: it must be a name from
  ``rendering.VARIABLE_CATALOGUE`` and it stays RAW — the interpolated EVE/game/user
  value is never translated (D14.7 / doc 08 §11.1). Translators keep the ``{slots}``
  verbatim (placeholder-parity gate).
* NEVER ``str.join``/concatenate the ``gettext_lazy`` proxies below; they are resolved
  with ``str(...)`` inside an active ``translation.override`` at render time
  (``rendering_i18n.render_for``), never before.

Populated incrementally as service call sites migrate from ``body=f"…"`` to a scaffold
key + ``context`` (phased, English-safe — doc 08 §16). Until a site migrates it keeps
``custom_message=True`` and delivers verbatim English, identical to today.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class Scaffold:
    """A translatable (subject, body) pair of gettext msgids with ``{slot}`` placeholders.

    ``subject`` may be empty when the alert has no distinct localised subject (the frozen
    ``Alert.title`` is used as the audit fallback).
    """

    subject: str  # a gettext msgid with {slot} placeholders (may be "")
    body: str     # a gettext msgid with {slot} placeholders


# Keyed by ``template_key`` (namespaced to the notification-event key it serves). Every
# {slot} MUST be a name from rendering.VARIABLE_CATALOGUE; the interpolated value stays raw.
SCAFFOLDS: dict[str, Scaffold] = {
    "operations.formup_reminder": Scaffold(
        subject=_("“{operation_name}” form-up"),
        body=_(
            "“{operation_name}” forms up in about {start_time} at {formup_system} · "
            "comms {link}. You signed up — see you there."
        ),
    ),
}


def scaffold(key: str) -> Scaffold | None:
    """The code scaffold registered for ``key``, or ``None``."""
    return SCAFFOLDS.get(key)
