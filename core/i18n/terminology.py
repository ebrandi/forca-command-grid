"""EVE terminology linter — enforces the protected-terms policy over catalogues.

Loads the committed rule data at ``core/i18n/data/protected-terms.yml`` (schema
documented in that file; decision D16) and checks that protected EVE jargon /
game-data names are preserved in a translation unless a governed per-locale
exception exists. The rule data lives in the package (version-controlled) rather
than under ``docs/`` so the linter runs in CI; the human glossary + governance
policy remain under ``docs/i18n/`` (not version-controlled).

Deliberately dependency-light: a tiny built-in ``.po`` reader means the linter (and
its test) run without ``polib`` or a ``gettext`` binary installed. ``polib`` remains
the documented tool for the wider catalogue lifecycle.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_GLOSSARY = Path(__file__).resolve().parent / "data" / "protected-terms.yml"


@lru_cache(maxsize=8)
def load_terms(path: str | None = None) -> dict:
    import yaml  # provided transitively by drf-spectacular

    with open(Path(path) if path else _GLOSSARY, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=2048)
def _rx(term: str, case_sensitive: bool) -> re.Pattern:
    # Unicode word-boundary, phrase-aware (so "cyno" ∉ "cynosural", "logi" ∉ "logistics").
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", flags)


def _b_terms(data: dict):
    for entry in data.get("category_b", {}).get("terms", []) or []:
        term = entry["term"]
        cs = bool(entry.get("case_sensitive", False))
        forms = [term, *(entry.get("aliases") or [])]
        yield entry, term, cs, forms


def check_pair(msgid: str, msgstr: str, locale: str, data: dict | None = None) -> list[dict]:
    """Violations for one ``(msgid, msgstr)`` under ``locale``.

    An empty ``msgstr`` (untranslated → English fallback) never violates. Category
    B: a protected term present in the msgid must be preserved in the msgstr unless
    a governed per-locale exception is recorded (``require: true`` makes the approved
    value mandatory). Category A: a sample game-data name in the msgid must appear
    verbatim in the msgstr (no ``.po`` exceptions — official names come from CCP data
    at the SDE seam, never a catalogue).
    """
    data = data or load_terms()
    if not msgstr or not msgstr.strip():
        return []

    out: list[dict] = []

    for entry, term, cs, forms in _b_terms(data):
        if not any(_rx(f, cs).search(msgid) for f in forms):
            continue
        preserved = any(_rx(f, cs).search(msgstr) for f in forms)
        exc = (entry.get("exceptions") or {}).get(locale)
        if exc and exc.get("value"):
            approved = _rx(exc["value"], False).search(msgstr) is not None
            ok = approved if exc.get("require") else (preserved or approved)
        else:
            ok = preserved
        if not ok:
            out.append({"category": "B", "term": term, "locale": locale})

    for term in data.get("category_a", {}).get("sample_terms", []) or []:
        if _rx(term, True).search(msgid) and not _rx(term, True).search(msgstr):
            out.append({"category": "A", "term": term, "locale": locale})

    return out


def _read_po(path: Path):
    """Yield ``(msgid, msgstr)`` from a ``.po`` file (minimal; header row dropped)."""
    pairs: list[tuple[str, str]] = []
    msgid: str | None = None
    msgstr: str | None = None
    field: str | None = None

    def unquote(s: str) -> str:
        i, j = s.find('"'), s.rfind('"')
        return s[i + 1 : j] if i != -1 and j > i else ""

    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if s.startswith("msgid "):
            if msgid is not None:
                pairs.append((msgid, msgstr or ""))
            msgid, msgstr, field = unquote(s[6:]), None, "id"
        elif s.startswith("msgstr "):
            msgstr, field = unquote(s[7:]), "str"
        elif s.startswith('"'):
            if field == "id":
                msgid = (msgid or "") + unquote(s)
            elif field == "str":
                msgstr = (msgstr or "") + unquote(s)
        elif not s or s.startswith("#"):
            field = None
    if msgid is not None:
        pairs.append((msgid, msgstr or ""))
    return [(mid, mstr) for mid, mstr in pairs if mid]


def scan_po(path, locale: str, data: dict | None = None) -> list[dict]:
    """Lint every entry of a ``.po`` file; each violation carries its ``msgid``.

    Uses ``polib`` when importable — it parses plural forms (``msgstr[0]``,
    ``msgstr[1]``, …) and multi-line entries correctly — and falls back to the
    dependency-light built-in reader otherwise. Violations are de-duplicated per
    (msgid, term, category) so a multi-form plural is not counted N times.
    """
    data = data or load_terms()
    out: list[dict] = []
    seen: set = set()

    def _emit(msgid: str, msgstr: str) -> None:
        for v in check_pair(msgid, msgstr, locale, data):
            key = (msgid, v["term"], v["category"])
            if key in seen:
                continue
            seen.add(key)
            v["msgid"] = msgid
            out.append(v)

    try:
        import polib

        po = polib.pofile(str(path))
        for e in po:
            if not e.msgid:
                continue
            if e.msgstr:
                _emit(e.msgid, e.msgstr)
            for form in (e.msgstr_plural or {}).values():
                if form:
                    _emit(e.msgid, form)
        return out
    except ImportError:
        for msgid, msgstr in _read_po(Path(path)):
            _emit(msgid, msgstr)
        return out
