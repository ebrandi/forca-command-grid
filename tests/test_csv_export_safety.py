"""CSV/formula-injection neutralisation for downloadable exports."""
from __future__ import annotations

from core.exporting import csv_safe, csv_safe_row


def test_formula_triggers_are_quoted():
    for payload in ("=1+1", "+1", "-1", "@SUM(A1)", "=cmd|'/c calc'!A1", "\ttab", "\rcr"):
        out = csv_safe(payload)
        assert out.startswith("'"), f"{payload!r} was not neutralised"
        assert out[1:] == payload


def test_benign_values_are_untouched():
    for payload in ("Pilot Name", "123", "0.5", "system:329791008", "", "a=b"):
        assert csv_safe(payload) == payload


def test_none_becomes_empty_string():
    assert csv_safe(None) == ""


def test_non_string_cells_are_stringified():
    assert csv_safe(42) == "42"
    assert csv_safe_row([1, "=EVIL()", None]) == ["1", "'=EVIL()", ""]


def test_a_hostile_character_name_cannot_smuggle_a_formula():
    # An EVE display name an attacker can influence, exported into a director's sheet.
    row = ["2026-01-01", '=HYPERLINK("http://evil","clickme")', 42, "ok"]
    safe = csv_safe_row(row)
    assert safe[1] == "'" + row[1]
    assert not safe[1][1:].startswith("'")  # exactly one quote added
