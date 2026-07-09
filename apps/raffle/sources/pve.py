"""PVE / corporation-economy ticket sources.

Each module turns an existing per-pilot activity table into ticket events. We only
*automate* sources that carry trustworthy per-pilot data (mining ledger, confirmed
fleet attendance); logistics and mentorship ship as **officer-approved** semi-auto
(the data is per-pilot but wants a human sign-off before it earns); industry ships
as officer-approved with no automatic detection, because there is no reliable
per-pilot completed-job signal — no fake automation.
"""
from __future__ import annotations

from datetime import datetime, time
from functools import lru_cache

from django.utils import timezone

from .base import AUTOMATIC, MANUAL, SEMI_AUTO, SourceEvent, TicketSource


@lru_cache(maxsize=8192)
def _type_volume(type_id: int) -> float:
    """Assembled m³ of one unit of a type (0.0 if SDE lacks it)."""
    from apps.sde.models import SdeType

    v = SdeType.objects.filter(type_id=type_id).values_list("volume", flat=True).first()
    return float(v or 0.0)


class MiningSource(TicketSource):
    key = "mining"
    label = "Mining"
    description = "Tickets for ore mined, from the corp mining ledger (X tickets per m³)."
    unit = "m³"
    reliability = AUTOMATIC
    default_mode = "auto"
    # basis: "m3" (uses SDE volume) or "units" (raw quantity). per_ticket is the
    # amount of the basis that earns one ticket.
    default_config = {"basis": "m3", "per_ticket": 50000}

    def iter_events(self, contest, config, since, until):
        from apps.mining.models import MiningLedgerEntry

        cfg = config.config or {}
        basis = cfg.get("basis", "m3")
        per_ticket = float(cfg.get("per_ticket", 50000)) or 1.0
        # Only fully-elapsed days are final and safe to award idempotently.
        today = timezone.now().date()
        rows = (
            MiningLedgerEntry.objects.filter(day__gte=since.date(), day__lt=until.date())
            .filter(day__lt=today)
            .values("character_id", "day", "type_id", "quantity")
        )
        # Aggregate per (character, day) so a pilot earns once per mining day.
        by_day: dict[tuple[int, object], float] = {}
        for r in rows:
            cid = r["character_id"]
            if not cid:
                continue
            qty = r["quantity"] or 0
            magnitude = qty * _type_volume(r["type_id"]) if basis == "m3" else qty
            by_day[(cid, r["day"])] = by_day.get((cid, r["day"]), 0.0) + magnitude
        for (cid, day), magnitude in by_day.items():
            tickets = int(magnitude // per_ticket)
            if tickets <= 0:
                continue
            occurred = timezone.make_aware(datetime.combine(day, time(12, 0)))
            unit = "m³" if basis == "m3" else "units"
            yield SourceEvent(
                character_id=cid,
                source_ref=f"mining:{cid}:{day.isoformat()}",
                base_tickets=tickets,
                occurred_at=occurred,
                magnitude=magnitude,
                reason=f"Mined {magnitude:,.0f} {unit} ({tickets} tickets)",
                metadata={"day": day.isoformat(), "basis": basis, unit: round(magnitude, 1)},
            )


class FleetSource(TicketSource):
    key = "fleet"
    label = "Fleet participation"
    description = "Tickets for confirmed attendance (PAP) on scheduled operations."
    unit = "ops"
    reliability = AUTOMATIC
    default_mode = "auto"
    default_config = {"per_op": 5}

    def iter_events(self, contest, config, since, until):
        from apps.operations.models import OperationAttendance

        per_op = int((config.config or {}).get("per_op", 5))
        rows = (
            OperationAttendance.objects.filter(
                confirmed=True,
                operation__target_at__gte=since,
                operation__target_at__lt=until,
            )
            .select_related("operation", "user")
        )
        for att in rows.iterator(chunk_size=500):
            cid = att.character_id or getattr(att.user, "main_character_id", None)
            if not cid:
                continue
            yield SourceEvent(
                character_id=cid,
                source_ref=f"attendance:{att.id}",
                base_tickets=per_op,
                occurred_at=att.operation.target_at,
                magnitude=1,
                character_name=att.character_name,
                reason=f"Fleet attendance: {att.operation.title[:60]} ({per_op})",
                metadata={"operation_id": att.operation_id},
            )


class LogisticsSource(TicketSource):
    key = "logistics"
    label = "Logistics / hauling"
    description = "Tickets for completed courier contracts delivered by a pilot."
    unit = "deliveries"
    reliability = SEMI_AUTO
    default_mode = "officer_approved"
    default_config = {"per_delivery": 5}

    def iter_events(self, contest, config, since, until):
        from apps.logistics.models import CourierContract

        per_delivery = int((config.config or {}).get("per_delivery", 5))
        rows = (
            CourierContract.objects.filter(
                status=CourierContract.Status.DELIVERED,
                assigned_user__isnull=False,
                updated_at__gte=since,
                updated_at__lt=until,
            )
            .select_related("assigned_user")
        )
        for c in rows.iterator(chunk_size=500):
            cid = c.assigned_hauler_character_id or getattr(c.assigned_user, "main_character_id", None)
            if not cid:
                continue
            yield SourceEvent(
                character_id=cid,
                source_ref=f"courier:{c.id}",
                base_tickets=per_delivery,
                occurred_at=c.updated_at,
                magnitude=float(c.reward or 0),
                reason=f"Courier delivered: {c.origin_name} → {c.dest_name} ({per_delivery})",
                metadata={"contract_id": c.id, "reward": str(c.reward or 0)},
            )


class MentorshipSource(TicketSource):
    key = "mentorship"
    label = "Mentorship / academy"
    description = "Tickets for completed, rewardable mentorship tasks (mentee and/or mentor)."
    unit = "tasks"
    reliability = SEMI_AUTO
    default_mode = "officer_approved"
    default_config = {"per_task": 3, "reward_mentee": True, "reward_mentor": False}

    def iter_events(self, contest, config, since, until):
        from apps.mentorship.models import MentorshipTaskAssignment

        cfg = config.config or {}
        per_task = int(cfg.get("per_task", 3))
        reward_mentee = bool(cfg.get("reward_mentee", True))
        reward_mentor = bool(cfg.get("reward_mentor", False))
        rows = (
            MentorshipTaskAssignment.objects.filter(
                status=MentorshipTaskAssignment.Status.COMPLETED,
                rewardable=True,
                completed_at__gte=since,
                completed_at__lt=until,
            )
            .select_related("pairing__mentee__character", "pairing__mentor__character", "task")
        )
        for a in rows.iterator(chunk_size=500):
            recipients = []
            if reward_mentee and a.pairing.mentee_id:
                recipients.append(("mentee", a.pairing.mentee))
            if reward_mentor and a.pairing.mentor_id:
                recipients.append(("mentor", a.pairing.mentor))
            for role, profile in recipients:
                character = getattr(profile, "character", None)
                cid = getattr(character, "character_id", None)
                if not cid:
                    continue
                yield SourceEvent(
                    character_id=cid,
                    source_ref=f"mentorship:{role}:{a.id}",
                    base_tickets=per_task,
                    occurred_at=a.completed_at,
                    magnitude=1,
                    reason=f"Mentorship task completed ({role}, {per_task})",
                    metadata={"assignment_id": a.id, "role": role},
                )


class IndustrySource(TicketSource):
    key = "industry"
    label = "Manufacturing / industry"
    description = ("Tickets for industry contribution. No reliable per-pilot completed-job "
                   "feed exists, so awards are made by officer approval / manual grant.")
    unit = "jobs"
    reliability = MANUAL
    default_mode = "officer_approved"
    default_config = {"per_job": 2}
    # No automatic detection — iter_events intentionally yields nothing.


class DirectiveSource(TicketSource):
    """CMD-2 (3.6): tickets for completing ranked corp directives (the pilot quest log)."""

    key = "directive"
    label = "Directives completed"
    description = "Tickets for completing your ranked corp directives (the quest log)."
    unit = "directives"
    reliability = AUTOMATIC
    default_mode = "auto"
    default_config = {"per_directive": 2}

    def iter_events(self, contest, config, since, until):
        from apps.command_intel.models import PilotDirective

        per = int((config.config or {}).get("per_directive", 2))
        rows = (
            PilotDirective.objects.filter(
                state=PilotDirective.State.DONE,
                completed_at__gte=since, completed_at__lt=until,
            ).select_related("user")
        )
        for d in rows.iterator(chunk_size=500):
            cid = getattr(d.user, "main_character_id", None)
            if not cid:
                continue
            yield SourceEvent(
                character_id=cid,
                source_ref=f"directive:{d.id}",  # idempotent per directive within a contest
                base_tickets=per,
                occurred_at=d.completed_at,
                magnitude=1,
                reason=f"Completed “{d.title}”",
            )
