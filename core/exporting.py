"""Helpers for generating downloadable exports safely."""
from __future__ import annotations

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
