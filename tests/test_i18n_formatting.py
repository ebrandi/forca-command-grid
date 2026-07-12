"""Formatting sweep (task 2f): human-facing Python ``strftime`` → ``formats.date_format``
and manual pluralisation → ``ngettext``.

Grounded in ``docs/i18n/design/09-formatting-spec.md`` §5.2 (dates emit English names
under ``strftime`` regardless of locale — ``date_format`` respects the active locale)
and §7.2 (manual English pluralisation → ``ngettext``); acceptance criteria F8 (chart
axis labels localise) and F13 (correct plural form per locale).

Every transform here is display-only: the English rendering must stay byte-identical to
the ``strftime`` it replaced, while a non-English locale localises month/day names. Tests
always use ``translation.override`` (never bare ``activate``) so the active locale is
restored, and need no DB (pure formatting + ``ngettext``).
"""
from __future__ import annotations

import datetime as dt

from django.utils import formats, translation
from django.utils.translation import ngettext

# Representative instants covering every field the sweep touches (%b %B %a %d %Y %H %M):
# a plain date (how truncated chart buckets arrive) and aware-UTC datetimes (how the app
# stores op/timer timestamps — UTC wall-clock, matching TIME_ZONE="UTC").
_DATE = dt.date(2026, 1, 5)                                    # chart week/day bucket
_DT = dt.datetime(2026, 1, 5, 14, 5, tzinfo=dt.UTC)  # notification body
_MONTH = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)      # month axis / hall-of-fame

# (Django format string used in the sweep, the strftime it replaced, the value).
_CASES = [
    ("M", "%b", _MONTH),                       # apps/pilots/services.py month axis
    ("d M", "%d %b", _DATE),                   # operations/readiness charts + report
    ("F Y", "%B %Y", _MONTH),                  # apps/pilots/halloffame.py labels
    ("D d M · H:i", "%a %d %b · %H:%M", _DT),  # operations notification/ping bodies
]


def test_date_format_byte_identical_to_strftime_under_english():
    """(a) Under ``en`` each converted call site renders byte-for-byte the same string
    ``date_format`` produces as the ``strftime`` it replaced — no visible English diff."""
    with translation.override("en"):
        for django_fmt, py_fmt, value in _CASES:
            assert formats.date_format(value, django_fmt) == value.strftime(py_fmt), (
                f"{django_fmt!r} must match strftime {py_fmt!r}"
            )


def test_full_month_name_localises_under_german():
    """(b) Under ``de`` a full month name localises and differs from English
    (Django's bundled locale name catalogue drives this — no project ``.mo`` needed)."""
    with translation.override("en"):
        english = formats.date_format(_MONTH, "F Y")
    with translation.override("de"):
        german = formats.date_format(_MONTH, "F Y")
    assert english == "March 2026"
    assert german == "März 2026"
    assert german != english


def test_abbrev_month_axis_localises_under_german():
    """The chart month axis uses the abbreviated ``"M"`` — it too localises under ``de``."""
    with translation.override("en"):
        assert formats.date_format(_MONTH, "M") == "Mar"
    with translation.override("de"):
        assert formats.date_format(_MONTH, "M") == "Mär"


def test_weekday_name_in_notification_body_localises_under_german():
    """The ``%a`` weekday in the operations timer/ping bodies (``"D"``) localises too,
    while the hour/minute (a machine wall-clock) stay unchanged across locales."""
    with translation.override("en"):
        assert formats.date_format(_DT, "D d M · H:i") == "Mon 05 Jan · 14:05"
    with translation.override("de"):
        assert formats.date_format(_DT, "D d M · H:i") == "Mo 05 Jan · 14:05"


def _buyback_row(n):
    """The exact ngettext message wired into apps/identity/views.py (dashboard row)."""
    return ngettext(
        "%(n)d buyback offer awaiting a buyer or payout",
        "%(n)d buyback offers awaiting a buyer or payout",
        n,
    ) % {"n": n}


def test_ngettext_singular_and_plural_english():
    """(c) An ngettext-converted message selects the correct English singular/plural for
    n=1 and n=2, with the count interpolated as a named placeholder."""
    with translation.override("en"):
        assert _buyback_row(1) == "1 buyback offer awaiting a buyer or payout"
        assert _buyback_row(2) == "2 buyback offers awaiting a buyer or payout"


def test_ngettext_reproduces_old_manual_plural_bytes():
    """Guard: under ``en`` ngettext reproduces exactly what the replaced
    ``f"{n} offer{'s' if n != 1 else ''}"`` manual pluralisation emitted, incl. n=0."""
    def old(n):
        return f"{n} buyback offer{'s' if n != 1 else ''} awaiting a buyer or payout"

    with translation.override("en"):
        for n in (0, 1, 2, 5):
            assert _buyback_row(n) == old(n)
