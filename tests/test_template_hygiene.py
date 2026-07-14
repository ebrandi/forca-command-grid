"""Static template-hygiene checks that rendered-page tests can miss."""
from __future__ import annotations

import glob
import re
from pathlib import Path

from django.conf import settings

TRANS_TAG = re.compile(r"{%\s*(?:translate|trans|blocktranslate|blocktrans)\b")
LOAD_I18N = re.compile(r"{%\s*load\b[^%]*\bi18n\b")


def test_no_multiline_django_comments():
    """Django ``{# #}`` comments are single-line ONLY — a multi-line one renders as
    literal text and leaks onto the page. Rendered-page tests miss comments inside
    conditionally-rendered partials (e.g. the capsuleer/campaigns dashboard panels),
    so scan every template statically. Use ``{% comment %}…{% endcomment %}`` instead.
    """
    root = Path(settings.BASE_DIR) / "templates"
    offenders = []
    for path in glob.glob(str(root / "**" / "*.html"), recursive=True):
        for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
            if "{#" in line and "#}" not in line:
                rel = Path(path).relative_to(settings.BASE_DIR)
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "Multi-line {# #} comments leak as literal text; use {% comment %}: "
        + ", ".join(offenders)
    )


def test_every_template_using_a_trans_tag_loads_i18n():
    """``{% load i18n %}`` does NOT inherit across ``{% extends %}`` — it is per-file and
    resolved at compile time. A template that uses ``{% translate %}`` without its own load
    tag raises TemplateSyntaxError, i.e. **HTTP 500**, not a silent English fallback.

    Much of the admin console is Director-only, so a page missing the tag can sit unnoticed
    until a director opens it in production. This is the cheap static gate for that whole
    class of bug.
    """
    root = Path(settings.BASE_DIR) / "templates"
    offenders = []
    for path in glob.glob(str(root / "**" / "*.html"), recursive=True):
        text = Path(path).read_text(encoding="utf-8")
        if TRANS_TAG.search(text) and not LOAD_I18N.search(text):
            offenders.append(str(Path(path).relative_to(settings.BASE_DIR)))
    assert not offenders, (
        "Template uses a translate tag but never loads i18n — this is a guaranteed "
        "TemplateSyntaxError (HTTP 500) when the page renders: " + ", ".join(sorted(offenders))
    )
