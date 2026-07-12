"""Static template-hygiene checks that rendered-page tests can miss."""
from __future__ import annotations

import glob
from pathlib import Path

from django.conf import settings


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
