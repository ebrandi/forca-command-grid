"""Manual grant source + declared-but-manual extension-point sources.

``ManualSource`` is the leadership recognition channel — tickets are created
directly by :func:`apps.raffle.services.grant_manual_tickets`, never swept. The
extension-point sources (PI, ratting, buyback) are declared so leaders can enable
them and grant by hand today, and so a future reliable integration can drop in an
``iter_events`` without any other change.
"""
from __future__ import annotations

from .base import MANUAL, TicketSource


class ManualSource(TicketSource):
    key = "manual"
    label = "Manual grants"
    description = ("Leadership grants for valuable but hard-to-measure work — FC, scouting, "
                   "logistics, doctrine prep, helping newbros, diplomacy, emergency response.")
    unit = "grants"
    reliability = MANUAL
    default_mode = "manual"
    manual_only = True


class PlanetarySource(TicketSource):
    key = "pi"
    label = "Planetary Industry"
    description = ("Tickets for PI contribution campaigns. Awarded by officer approval / manual "
                   "grant until a reliable per-pilot PI feed exists.")
    unit = "PI"
    reliability = MANUAL
    default_mode = "officer_approved"
    manual_only = True


class RattingSource(TicketSource):
    key = "ratting"
    label = "Ratting / PVE combat"
    description = ("Tickets for taxable bounty contribution. No reliable per-pilot bounty feed "
                   "exists, so awards are officer-approved / manual (e.g. X tickets per Y ISK taxed).")
    unit = "ISK"
    reliability = MANUAL
    default_mode = "officer_approved"
    manual_only = True


class BuybackSource(TicketSource):
    key = "buyback"
    label = "Buyback / market / economy"
    description = ("Tickets for buyback, market seeding, doctrine production and stockpile "
                   "contribution. Officer-approved / manual until verifiable.")
    unit = "ISK"
    reliability = MANUAL
    default_mode = "officer_approved"
    manual_only = True
