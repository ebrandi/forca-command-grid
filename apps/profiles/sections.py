"""The catalogue of what a public profile CAN show — and, by omission, what it never will.

Each entry is something a pilot may choose to expose on their own unlisted page. The registry
is deliberately short, and the reasoning for what is NOT here matters as much as what is:

* **Never shareable — corporation operational security.** Doctrine readiness, doctrine
  training plans (their names are ``Fly <doctrine>``), corp readiness scores, directives,
  operations, Capsuleer Path goals, contributions, Hall-of-Fame standing and trophies all
  describe the corporation's composition, priorities and internal metrics. A pilot may own the
  facts about themselves, but not the corp's context around them.
* **Never shareable — personal safety.** Assets, fitted ships, wallet, location, jump clones
  and the account's OTHER linked characters (an alt↔main link is exactly what hostile intel
  wants; every profile is one character and one character only — LP-3).
* **Shareable, on by default.** Trained skills: the whole point of the feature.
* **Shareable, opt-in.** The training queue (what you are working towards), the hulls your
  skills already let you sit in (derived from the SDE, not from assets), attributes + implants
  (flagged: implants say what is in your head), and the combat record (killmails are public EVE
  facts already visible on any killboard, so this only summarises what the world can see).

Labels and descriptions are lazy so the management page renders them in the reader's language.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    description: str
    default: bool = False
    # A caution shown beside the checkbox for sections with a privacy cost worth spelling out.
    caution: str = ""
    # A core.features key the section depends on; the section is hidden (and stripped from a
    # saved profile at render time) while that feature is off.
    feature: str = ""


SECTIONS: list[Section] = [
    Section(
        "skills",
        _("Trained skills"),
        _("Total skill points and every trained skill, grouped by category, with its level."),
        default=True,
    ),
    Section(
        "queue",
        _("Training queue"),
        _("What is training right now and what is queued after it, with finish times."),
    ),
    Section(
        "ships",
        _("Ships you can fly"),
        _("Every hull whose skill requirements you already meet, worked out from your trained "
          "skills — not from what you own."),
    ),
    Section(
        "attributes",
        _("Attributes & implants"),
        _("Your five attributes and the implants plugged into this clone."),
        caution=_("Implants reveal what is in your head. Share with care."),
    ),
    Section(
        "combat",
        _("Combat record"),
        _("All-time kills, losses, ISK destroyed, rank and your most recent kills from the "
          "corp killboard. Killmails are public EVE facts."),
        feature="killboard",
    ),
]

SECTIONS_BY_KEY: dict[str, Section] = {s.key: s for s in SECTIONS}
DEFAULT_SECTIONS: list[str] = [s.key for s in SECTIONS if s.default]


def available_sections() -> list[Section]:
    """The sections a pilot may pick right now (feature-dependent ones only while that
    feature is on)."""
    from core.features import feature_enabled

    return [s for s in SECTIONS if not s.feature or feature_enabled(s.feature)]


def clean_sections(keys) -> list[str]:
    """Normalise a submitted list of section keys: known keys only, registry order, no
    duplicates. Feature-dependent sections are kept (the render pass hides them while the
    feature is off, and shows them again when it returns)."""
    wanted = {str(k) for k in (keys or [])}
    return [s.key for s in SECTIONS if s.key in wanted]


def renderable_sections(keys) -> list[str]:
    """The saved keys that may actually render today (drops feature-off sections)."""
    allowed = {s.key for s in available_sections()}
    return [k for k in clean_sections(keys) if k in allowed]
