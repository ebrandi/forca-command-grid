"""Ticket-source registry.

The single place that knows every source. ``SOURCES`` preserves display order
(PVP and manual first — the two always-on sources — then the PVE/economy sources).
Adding a source is: write the module, import its class here, add it to ``_ALL``.
"""
from __future__ import annotations

from .base import AUTOMATIC, MANUAL, SEMI_AUTO, SourceEvent, TicketSource
from .manual import BuybackSource, ManualSource, PlanetarySource, RattingSource
from .pve import (
    DirectiveSource,
    FleetSource,
    IndustrySource,
    LogisticsSource,
    MentorshipSource,
    MiningSource,
)
from .pvp import PvpSource

_ALL: list[TicketSource] = [
    PvpSource(),
    ManualSource(),
    MiningSource(),
    FleetSource(),
    LogisticsSource(),
    MentorshipSource(),
    IndustrySource(),
    PlanetarySource(),
    RattingSource(),
    BuybackSource(),
    DirectiveSource(),
]

SOURCES: dict[str, TicketSource] = {s.key: s for s in _ALL}

# Sources enabled by default when a contest is created from scratch (the reliable,
# always-on pair). Templates may enable more.
DEFAULT_ENABLED_KEYS = ("pvp", "manual")

__all__ = [
    "SOURCES", "DEFAULT_ENABLED_KEYS", "SourceEvent", "TicketSource",
    "AUTOMATIC", "SEMI_AUTO", "MANUAL",
    "get_source", "all_sources", "sweepable_sources",
]


def get_source(key: str) -> TicketSource | None:
    return SOURCES.get(key)


def all_sources() -> list[TicketSource]:
    return list(_ALL)


def sweepable_sources() -> list[TicketSource]:
    """Sources the automatic engine sweep should process (have real ``iter_events``)."""
    return [s for s in _ALL if not s.manual_only]
