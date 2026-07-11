"""The shared Capsuleer Path activity taxonomy (brief §5).

One vocabulary, one module: profile preferences, career templates, goals and suggestion
matching all draw their activity values from :class:`Activity` here, and the model fields
generate their ``choices`` from it at import time. Keeping the vocabulary in a single place
is what lets the wizard, the catalogue filters and the suggestion rules agree on what a
"logistics" or "exploration" pilot means without three parallel enumerations drifting apart.

Where an activity maps onto an existing corp concept, the mapping lives here too — currently
:data:`ACTIVITY_TO_MENTORSHIP_CATEGORY`, which relates a career activity to the nearest
``mentorship.MentorshipTrack.Category`` so the mentor-matching suggestion rule (Phase 3) can
find mentors by track without inventing a second category vocabulary (synthesis §2). The map
is intentionally best-effort and total over :class:`Activity`; unmapped nuance degrades to
``OTHER`` rather than guessing.
"""
from __future__ import annotations

from django.db import models

from apps.mentorship.models import MentorshipTrack


class Activity(models.TextChoices):
    """Career activities a pilot can prefer, avoid, template or set a goal toward."""

    COMBAT_LINE = "combat_line", "Combat line (DPS)"
    COMBAT_SUPPORT = "combat_support", "Combat support (logi/ewar)"
    TACKLE_SCOUT = "tackle_scout", "Tackle & scouting"
    FLEET_COMMAND = "fleet_command", "Fleet command"
    BLACK_OPS = "black_ops", "Black ops"
    CAPITALS = "capitals", "Capitals"
    MINING = "mining", "Mining"
    INDUSTRY = "industry", "Industry"
    PLANETARY = "planetary", "Planetary industry"
    HAULING = "hauling", "Hauling"
    EXPLORATION = "exploration", "Exploration"
    WORMHOLES = "wormholes", "Wormholes"
    TRADE = "trade", "Trade"
    MENTORING = "mentoring", "Mentoring"
    CORP_SERVICE = "corp_service", "Corp service"


# Every taxonomy value, for membership checks in form/service validation.
ACTIVITY_VALUES = frozenset(Activity.values)

# Career activity → nearest mentorship track category (brief §5). Total over Activity; a
# nuance with no faithful track (trade, black ops, capitals, corp service) maps to OTHER so
# mentor matching never silently invents a category. Phase 3 may widen MentorshipTrack.Category
# rather than growing a parallel vocabulary here.
_C = MentorshipTrack.Category
ACTIVITY_TO_MENTORSHIP_CATEGORY: dict[str, str] = {
    Activity.COMBAT_LINE: _C.PVP,
    Activity.COMBAT_SUPPORT: _C.LOGISTICS,
    Activity.TACKLE_SCOUT: _C.PVP,
    Activity.FLEET_COMMAND: _C.FLEET,
    Activity.BLACK_OPS: _C.PVP,
    Activity.CAPITALS: _C.PVP,
    Activity.MINING: _C.MINING,
    Activity.INDUSTRY: _C.INDUSTRY,
    Activity.PLANETARY: _C.INDUSTRY,
    Activity.HAULING: _C.LOGISTICS,
    Activity.EXPLORATION: _C.EXPLORATION,
    Activity.WORMHOLES: _C.EXPLORATION,
    Activity.TRADE: _C.OTHER,
    Activity.MENTORING: _C.OTHER,
    Activity.CORP_SERVICE: _C.OTHER,
}


def mentorship_category_for(activity: str) -> str:
    """The mentorship track category nearest to ``activity`` (``OTHER`` when unknown)."""
    return ACTIVITY_TO_MENTORSHIP_CATEGORY.get(activity, _C.OTHER)


# Career activity → the operation types that serve it (Stage 3 event_match, doc 08 §5.2). Values
# are ``operations.Operation.Type`` strings; an activity with no fleet expression (solo/industrial
# lines) maps to an empty set so event_match simply produces nothing for it. Kept as plain strings
# (not the enum) so this module never imports the operations app.
ACTIVITY_TO_OPERATION_TYPES: dict[str, frozenset[str]] = {
    Activity.COMBAT_LINE: frozenset({"pvp", "roam", "home_defence", "deployment", "war_prep",
                                     "doctrine_rollout"}),
    Activity.COMBAT_SUPPORT: frozenset({"pvp", "roam", "home_defence", "deployment", "logistics"}),
    Activity.TACKLE_SCOUT: frozenset({"pvp", "roam", "gatecamp", "home_defence"}),
    Activity.FLEET_COMMAND: frozenset({"pvp", "roam", "home_defence", "deployment"}),
    Activity.BLACK_OPS: frozenset({"pvp", "roam", "deployment"}),
    Activity.CAPITALS: frozenset({"pvp", "home_defence", "deployment", "structure_timer"}),
    Activity.MINING: frozenset({"mining"}),
    Activity.INDUSTRY: frozenset({"industrial"}),
    Activity.PLANETARY: frozenset({"industrial"}),
    Activity.HAULING: frozenset({"logistics"}),
    Activity.EXPLORATION: frozenset(),
    Activity.WORMHOLES: frozenset(),
    Activity.TRADE: frozenset(),
    Activity.MENTORING: frozenset(),
    Activity.CORP_SERVICE: frozenset(),
}

# Career activity → the campaign categories whose help-wanted objectives it can serve (Stage 3
# campaign_opportunity, doc 08 §5.7). Values are ``campaigns.Campaign.Category`` strings.
ACTIVITY_TO_CAMPAIGN_CATEGORIES: dict[str, frozenset[str]] = {
    Activity.COMBAT_LINE: frozenset({"doctrine_rollout", "deployment", "defence_readiness",
                                     "training", "coverage"}),
    Activity.COMBAT_SUPPORT: frozenset({"doctrine_rollout", "deployment", "defence_readiness",
                                        "logistics", "training"}),
    Activity.TACKLE_SCOUT: frozenset({"deployment", "defence_readiness", "coverage"}),
    Activity.FLEET_COMMAND: frozenset({"training", "deployment", "coverage"}),
    Activity.BLACK_OPS: frozenset({"deployment", "coverage"}),
    Activity.CAPITALS: frozenset({"deployment", "defence_readiness"}),
    Activity.MINING: frozenset({"industry", "stockpile"}),
    Activity.INDUSTRY: frozenset({"industry", "stockpile"}),
    Activity.PLANETARY: frozenset({"industry"}),
    Activity.HAULING: frozenset({"logistics", "stockpile", "relocation"}),
    Activity.EXPLORATION: frozenset({"coverage"}),
    Activity.WORMHOLES: frozenset({"coverage"}),
    Activity.TRADE: frozenset({"stockpile"}),
    Activity.MENTORING: frozenset({"membership", "training"}),
    Activity.CORP_SERVICE: frozenset({"membership", "srp_reserve"}),
}


def operation_types_for(activity: str) -> frozenset[str]:
    """Operation types that serve ``activity`` (empty when it has no fleet expression)."""
    return ACTIVITY_TO_OPERATION_TYPES.get(activity, frozenset())


def campaign_categories_for(activity: str) -> frozenset[str]:
    """Campaign categories whose help-wanted work ``activity`` can serve."""
    return ACTIVITY_TO_CAMPAIGN_CATEGORIES.get(activity, frozenset())
