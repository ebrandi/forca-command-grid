"""Render-time i18n seam for the colony-issue codes (Seam A / code→label map).

``PiColony.summary['issues']`` is a JSON list persisted by the ESI import
(:func:`apps.planetary.esi._classify_pins` / :func:`~apps.planetary.esi.import_colonies`).
It must hold **stable CODES**, never prose: a ``gettext_lazy`` proxy cannot live inside a
``JSONField`` (it is not serialisable and raises at save time), and a plain English
sentence frozen into the column would render English to every locale forever — and would
also drift the alert signature every time the wording changed.

So the column stores CODES (``"extractor_expired"``, ``"factory_no_schematic"``,
``"no_routes"``) and the human, translatable label is resolved here, at *render* time,
keyed on the code. This mirrors :mod:`apps.planetary.labels` (the complexity/confidence
code→label map) exactly.

An unknown code — a value written by an older import, or a raw string handed in by a test —
is returned verbatim, so this can never blank an issue or crash on legacy data.
"""
from __future__ import annotations

from django.utils.translation import gettext_lazy as _

# Stable colony-issue codes emitted into ``PiColony.summary['issues']``.
EXTRACTOR_EXPIRED = "extractor_expired"
FACTORY_NO_SCHEMATIC = "factory_no_schematic"
NO_ROUTES = "no_routes"

# {stable code -> translatable label}. ``gettext_lazy`` marks the English for xgettext
# extraction and is only ever *evaluated* at render time (never stored) — no locale is
# frozen into the persisted JSON.
ISSUE_LABELS: dict[str, str] = {
    EXTRACTOR_EXPIRED: _("An extractor program has expired — restart it to keep pulling P0."),
    FACTORY_NO_SCHEMATIC: _("A factory has no schematic set — it isn't producing anything."),
    NO_ROUTES: _("No routes configured — nothing will flow between facilities."),
}


def issue_label(code: str) -> str:
    """The human, translated label for a colony-issue ``code`` (verbatim if unknown).

    Returns a plain ``str`` (the lazy proxy is evaluated under the active locale here), so
    callers may safely ``"; ".join(...)`` the result or hand it straight to a template.
    """
    label = ISSUE_LABELS.get(code or "")
    return str(label) if label is not None else (code or "")


def issue_labels(codes) -> list[str]:
    """Resolve a sequence of colony-issue codes to translated labels (order preserved)."""
    return [issue_label(c) for c in (codes or [])]
