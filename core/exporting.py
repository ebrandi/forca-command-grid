"""Helpers for generating downloadable exports safely."""
from __future__ import annotations

import csv
from decimal import Decimal

# A cell whose text begins with one of these is interpreted as a formula by Excel,
# LibreOffice and Google Sheets when the CSV is opened. If that text is
# attacker-influenced — an EVE character name, a free-text reason — the formula runs
# in the spreadsheet of whoever opens the export (typically a director). Prefixing a
# single quote forces the cell to render as literal text.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value) -> str:
    """Neutralise CSV/formula injection in one cell value.

    Stringifies ``value`` and, if it begins with a spreadsheet formula trigger,
    prefixes a single quote so the cell is rendered as text rather than executed.
    """
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in _FORMULA_TRIGGERS else s


def csv_safe_row(row) -> list[str]:
    """Apply :func:`csv_safe` to every cell in a row."""
    return [csv_safe(cell) for cell in row]


def _safe_cell(value):
    """Like :func:`csv_safe`, but leaves genuine numbers alone.

    A real ``int``/``float``/``Decimal`` cannot carry a formula, and quoting one would be
    actively wrong: ``-5`` would become the *text* ``'-5`` in the spreadsheet, silently
    breaking every numeric column that can go negative (surplus, remaining slots, deltas).
    ``csv_safe`` keeps stringifying numbers because its callers and tests rely on that;
    this is the variant the writer below uses, where preserving column types matters.
    ``bool`` is deliberately handled by the number branch — it is an ``int`` subclass and
    ``True``/``False`` are not formula triggers either way.
    """
    if isinstance(value, int | float | Decimal):
        return value
    return csv_safe(value)


class SafeCsvWriter:
    """A ``csv.writer`` that neutralises formula injection on the way out.

    Formula-injection protection kept regressing here: ``csv_safe_row`` has to be
    remembered at every ``writerow`` call site, and five exports added after the 2026-07-15
    audit each forgot it. Making the *writer* responsible removes the thing that was being
    forgotten — a call site now has to opt OUT to be unsafe, instead of opting in to be safe.
    """

    def __init__(self, fileobj, **kwargs):
        self._writer = csv.writer(fileobj, **kwargs)

    def writerow(self, row):
        return self._writer.writerow([_safe_cell(cell) for cell in row])

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)


def safe_csv_writer(fileobj, **kwargs) -> SafeCsvWriter:
    """Return a :class:`SafeCsvWriter` over ``fileobj`` — a drop-in for ``csv.writer``."""
    return SafeCsvWriter(fileobj, **kwargs)
