"""EVE terminology linter — protected terms must survive translation (D16).

Exercises the linter logic directly (no compiled catalogues needed). Once real
catalogues exist, CI runs terminology.scan_po over every locale/<lang>/*.po.
"""
from __future__ import annotations

import glob
from pathlib import Path

import pytest
from django.conf import settings

from core.i18n import terminology

# Committed rule data (version-controlled, so the linter is enforceable in CI).
GLOSSARY = Path(settings.BASE_DIR) / "core" / "i18n" / "data" / "protected-terms.yml"


def test_glossary_loads_and_has_expected_terms():
    data = terminology.load_terms(str(GLOSSARY))
    assert data.get("version") == 1
    b_terms = {t["term"] for t in data["category_b"]["terms"]}
    assert {"FC", "cyno", "logi", "killmail", "SRP", "doctrine"} <= b_terms
    assert "Rifter" in data["category_a"]["sample_terms"]


def test_preserved_jargon_passes():
    assert terminology.check_pair("You are the FC", "Vous êtes le FC", "fr") == []
    assert terminology.check_pair("Submit an SRP claim", "Envoyer une demande SRP", "fr") == []


def test_dropped_jargon_is_flagged():
    viol = terminology.check_pair("You are the FC", "Vous êtes le commandant", "fr")
    assert any(v["term"] == "FC" and v["category"] == "B" for v in viol)


def test_empty_msgstr_never_violates():
    # Untranslated → English fallback → not a violation.
    assert terminology.check_pair("You are the FC", "", "fr") == []


def test_category_a_game_name_must_be_verbatim():
    bad = terminology.check_pair("Fly the Rifter", "Volez le Chasseur", "fr")
    assert any(v["category"] == "A" and v["term"] == "Rifter" for v in bad)
    assert terminology.check_pair("Fly the Rifter", "Volez le Rifter", "fr") == []


def test_case_sensitive_abbreviation():
    # "FC" is case-sensitive; a lowercase "fc" in the source is not the protected token.
    assert terminology.check_pair("the fabric", "le tissu", "fr") == []


def test_committed_catalogues_have_no_protected_term_violations():
    """Regression gate: every shipped .po must preserve protected EVE terms.

    This is the enforcement arm of the terminology policy (D16) — a machine or human
    translation that renders a protected term (FC, killmail, doctrine, a ship name…)
    into the target language fails here. Skips if no catalogues are present.
    """
    pos = sorted(glob.glob(str(Path(settings.BASE_DIR) / "locale" / "*" / "LC_MESSAGES" / "django.po")))
    if not pos:
        pytest.skip("no locale catalogues in this checkout")
    violations = []
    for p in pos:
        loc = Path(p).parent.parent.name
        code = {"pt_BR": "pt-br", "zh_Hans": "zh-hans"}.get(loc, loc)
        violations.extend(terminology.scan_po(p, code))
    assert not violations, (
        f"{len(violations)} protected-term violation(s); first few: "
        + "; ".join(f"[{v['locale']}] {v['term']!r} in {v['msgid'][:40]!r}" for v in violations[:5])
    )


def test_scan_po_reads_and_flags(tmp_path):
    po = tmp_path / "django.po"
    po.write_text(
        'msgid ""\nmsgstr ""\n\n'
        'msgid "You are the FC"\nmsgstr "Vous êtes le commandant"\n\n'
        'msgid "Save"\nmsgstr "Enregistrer"\n',
        encoding="utf-8",
    )
    viol = terminology.scan_po(po, "fr")
    assert any(v["term"] == "FC" and v.get("msgid") == "You are the FC" for v in viol)
    assert not any(v.get("msgid") == "Save" for v in viol)
