"""Operation readiness scoring and per-pilot prep."""
from __future__ import annotations

import datetime as dt
import logging

from apps.doctrines.services import character_readiness, doctrine_coverage

log = logging.getLogger("forca.operations")

# T-minus form-up reminder: minutes before form-up a committed pilot is pinged, once
# per commitment. Overridable via ``settings.OPERATIONS_FORMUP_REMINDER_LEAD_MINUTES``.
FORMUP_REMINDER_LEAD_MINUTES = 60
_FORMUP_EVENT_KEY = "operations.formup_reminder"

READY = ("viable", "optimal")

# Operation type → Pingboard alert category (drives routing / styling / rate tiers).
# Anything unmapped falls back to a generic PvP-fleet alert.
_OP_ALERT_CATEGORY = {
    "home_defence": "home_defence",
    "structure_timer": "structure_timer",
    "mining": "mining",
    "logistics": "logistics",
    "roam": "roaming_gang",
    "gatecamp": "gatecamp",
    "industrial": "industry_job",
}


def op_alert_category(op) -> str:
    return _OP_ALERT_CATEGORY.get(getattr(op, "type", ""), "pvp_fleet")


def _formup_lead_minutes() -> int:
    from django.conf import settings

    try:
        return max(1, int(getattr(settings, "OPERATIONS_FORMUP_REMINDER_LEAD_MINUTES",
                                  FORMUP_REMINDER_LEAD_MINUTES)))
    except (TypeError, ValueError):
        return FORMUP_REMINDER_LEAD_MINUTES


def _send_formup_reminder(op, commitment) -> bool:
    """Best-effort per-pilot form-up DM (targeted, never corp-wide). True if emitted."""
    from django.utils import timezone

    when = op.target_at
    mins = int((when - timezone.now()).total_seconds() // 60) if when else 0
    where = op.formup or op.destination or ""
    body = f"“{op.name}” forms up "
    body += f"in about {mins} min" if mins > 0 else "shortly"
    if where:
        body += f" at {where}"
    if op.comms:
        body += f" · comms {op.comms}"
    body += ". You signed up — see you there."
    # One scaffold key per English sentence shape (timed/imminent × place? × comms?): the optional
    # fragments are chrome, so they live inside a msgid rather than being smuggled through a slot
    # (a slot value is raw and would freeze the worker's English into every locale).
    if mins > 0:
        key = {
            (True, True): "operations.formup_reminder",
            (True, False): "operations.formup_reminder.no_comms",
            (False, True): "operations.formup_reminder.no_place",
            (False, False): "operations.formup_reminder.time_only",
        }[(bool(where), bool(op.comms))]
    else:
        key = {
            (True, True): "operations.formup_reminder.soon",
            (True, False): "operations.formup_reminder.soon_no_comms",
            (False, True): "operations.formup_reminder.soon_no_place",
            (False, False): "operations.formup_reminder.soon_only",
        }[(bool(where), bool(op.comms))]
    try:
        from apps.pingboard import services as pingboard

        pingboard.emit_broadcast(
            category=op_alert_category(op),
            title="Form-up reminder: {operation_name}",
            body=body,
            template=key,
            context={"operation_name": op.name, "start_time": f"{mins} min",
                     "formup_system": where, "link": op.comms},
            audience={"kind": "user", "id": commitment.user_id},
            source_service="operations",
            source_object_id=f"formup:{op.id}:{commitment.user_id}",
            idempotency_key=f"ops:formup:{op.id}:{commitment.user_id}",
        )
        return True
    except Exception:  # noqa: BLE001 — a notification must never break the sweep
        log.exception("form-up reminder failed for op %s user %s", op.id, commitment.user_id)
        return False


def send_formup_reminders() -> int:
    """DM each YES-committed pilot one T-minus reminder before their op forms up.

    Runs on a short beat cadence; fires only inside the lead window, only for ops still
    scheduled/active with a *future* form-up time (never a late reminder), and at most
    once per commitment — guarded by ``reminder_sent_at`` and a per-pilot idempotency key.
    Targeted at the individual pilots who claimed a seat, never a corp-wide broadcast.
    No-op when leadership turns the ``operations.formup_reminder`` event off.
    """
    from django.utils import timezone

    from apps.pingboard.notifications import is_enabled

    from .models import Operation, OperationCommitment

    if not is_enabled(_FORMUP_EVENT_KEY):
        return 0
    now = timezone.now()
    horizon = now + dt.timedelta(minutes=_formup_lead_minutes())
    ops = list(
        Operation.objects.filter(
            status__in=[Operation.Status.PLANNED, Operation.Status.ACTIVE],
            target_at__gt=now, target_at__lte=horizon,
        )
    )
    sent = 0
    for op in ops:
        commitments = list(OperationCommitment.objects.filter(
            operation=op, response=OperationCommitment.Response.YES,
            reminder_sent_at__isnull=True, user__isnull=False,
        ))
        for c in commitments:
            if _send_formup_reminder(op, c):
                sent += 1
            # Mark reminded regardless of send outcome so a broken channel never causes a
            # retry storm; the idempotency key is the second guard against a real duplicate.
            c.reminder_sent_at = now
            c.save(update_fields=["reminder_sent_at", "updated_at"])
    return sent


def notify_operation(op, *, title, body, source_suffix, channels=None, created_by=None,
                     template=None, context=None):
    """Best-effort corp alert for an operation, fanned out across every armed channel via
    Pingboard (Discord + in-app + EVE-mail + Telegram + WhatsApp + Slack) with history +
    retry. Returns the ``Alert`` (or ``None`` when disabled/suppressed/failed); never
    raises into the caller's business action.

    ``template`` (a ``pingboard.messages.SCAFFOLDS`` key) + ``context`` (raw scalars) make the
    alert re-renderable per recipient locale. An officer-composed announcement passes neither —
    its text is human free-text and is delivered verbatim (D14.6).
    """
    try:
        from apps.pingboard import services as pingboard

        # A fleet announcement is a corp-wide rally call — force the corp audience rather
        # than inheriting a category's officer/user routing default.
        return pingboard.emit_broadcast(
            category=op_alert_category(op), title=title, body=body,
            template=template, context=context,
            audience={"kind": "corp"},
            source_service="operations", source_object_id=f"{source_suffix}:{op.pk}",
            channels=channels, created_by=created_by,
        )
    except Exception:  # noqa: BLE001 - a notification must never block the business action
        log.exception("operation notify failed for op %s", getattr(op, "pk", "?"))
        return None


# --- Structure timers --------------------------------------------------------
# One entry point (``add_structure_timer``) used by BOTH the operations timer board
# and the Pingboard calendar's structure-timer form, so a timer added from either
# surface is the same ``StructureTimer`` row, lands on the Pingboard calendar, and
# fans a notification across every armed channel. The calendar is the aggregation +
# reminder layer; ``StructureTimer`` stays the single source of truth.
def create_structure_timer(*, name, exits_at, timer_type="", side="", system_name="",
                           structure_type="", notes="", created_by=None):
    """Create a ``StructureTimer`` with validated fields (pure — no notifications).

    Naive ``exits_at`` is read as EVE/UTC (the datetime-local widget has no tz); an
    unknown ``timer_type``/``side`` falls back to the model defaults (armor / hostile).
    """
    import datetime as dt

    from django.utils import timezone

    from .models import StructureTimer

    if timezone.is_naive(exits_at):
        exits_at = exits_at.replace(tzinfo=dt.UTC)
    if timer_type not in StructureTimer.TimerType.values:
        timer_type = StructureTimer.TimerType.ARMOR
    if side not in StructureTimer.Side.values:
        side = StructureTimer.Side.HOSTILE
    # Bound to the model field limits — the Pingboard form maps an unbounded description
    # textarea into notes(300), so truncate defensively rather than 500 on overflow.
    return StructureTimer.objects.create(
        name=(name or "").strip()[:200], system_name=(system_name or "").strip()[:120],
        structure_type=(structure_type or "").strip()[:80], timer_type=timer_type, side=side,
        exits_at=exits_at, notes=(notes or "").strip()[:300], created_by=created_by,
    )


def publish_structure_timer(timer):
    """Best-effort: mirror a timer onto the Pingboard calendar. Returns the event or None."""
    try:
        from apps.pingboard.calendar import publish_timer

        return publish_timer(timer)
    except Exception:  # noqa: BLE001 - a calendar mirror must never block the timer
        log.exception("timer calendar publish failed for timer %s", getattr(timer, "pk", "?"))
        return None


def unpublish_structure_timer(pk) -> None:
    """Best-effort: cancel the calendar mirror of a removed timer."""
    try:
        from apps.pingboard.calendar import cancel_event

        cancel_event(source_system="operations", source_object_id=f"timer:{pk}")
    except Exception:  # noqa: BLE001
        log.exception("timer calendar cancel failed for timer %s", pk)


def announce_structure_timer(timer, *, channels=None, created_by=None):
    """Best-effort corp alert for a timer, fanned across every armed channel (or the
    ``channels`` subset) via Pingboard. ``channels=None`` reaches every armed channel."""
    try:
        from django.utils import formats, translation

        from apps.pingboard import services as pingboard

        from .models import StructureTimer

        # Everything that lands in ``context`` or in the frozen English audit ``body`` is resolved
        # under EN, never under the announcing officer's request locale. ``date_format`` is
        # locale-aware (a Japanese officer would freeze Japanese weekday/month names into the
        # JSONField) and ``get_*_display()`` returns a gettext_lazy proxy, so both are pinned here.
        with translation.override("en"):
            when = formats.date_format(timer.exits_at, "D d M · H:i")
            body = (
                f"⏰ **Timer** — {timer.name} ({timer.get_timer_type_display()}, "
                f"{timer.get_side_display()})\n🕒 {when} EVE"
                + (f" · {timer.system_name}" if timer.system_name else "")
            )
        # The timer-type/side LABELS are chrome and live inside the msgid: one scaffold key per
        # (timer_type, side) pair × the with/without-system sentence shape, selected here from the
        # RAW codes. A label pushed through a context slot would be interpolated verbatim into
        # every recipient's render, freezing the emitting officer's locale. An out-of-choices code
        # (only reachable by a direct ORM write — ``create_structure_timer`` normalises) resolves
        # to no scaffold, so the alert safely degrades to the verbatim English ``body``.
        key = ""
        if (timer.timer_type in StructureTimer.TimerType.values
                and timer.side in StructureTimer.Side.values):
            key = f"operations.structure_timer.{timer.timer_type}.{timer.side}"
            if not timer.system_name:
                key += ".no_system"
        # An officer announcing a timer wants the whole corp to rally, so force the corp
        # audience rather than the structure_timer category's officer-only routing default.
        return pingboard.emit_broadcast(
            category="structure_timer", title="Timer — {structure_name}", body=body,
            template=key or None,
            context={
                "structure_name": timer.name,
                "timer_time": when,
                "system_name": timer.system_name,
            },
            audience={"kind": "corp"}, channels=channels, source_service="operations",
            source_object_id=f"timer:{timer.pk}", created_by=created_by,
        )
    except Exception:  # noqa: BLE001 - a notification must never block adding the timer
        log.exception("timer announce failed for timer %s", getattr(timer, "pk", "?"))
        return None


def add_structure_timer(*, name, exits_at, timer_type="", side="", system_name="",
                        structure_type="", notes="", announce=False, channels=None,
                        created_by=None):
    """Create a timer, mirror it to the calendar, and (optionally) announce it — the
    single entry point shared by the operations board and the Pingboard calendar."""
    timer = create_structure_timer(
        name=name, exits_at=exits_at, timer_type=timer_type, side=side,
        system_name=system_name, structure_type=structure_type, notes=notes,
        created_by=created_by,
    )
    publish_structure_timer(timer)
    if announce:
        announce_structure_timer(timer, channels=channels, created_by=created_by)
    return timer


# How close to the RSVP deadline an op with unmet requirements is flagged "at risk".
AT_RISK_WINDOW_HOURS = 6


def days_until(target_at) -> int | None:
    """Whole days from now until ``target_at`` (negative if past), or None."""
    if not target_at:
        return None
    from django.utils import timezone

    # timedelta.days floors toward -inf: a target a few hours out is day 0
    # ("today"); anything in the past is negative.
    return (target_at - timezone.now()).days


def urgency_for(days: int | None) -> str:
    """Time-aware urgency tier for an upcoming operation."""
    if days is None:
        return "none"
    if days < 0:
        return "overdue"
    if days <= 2:
        return "critical"
    if days <= 7:
        return "high"
    if days <= 21:
        return "medium"
    return "low"


def _corp_characters():
    from apps.sso.models import EveCharacter

    return list(EveCharacter.objects.filter(is_corp_member=True))


def operation_readiness(operation) -> dict:
    """Corp readiness for an operation: overall % + per-doctrine coverage + gaps."""
    characters = _corp_characters()
    rows = []
    total_ratio = 0.0
    scored = 0
    gaps = []
    for od in operation.doctrines.select_related("doctrine").all():
        doctrine = od.doctrine
        counts = doctrine_coverage(doctrine, characters)
        known = counts["optimal"] + counts["viable"] + counts["not_ready"]
        ready = counts["optimal"] + counts["viable"]
        target = od.target_count or 0
        # Ratio: against the wanted count if set, else against known pilots.
        ratio = (min(ready, target) / target) if target else (ready / known if known else 0.0)
        if known or target:
            total_ratio += ratio
            scored += 1
        rows.append(
            {
                "doctrine": doctrine,
                "ready": ready,
                "known": known,
                "target": target,
                "pct": round(100 * ratio),
            }
        )
        if ratio < 1.0:
            gaps.append(
                {
                    "doctrine_id": doctrine.id,
                    "label": f"{doctrine.name}: {ready}{'/' + str(target) if target else ''} ready",
                    "shortfall": (target - ready) if target else (known - ready),
                }
            )
    pct = round(100 * total_ratio / scored) if scored else 0
    days = days_until(operation.target_at)
    urgency = urgency_for(days)
    # Time-aware: a real shortfall with little time left is "at risk" — unlikely to
    # close before the target. Gaps share the op's date, so urgency is op-level.
    at_risk = urgency in ("critical", "high", "overdue")
    for g in gaps:
        g["urgency"] = urgency
        g["at_risk"] = at_risk and g["shortfall"] > 0
    return {
        "pct": pct, "rows": rows,
        "gaps": sorted(gaps, key=lambda g: -g["shortfall"]),
        "days_until": days, "urgency": urgency,
        "at_risk_gaps": sum(1 for g in gaps if g["at_risk"]),
    }


def set_rsvp(operation, user, response: str, character=None):
    """Record/replace a pilot's availability signal for an operation."""
    from .models import OperationRsvp

    if response not in OperationRsvp.Response.values:
        return None
    rsvp, _ = OperationRsvp.objects.update_or_create(
        operation=operation, user=user,
        defaults={
            "response": response,
            "character_name": (character.name if character else user.get_username()),
        },
    )
    return rsvp


def rsvp_summary(operation) -> dict:
    """Counts + grouped responders for an operation's availability board."""
    rsvps = list(operation.rsvps.all())
    counts = {"yes": 0, "maybe": 0, "no": 0}
    for r in rsvps:
        counts[r.response] = counts.get(r.response, 0) + 1
    return {
        "counts": counts,
        "committed": counts["yes"] + counts["maybe"],
        "responders": rsvps,
    }


def pilot_readiness(operation, character) -> list[dict]:
    """Whether the pilot can fly each of the operation's doctrines."""
    out = []
    rank = {"optimal": 3, "viable": 2, "not_ready": 1, "unknown": 0}
    # One snapshot fetch + prefetched fits/requirements for the whole op, instead of a
    # snapshot query per fit and a fits query per doctrine.
    snapshot = character.skill_snapshots.filter(is_latest=True).first()
    ods = operation.doctrines.select_related("doctrine").prefetch_related(
        "doctrine__fits__skill_requirements"
    )
    for od in ods:
        doctrine = od.doctrine
        best = "unknown"
        for fit in doctrine.fits.all():
            s = character_readiness(character, fit, snapshot=snapshot).status
            if rank[s] > rank[best]:
                best = s
        out.append({"doctrine": doctrine, "status": best, "ready": best in READY})
    return out


def upcoming_for_pilot(character):
    """The nearest upcoming operation and the pilot's readiness for it."""
    from .models import Operation

    op = (
        Operation.objects.filter(status__in=[Operation.Status.PLANNED, Operation.Status.ACTIVE])
        .order_by("target_at", "-created_at")
        .first()
    )
    if not op or character is None:
        return None
    rows = pilot_readiness(op, character)
    if not rows:
        return None
    ready = sum(1 for r in rows if r["ready"])
    return {"op": op, "rows": rows, "ready": ready, "total": len(rows),
            "pct": round(100 * ready / len(rows)) if rows else 0}


def participation_leaderboard(days: int = 90, limit: int = 10) -> list[dict]:
    """Top fleet participants over the window, honouring recognition opt-out."""
    from datetime import timedelta

    from django.db.models import Count
    from django.utils import timezone

    from apps.pilots.models import PilotPreference

    from .models import OperationAttendance

    since = timezone.now() - timedelta(days=days)
    opted_out = set(
        PilotPreference.objects.filter(public_recognition=False).values_list("user_id", flat=True)
    )
    rows = (
        # OPS-2 (3.1): only confirmed / ESI-verified PAP counts — unconfirmed self-reports
        # must not inflate the participation leaderboard.
        OperationAttendance.objects.filter(created_at__gte=since, confirmed=True)
        .exclude(user_id__in=opted_out)
        .values("user_id")
        .annotate(n=Count("id"))
        .order_by("-n")[:limit]
    )
    out = []
    for r in rows:
        name = (
            OperationAttendance.objects.filter(user_id=r["user_id"])
            .exclude(character_name="").values_list("character_name", flat=True).first()
            or f"Pilot {r['user_id']}"
        )
        out.append({"name": name, "count": r["n"]})
    return out


# --- Doctrine ship catalogue (for the composition builder) -------------------
def doctrine_fit_catalogue() -> dict:
    """Every active doctrine ship, with the category/hull/role facets to filter on.

    Reuses the doctrines browse layer so the operations builder shows the same
    catalogue as the /doctrines/ ships browser. Returns ``{"fits": [...],
    "categories": [...], "hull_classes": [...], "roles": [...]}`` — all JSON-safe.
    """
    from apps.doctrines.browse import enriched_fits, filter_options
    from apps.doctrines.models import Doctrine

    rows = enriched_fits(None)  # no character → no skill/price lookups
    # Map each doctrine to its category for the category filter.
    cat = dict(
        Doctrine.objects.filter(status=Doctrine.Status.ACTIVE)
        .values_list("id", "category__label")
    )
    fits = [{
        "fit_id": r["fit_id"],
        "ship_name": r["ship_name"],
        "ship_type_id": r["ship_type_id"],
        "doctrine": r["doctrine"],
        "doctrine_id": r["doctrine_id"],
        "category": cat.get(r["doctrine_id"]) or "Uncategorised",
        "hull_class": r["hull_class"],
        "role": r["role"] or "",
    } for r in rows]
    opts = filter_options(rows)
    categories = sorted({f["category"] for f in fits})
    return {
        "fits": fits,
        "categories": categories,
        "hull_classes": opts["hull_classes"],
        "roles": opts["roles"],
    }


# --- Fleet composition planning ----------------------------------------------
def recompute_deadline(operation) -> None:
    """Derive the absolute RSVP deadline from the relative offset, if one is set.

    Keeps the deadline correct when the form-up time is edited (edge case: start
    time changed after creation). Caller is responsible for saving.
    """
    if operation.rsvp_offset_minutes is not None and operation.target_at is not None:
        import datetime as dt

        operation.rsvp_deadline = operation.target_at - dt.timedelta(
            minutes=operation.rsvp_offset_minutes
        )


def deadline_passed(operation, now=None) -> bool:
    from django.utils import timezone

    if not operation.rsvp_deadline:
        return False
    return (now or timezone.now()) >= operation.rsvp_deadline


def fleet_plan(operation) -> dict:
    """Live fleet-composition picture for an operation.

    Per ship slot: how many have committed, how many are still required, whether
    it's capped/full, and (by organiser priority) which slot to recommend next.
    Distinguishes *required* slots (needed for viability) from *extra* sign-ups.
    """
    from django.utils import timezone

    from .models import OperationCommitment

    slots = list(operation.ship_slots.all())  # ordered by priority then id
    commitments = list(operation.commitments.select_related("slot").all())
    # Only firm "Coming" sign-ups fill seats; "Maybe" is a soft tally shown alongside.
    yes_by_slot: dict[int, int] = {}
    maybe_by_slot: dict[int, int] = {}
    for c in commitments:
        if c.slot_id is None:
            continue
        bucket = maybe_by_slot if c.response == OperationCommitment.Response.MAYBE else yes_by_slot
        bucket[c.slot_id] = bucket.get(c.slot_id, 0) + 1

    rows = []
    required_total = 0
    required_filled = 0
    composition_met = True
    recommended = None
    for slot in slots:
        confirmed = yes_by_slot.get(slot.id, 0)
        maybe = maybe_by_slot.get(slot.id, 0)
        still_needed = max(0, slot.min_pilots - confirmed)
        capped = slot.max_pilots is not None
        has_room = (not capped) or confirmed < slot.max_pilots
        required_total += slot.min_pilots
        required_filled += min(confirmed, slot.min_pilots)
        if still_needed > 0:
            composition_met = False
            if recommended is None:
                recommended = slot  # first (highest-priority) slot still short
        rows.append({
            "slot": slot, "confirmed": confirmed, "maybe": maybe, "min": slot.min_pilots,
            "max": slot.max_pilots, "still_needed": still_needed,
            "met": still_needed == 0, "has_room": has_room, "capped": capped,
            "pct": round(100 * min(confirmed, slot.min_pilots) / slot.min_pilots) if slot.min_pilots else 100,
        })
    # If every required slot is full, recommend the highest-priority slot with room.
    if recommended is None:
        recommended = next((s for s, r in zip(slots, rows, strict=True) if r["has_room"]), None)

    total_confirmed = sum(1 for c in commitments if c.response != OperationCommitment.Response.MAYBE)
    total_maybe = sum(1 for c in commitments if c.response == OperationCommitment.Response.MAYBE)
    min_pilots = operation.min_pilots or 0
    min_met = total_confirmed >= min_pilots
    # A "viable" fleet has the head-count AND the required composition (or an override).
    requirements_met = min_met and composition_met
    viable = requirements_met or operation.requirements_overridden

    # Does the declared composition line up with the head-count requirement?
    slot_mismatch = bool(slots) and min_pilots and required_total != min_pilots

    # Posture: the human-facing state derived from status + requirements + clock.
    now = timezone.now()
    near_deadline = bool(
        operation.rsvp_deadline
        and 0 <= (operation.rsvp_deadline - now).total_seconds() <= AT_RISK_WINDOW_HOURS * 3600
    )
    if operation.status == operation.Status.DRAFT:
        posture = "draft"
    elif operation.status == operation.Status.CANCELLED_AUTO:
        posture = "cancelled_auto"
    elif operation.status == operation.Status.CANCELLED:
        posture = "cancelled"
    elif operation.status == operation.Status.DONE:
        posture = "completed"
    elif operation.status == operation.Status.ACTIVE:
        posture = "running"
    elif viable:
        posture = "ready" if requirements_met else "override"
    elif near_deadline or deadline_passed(operation, now):
        posture = "at_risk"
    else:
        posture = "scheduled"

    return {
        "slots": rows,
        "has_slots": bool(slots),
        "total_confirmed": total_confirmed,
        "total_maybe": total_maybe,
        "min_pilots": min_pilots,
        "min_met": min_met,
        "still_needed_pilots": max(0, min_pilots - total_confirmed),
        "composition_met": composition_met,
        "required_total": required_total,
        "required_filled": required_filled,
        "requirements_met": requirements_met,
        "viable": viable,
        "overridden": operation.requirements_overridden,
        "slot_mismatch": slot_mismatch,
        "recommended_slot_id": recommended.id if recommended else None,
        "near_deadline": near_deadline,
        "deadline_passed": deadline_passed(operation, now),
        "posture": posture,
    }


# Outcomes returned by claim_slot, so views can map them to messages.
CLAIM_OK = "ok"
CLAIM_CLOSED = "closed"
CLAIM_INVALID_SLOT = "invalid_slot"
CLAIM_FULL = "full"


def claim_slot(operation, user, slot_id, character=None, response=None) -> str:
    """Commit a pilot to a ship slot with a ``response`` (coming / maybe), race-safely.

    Concurrent claims on the same operation are serialised with a row lock so two
    pilots can't both take the last capped seat. Only firm "Coming" sign-ups occupy
    a seat, so a "Maybe" never overbooks a capped slot. Switching ship or answer
    updates the pilot's single commitment row. Returns a ``CLAIM_*`` constant.
    """
    from django.db import transaction

    from .models import Operation, OperationCommitment

    if response not in OperationCommitment.Response.values:
        response = OperationCommitment.Response.YES
    char_name = character.name if character else user.get_username()
    with transaction.atomic():
        op = Operation.objects.select_for_update().get(pk=operation.pk)
        if not op.is_open_for_signup:
            return CLAIM_CLOSED
        slot = op.ship_slots.filter(pk=slot_id).first()
        if slot is None:
            return CLAIM_INVALID_SLOT
        # Only firm "Coming" sign-ups occupy a capped seat. Count them inside the
        # lock (excluding the claimant, who may be switching to this slot).
        if slot.max_pilots is not None and response == OperationCommitment.Response.YES:
            taken = (
                OperationCommitment.objects.filter(
                    operation=op, slot=slot, response=OperationCommitment.Response.YES
                ).exclude(user=user).count()
            )
            if taken >= slot.max_pilots:
                return CLAIM_FULL
        OperationCommitment.objects.update_or_create(
            operation=op, user=user,
            defaults={"slot": slot, "character_name": char_name, "response": response},
        )
    return CLAIM_OK


def release_commitment(operation, user) -> bool:
    """Drop a pilot's commitment. Returns True if one existed."""
    from .models import OperationCommitment

    deleted, _ = OperationCommitment.objects.filter(operation=operation, user=user).delete()
    return bool(deleted)


def _composition_snapshot(operation) -> tuple[dict, dict]:
    """``(required, actual)`` maps of ship name → count, for a cancellation record."""
    plan = fleet_plan(operation)
    required = {}
    actual = {}
    for row in plan["slots"]:
        label = row["slot"].ship_name or f"slot {row['slot'].id}"
        required[label] = row["min"]
        actual[label] = row["confirmed"]
    return required, actual


def record_cancellation(operation, reason: str, *, confirmed: int | None = None):
    """Write the immutable analytics snapshot for a cancelled operation."""
    from .models import OperationCancellation

    required, actual = _composition_snapshot(operation)
    if confirmed is None:
        confirmed = operation.commitments.count()
    fc = operation.effective_fc
    return OperationCancellation.objects.create(
        operation=operation,
        operation_pk=operation.pk,
        operation_type=operation.type,
        organiser_name=(getattr(fc, "username", "") or "") if fc else "",
        scheduled_start=operation.target_at,
        rsvp_deadline=operation.rsvp_deadline,
        min_pilots=operation.min_pilots or 0,
        confirmed_at_deadline=confirmed,
        required_composition=required,
        actual_composition=actual,
        reason=reason,
    )


def auto_cancel_due(now=None) -> list[int]:
    """Auto-cancel scheduled ops whose RSVP deadline passed without enough sign-ups.

    A deadline-passed operation is cancelled unless the organiser overrode the
    requirement or the minimum head-count AND required composition are both met.
    Returns the ids cancelled. Safe to run repeatedly (only touches PLANNED ops).
    """
    from django.utils import timezone

    from .models import Operation, OperationCancellation

    now = now or timezone.now()
    due = Operation.objects.filter(
        status=Operation.Status.PLANNED,
        requirements_overridden=False,
        rsvp_deadline__isnull=False,
        rsvp_deadline__lte=now,
    )
    cancelled = []
    for op in due:
        plan = fleet_plan(op)
        if plan["requirements_met"]:
            continue  # enough pilots + composition — let it run
        reason = (
            OperationCancellation.Reason.INSUFFICIENT
            if not plan["min_met"]
            else OperationCancellation.Reason.COMPOSITION
        )
        record_cancellation(op, reason, confirmed=plan["total_confirmed"])
        op.status = Operation.Status.CANCELLED_AUTO
        op.save(update_fields=["status", "updated_at"])
        _announce_auto_cancel(op, plan)
        cancelled.append(op.pk)
    return cancelled


def _announce_auto_cancel(operation, plan) -> None:
    """Best-effort auto-cancel notice, fanned out across every armed channel via Pingboard.

    Reaches Discord + in-app + EVE-mail + Telegram + WhatsApp + Slack (whatever the corp
    armed) and lands each confirmed pilot's per-pilot in-app alert — superseding the old
    Discord-webhook-only broadcast. Never blocks the cancellation.
    """
    notify_operation(
        operation, source_suffix="cancel",
        title="Cancelled — {operation_name}",
        body=(
            f"❌ {operation.name} auto-cancelled — only {plan['total_confirmed']} "
            f"of {operation.min_pilots} pilots confirmed by the sign-up deadline."
        ),
        template="operations.auto_cancelled",
        context={"operation_name": operation.name, "count": plan["total_confirmed"],
                 "required_count": operation.min_pilots},
    )


# --------------------------------------------------------------------------- #
#  OPS-4 (3.12): recurring / templated strat-op schedules
# --------------------------------------------------------------------------- #
def _template_occurrences(template, now, horizon):
    """Weekly (weekday/hour/minute, UTC) form-up datetimes in the window [now, horizon]."""
    from datetime import timedelta

    anchor = now.astimezone(dt.UTC).replace(
        hour=template.hour, minute=template.minute, second=0, microsecond=0)
    anchor += timedelta(days=(template.weekday - anchor.weekday()) % 7)
    out = []
    while anchor <= horizon:
        if anchor >= now:
            out.append(anchor)
        anchor += timedelta(days=7)
    return out


def _publish_op_calendar(op) -> None:
    """Best-effort: mirror a materialised op onto the Pingboard calendar."""
    try:
        from apps.pingboard.calendar import publish_event

        publish_event(
            source_system="operations", source_object_id=op.pk, event_type="fleet_op",
            title=op.name, start_at=op.target_at,
        )
    except Exception:  # noqa: BLE001 — a calendar mirror must never block materialisation
        log.exception("op calendar publish failed for op %s", getattr(op, "pk", "?"))


def _spawn_from_template(template, occurrence):
    """Create one ``Operation`` from a template at ``occurrence``; None if it already exists."""
    from datetime import timedelta

    from .models import Operation

    offset = template.rsvp_offset_minutes or 0
    op, created = Operation.objects.get_or_create(
        recurring_template=template, target_at=occurrence,
        defaults={
            "name": template.name, "type": template.type,
            "duration_minutes": template.duration_minutes, "formup": template.formup,
            "destination": template.destination, "comms": template.comms, "link": template.link,
            "notes": template.notes, "min_pilots": template.min_pilots, "srp": template.srp,
            "rsvp_offset_minutes": offset or None,
            "rsvp_deadline": (occurrence - timedelta(minutes=offset)) if offset else None,
            "fc": template.fc, "created_by": template.created_by,
            "status": Operation.Status.PLANNED,
        },
    )
    if not created:  # a concurrent/prior run already materialised this form-up time
        return None
    for slot in template.slots.all():
        op.ship_slots.create(
            ship_name=slot.ship_name, ship_type_id=slot.ship_type_id, role=slot.role,
            min_pilots=slot.min_pilots, max_pilots=(slot.max_pilots or None),
            priority=slot.priority,
        )
    _publish_op_calendar(op)
    return op


def materialize_recurring_ops() -> dict:
    """Beat: spawn upcoming ``Operation`` instances from every active template on its cadence.

    Idempotent — each (template, form-up time) is created at most once via get_or_create on
    (recurring_template, target_at). Returns the count created this run."""
    from datetime import timedelta

    from django.utils import timezone

    from .models import OperationTemplate

    now = timezone.now()
    created = 0
    for template in OperationTemplate.objects.filter(active=True).prefetch_related("slots"):
        horizon = now + timedelta(days=template.lead_days)
        for occ in _template_occurrences(template, now, horizon):
            try:
                if _spawn_from_template(template, occ) is not None:
                    created += 1
            except Exception:  # noqa: BLE001 — one bad template must not starve the rest
                log.exception("materialise failed for template %s @ %s", template.pk, occ)
    return {"created": created}
