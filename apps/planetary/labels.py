"""Display labels for the PI code values (complexity, confidence).

``complexity`` and ``confidence`` are CODES, not prose. ``recommend`` *compares* the
complexity code (``item["complexity"] == "Low"`` is what earns a plan the "Beginner
recommended" badge), and the calculator persists both codes verbatim into
``PiPlan.snapshot`` (a JSONField). Translating the value itself would therefore do two
silent kinds of damage: the badge would stop firing for every non-English pilot, and a
localised string would be frozen into the snapshot of whoever recomputed it last.

So the code stays canonical English, forever, and the *label* — resolved at render time,
never persisted — is the translated half. Views attach ``complexity_label`` /
``confidence_label`` beside the code; templates render the label and keep comparing the
code.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

COMPLEXITY_LABELS: dict[str, str] = {
    "Low": _("Low"),
    "Medium": _("Medium"),
    "High": _("High"),
}

CONFIDENCE_LABELS: dict[str, str] = {
    "Low": _("Low"),
    "Medium": _("Medium"),
    "High": _("High"),
}


def complexity_label(code: str):
    """The human label for a complexity code (the code itself if unmapped, e.g. ``"—"``)."""
    return COMPLEXITY_LABELS.get(code, code)


def confidence_label(code: str):
    """The human label for a confidence code (the code itself if unmapped, e.g. ``"—"``)."""
    return CONFIDENCE_LABELS.get(code, code)
