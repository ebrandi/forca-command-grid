"""Ticket-source framework — the pluggable contract every source implements.

A source's ONLY job is to turn raw activity in a time window into a stream of
:class:`SourceEvent` candidates (who did what, when, worth how many base tickets).
Everything downstream — eligibility, enrolment/ESI gating, idempotency, caps,
booster multipliers, ledger writes and ineligible-activity recording — is handled
once, centrally, by :mod:`apps.raffle.engine`. This keeps sources tiny and makes
adding a new one (mining, fleet, a future integration) a self-contained module,
per the spec's "extensible Ticket Source Engine with separate source modules".
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Reliability of a source's automatic detection — surfaced in the admin so leaders
# know what they're turning on, and honest about where no reliable per-pilot data
# exists yet (those sources ship as officer-approved / manual, never faked).
AUTOMATIC = "automatic"      # trustworthy per-pilot data straight from ESI-derived tables
SEMI_AUTO = "semi_auto"      # per-pilot data exists but wants an officer sign-off
MANUAL = "manual"            # no reliable per-pilot signal — leaders grant by hand


@dataclass
class SourceEvent:
    """One candidate ticket award, before eligibility/caps are applied."""

    character_id: int
    source_ref: str                 # stable id for idempotency, e.g. "killmail:123"
    base_tickets: int               # tickets before booster/cap
    occurred_at: object             # datetime the activity happened
    magnitude: float = 0.0          # size (ISK, m³, units…) — for min_threshold + stats
    character_name: str = ""
    reason: str = ""                # human calc reason, e.g. "Solo kill (100)"
    metadata: dict = field(default_factory=dict)


class TicketSource:
    """Base class for a ticket source. Subclasses set the identity attributes and
    implement :meth:`iter_events`. Stateless — one shared instance per key."""

    key: str = ""
    label: str = ""
    description: str = ""
    unit: str = "event"                 # what magnitude counts (kills, m³, ISK…)
    reliability: str = MANUAL
    default_mode: str = "manual"        # RaffleTicketSourceConfig.Mode value
    default_config: dict = {}           # seeded into RaffleTicketSourceConfig.config
    default_filters: dict = {}
    # True for sources whose tickets are created directly (manual grants), so the
    # engine's automatic sweep skips them.
    manual_only: bool = False

    def iter_events(self, contest, config, since, until):
        """Yield :class:`SourceEvent`s for eligible activity in ``[since, until]``.

        ``config`` is the contest's :class:`RaffleTicketSourceConfig` row. Return
        an empty iterator for a source with no reliable automatic signal (its
        tickets then come only via officer approval / manual grants).
        """
        return iter(())

    # --- helpers shared by concrete sources ------------------------------- #
    def cfg(self, config, key, default):
        """Read a value from the source config JSON with a fallback to the default."""
        return (config.config or {}).get(key, self.default_config.get(key, default))

    def filt(self, config, key, default=None):
        return (config.filters or {}).get(key, default)
