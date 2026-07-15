"""Raffle business logic — the only place raffle state changes.

Views are thin: they parse input, call a service, audit, flash and redirect. All
rules live here — the guarded contest lifecycle, the enrolment-gated manual grant
(and its audited emergency override), the CSPRNG draw orchestration (cross-worker
lock + DB compare-and-set so a retried beat can never draw twice), leaderboard
summary recomputation, append-only ledger corrections and prize fulfilment.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext
from django.utils.translation import ngettext

from core.audit import audit_log

from . import boosters, notify
from . import draw as draw_engine
from . import eligibility as elig
from .models import (
    RaffleConfig,
    RaffleContest,
    RaffleDraw,
    RaffleDrawResult,
    RaffleEnrolmentOutreach,
    RaffleExclusion,
    RaffleIneligibleActivity,
    RaffleManualGrant,
    RaffleOutreachOptOut,
    RaffleParticipantSummary,
    RaffleTicketLedgerEntry,
    RaffleTicketSourceConfig,
)
from .sources import DEFAULT_ENABLED_KEYS, all_sources

_log = logging.getLogger("forca.raffle")
_DRAW_LOCK_TTL = 300


# --------------------------------------------------------------------------- #
#  Global config
# --------------------------------------------------------------------------- #
def active_config() -> RaffleConfig:
    """The live raffle-wide config, seeding a default the first time."""
    cfg = RaffleConfig.objects.filter(is_active=True).order_by("-updated_at").first()
    if cfg is None:
        cfg = RaffleConfig.objects.create(name="Default", is_active=True)
    return cfg


# --------------------------------------------------------------------------- #
#  Source configuration
# --------------------------------------------------------------------------- #
def seed_source_configs(contest: RaffleContest) -> None:
    """Ensure every registered source has a config row for the contest.

    PVP + manual are enabled by default (the two always-reliable sources); the rest
    are created disabled with their sensible defaults so leaders can opt in.
    """
    existing = set(contest.source_configs.values_list("source_key", flat=True))
    to_create = []
    for source in all_sources():
        if source.key in existing:
            continue
        to_create.append(RaffleTicketSourceConfig(
            contest=contest,
            source_key=source.key,
            enabled=source.key in DEFAULT_ENABLED_KEYS,
            mode=source.default_mode,
            config=dict(source.default_config),
            filters=dict(source.default_filters),
            require_esi=True,
        ))
    if to_create:
        RaffleTicketSourceConfig.objects.bulk_create(to_create, ignore_conflicts=True)


# --------------------------------------------------------------------------- #
#  Lifecycle
# --------------------------------------------------------------------------- #
_ALLOWED_TRANSITIONS = {
    RaffleContest.Status.DRAFT: {RaffleContest.Status.SCHEDULED, RaffleContest.Status.ACTIVE,
                                 RaffleContest.Status.CANCELLED},
    RaffleContest.Status.SCHEDULED: {RaffleContest.Status.ACTIVE, RaffleContest.Status.DRAFT,
                                     RaffleContest.Status.CANCELLED},
    RaffleContest.Status.ACTIVE: {RaffleContest.Status.CLOSED, RaffleContest.Status.CANCELLED},
    RaffleContest.Status.CLOSED: {RaffleContest.Status.COMPLETED, RaffleContest.Status.ACTIVE,
                                  RaffleContest.Status.CANCELLED},
    RaffleContest.Status.COMPLETED: {RaffleContest.Status.ARCHIVED},
    RaffleContest.Status.ARCHIVED: {RaffleContest.Status.COMPLETED},
    RaffleContest.Status.CANCELLED: set(),
}


def can_transition(contest: RaffleContest, new_status: str) -> bool:
    return new_status in _ALLOWED_TRANSITIONS.get(contest.status, set())


@transaction.atomic
def set_status(contest: RaffleContest, new_status: str, actor=None, *, reason: str = "") -> bool:
    """Guarded lifecycle transition + audit + side effects.

    Returns False on an illegal transition (caller flashes an error).
    """
    locked = RaffleContest.objects.select_for_update().get(pk=contest.pk)
    if new_status == locked.status:
        return True
    if not can_transition(locked, new_status):
        return False
    # RAF-5 (3.14): hold a contest from COMMITTING new prize spend — i.e. leaving draft into a
    # will-draw status (schedule or activate) — when its draw month would breach the ceiling.
    # Re-activating an already-committed contest (scheduled/closed → active) adds no new spend,
    # so it is never blocked; and guarding the draft-exit closes the schedule-then-activate gap.
    if (locked.status == RaffleContest.Status.DRAFT
            and new_status in _committed_statuses()
            and budget_block_reason(locked)):
        return False
    old = locked.status
    locked.status = new_status
    locked.save(update_fields=["status", "updated_at"])
    contest.status = new_status
    audit_log(actor, "raffle.status", target_type="raffle_contest", target_id=str(locked.pk),
              metadata={"from": old, "to": new_status, "reason": reason})

    if new_status in (RaffleContest.Status.SCHEDULED, RaffleContest.Status.ACTIVE):
        notify.publish_timeline(locked)
    if new_status == RaffleContest.Status.ACTIVE:
        blurb = (locked.objective or locked.description or "")[:400]
        notify.announce(locked, title="🎟️ Raffle open: {contest_name}",
                        body=blurb + "  Connect your ESI token and fly to earn tickets.",
                        # The officer-written blurb is corp content (verbatim in every locale,
                        # D14.8) and rides in as a raw slot; only the chrome around it localises.
                        template="raffle.started",
                        context={"contest_name": locked.name, "details": blurb},
                        suffix="started")
    if new_status == RaffleContest.Status.CLOSED:
        notify.announce(locked, title="Raffle closed: {contest_name}",
                        body="Ticket accrual is closed. The draw happens soon — good luck!",
                        template="raffle.closed",
                        context={"contest_name": locked.name},
                        suffix="closed")
    if new_status == RaffleContest.Status.CANCELLED:
        notify.cancel_timeline(locked)
    return True


def open_scheduled_contests() -> int:
    """Beat helper: activate scheduled/draft contests whose start time has passed."""
    now = timezone.now()
    n = 0
    qs = RaffleContest.objects.filter(
        status__in=[RaffleContest.Status.SCHEDULED, RaffleContest.Status.DRAFT],
        start_at__lte=now, end_at__gt=now,
    )
    for contest in qs:
        # Only auto-open contests explicitly scheduled (never silent-activate a draft).
        if contest.status == RaffleContest.Status.SCHEDULED and set_status(contest, RaffleContest.Status.ACTIVE):
            n += 1
    return n


def close_ended_contests() -> int:
    """Beat helper: close active contests whose end time has passed (freezes ledger)."""
    now = timezone.now()
    n = 0
    for contest in RaffleContest.objects.filter(status=RaffleContest.Status.ACTIVE, end_at__lte=now):
        if set_status(contest, RaffleContest.Status.CLOSED):
            n += 1
    return n


# --------------------------------------------------------------------------- #
#  Manual grants
# --------------------------------------------------------------------------- #
class GrantBlocked(Exception):
    """A manual grant was refused (ineligible pilot, no override, bad input)."""


class ActivityNotMet(Exception):
    """The draw was held because the contest hasn't reached its minimum activity."""


@transaction.atomic
def grant_manual_tickets(contest, actor, *, character_id=None, user=None, amount: int,
                         reason: str, category: str = "", internal_notes: str = "",
                         override: bool = False) -> RaffleManualGrant:
    """Grant tickets to a pilot, enforcing the enrolment rule by default.

    A grant to a non-enrolled / invalid-token pilot is refused (``GrantBlocked``)
    unless ``override`` is set AND the raffle config's emergency override is enabled
    AND the actor is a Director — in which case it is loudly audited. Everything
    (grant + the ledger row it creates) is written atomically.
    """
    from apps.sso.models import EveCharacter
    from core import rbac

    if amount is None or int(amount) <= 0:
        raise GrantBlocked(gettext("Ticket amount must be a positive number."))
    amount = int(amount)
    if not reason or not reason.strip():
        raise GrantBlocked(gettext("A reason is required for every manual grant."))
    if contest.is_frozen:
        raise GrantBlocked(gettext("This contest's ledger is frozen — no new grants."))

    character = None
    if user is not None and character_id is None:
        character_id = getattr(user, "main_character_id", None)
    if character_id:
        character = EveCharacter.objects.filter(character_id=character_id).select_related("user").first()
        if character and user is None:
            user = character.user

    # Separation of duties: an officer may not grant tickets to their own account
    # (a Director / superuser is exempt as a break-glass), mirroring SRP.
    if (user is not None and actor is not None and user.pk == getattr(actor, "pk", None)
            and not rbac.has_role(actor, rbac.ROLE_DIRECTOR)
            and not getattr(actor, "is_superuser", False)):
        raise GrantBlocked(gettext("You can't grant raffle tickets to your own account."))

    e = (
        elig.for_character(contest, character) if character is not None
        else elig.Eligibility(reason_code="not_enrolled", character_id=character_id)
    )

    override_used = False
    if not e.eligible:
        cfg = active_config()
        may_override = (
            override and cfg.allow_manual_override and rbac.has_role(actor, rbac.ROLE_DIRECTOR)
        )
        if not may_override:
            raise GrantBlocked(
                gettext("%(reason)s Ask them to enrol in FORCA Command Grid and connect their "
                   "ESI token first.")
                % {"reason": e.message or gettext("Pilot is not eligible.")}
            )
        override_used = True

    grant = RaffleManualGrant.objects.create(
        contest=contest, user=user, character_id=character_id,
        character_name=(character.name if character else "") or e.character_name,
        amount=amount, reason=reason.strip(), category=category.strip(),
        internal_notes=internal_notes, override_used=override_used, granted_by=actor,
    )
    entry = RaffleTicketLedgerEntry.objects.create(
        contest=contest, user=user, character_id=character_id or 0,
        character_name=grant.character_name,
        source_key="manual", source_ref=f"manual:{grant.pk}",
        amount=amount, reason=(reason.strip() or "Manual grant"),
        status=RaffleTicketLedgerEntry.Status.APPROVED,
        occurred_at=timezone.now(),
        eligibility_snapshot=e.snapshot(),
        esi_status=e.esi_status,
        created_by_system=False, created_by=actor,
        metadata={"category": category, "override": override_used},
    )
    grant.ledger_entry = entry
    grant.save(update_fields=["ledger_entry", "updated_at"])

    audit_log(actor, "raffle.manual_grant", target_type="raffle_contest",
              target_id=str(contest.pk),
              metadata={"grant_id": grant.pk, "amount": amount, "character_id": character_id,
                        "override": override_used, "reason": reason[:120]})
    if user is not None:
        notify.notify_user(contest, user.id, title="🎟️ +{ticket_count} raffle tickets",
                           body=f"You received {amount} tickets in “{contest.name}”: {reason[:200]}",
                           template="raffle.grant",
                           context={"ticket_count": amount, "contest_name": contest.name,
                                    "reason": reason[:200]},
                           suffix="grant")
    return grant


# --------------------------------------------------------------------------- #
#  Exclusions
# --------------------------------------------------------------------------- #
@transaction.atomic
def exclude_pilot(contest, actor, *, user=None, character_id=None, character_name: str = "",
                  reason: str) -> RaffleExclusion:
    if not reason.strip():
        raise ValidationError(gettext("An exclusion reason is required."))
    excl, _ = RaffleExclusion.objects.update_or_create(
        contest=contest,
        user=user if user is not None else None,
        character_id=character_id if user is None else None,
        defaults={"reason": reason.strip(), "active": True, "excluded_by": actor,
                  "character_name": character_name},
    )
    audit_log(actor, "raffle.exclude", target_type="raffle_contest", target_id=str(contest.pk),
              metadata={"user_id": getattr(user, "id", None), "character_id": character_id,
                        "reason": reason[:120]})
    return excl


@transaction.atomic
def remove_exclusion(exclusion: RaffleExclusion, actor) -> None:
    exclusion.active = False
    exclusion.save(update_fields=["active", "updated_at"])
    audit_log(actor, "raffle.exclude.remove", target_type="raffle_contest",
              target_id=str(exclusion.contest_id), metadata={"exclusion_id": exclusion.pk})


# --------------------------------------------------------------------------- #
#  Ledger corrections (append-only)
# --------------------------------------------------------------------------- #
@transaction.atomic
def reverse_entry(entry: RaffleTicketLedgerEntry, actor, *, reason: str) -> RaffleTicketLedgerEntry:
    """Correct a ticket award by appending a reversal — never a destructive edit."""
    locked = RaffleTicketLedgerEntry.objects.select_for_update().get(pk=entry.pk)
    if locked.status == RaffleTicketLedgerEntry.Status.REVERSED:
        return locked
    locked.status = RaffleTicketLedgerEntry.Status.REVERSED
    locked.save(update_fields=["status", "updated_at"])
    # The reversal is an append-only audit record of the correction. It carries the
    # REVERSED status (not APPROVED) so it — like the original it cancels — is
    # excluded from every "approved" sum, keeping the draw, leaderboard and stats
    # consistently netted to zero (the original's removal is the actual correction).
    reversal = RaffleTicketLedgerEntry.objects.create(
        contest_id=locked.contest_id, user_id=locked.user_id, character_id=locked.character_id,
        character_name=locked.character_name, source_key=locked.source_key,
        source_ref=f"reversal:{locked.pk}", amount=-locked.amount,
        reason=f"Reversal: {reason[:200]}", status=RaffleTicketLedgerEntry.Status.REVERSED,
        # Seam B: the "Reversal:" prefix is our prose (translatable); the officer's note is
        # human free text, interpolated raw and rendered verbatim in every locale.
        reason_key="ledger.reversal", reason_params={"reason": reason[:200]},
        occurred_at=locked.occurred_at, created_by_system=False, created_by=actor,
        metadata={"reverses": locked.pk},
    )
    audit_log(actor, "raffle.ledger.reverse", target_type="raffle_ticket",
              target_id=str(locked.pk), metadata={"reason": reason[:160], "amount": locked.amount})
    return reversal


@transaction.atomic
def set_entry_status(entry: RaffleTicketLedgerEntry, actor, status: str, *, reason: str = "") -> bool:
    """Approve a pending entry or exclude/disqualify one (officer review). Audited."""
    valid = {RaffleTicketLedgerEntry.Status.APPROVED, RaffleTicketLedgerEntry.Status.EXCLUDED,
             RaffleTicketLedgerEntry.Status.DISQUALIFIED}
    if status not in valid:
        return False
    locked = RaffleTicketLedgerEntry.objects.select_for_update().get(pk=entry.pk)
    if locked.status == RaffleTicketLedgerEntry.Status.REVERSED:
        return False
    locked.status = status
    locked.save(update_fields=["status", "updated_at"])
    audit_log(actor, "raffle.ledger.status", target_type="raffle_ticket", target_id=str(locked.pk),
              metadata={"status": status, "reason": reason[:160]})
    return True


# --------------------------------------------------------------------------- #
#  Draw orchestration
# --------------------------------------------------------------------------- #
def prepare_draw(contest, actor=None, *, external_entropy: str = "") -> RaffleDraw:
    draw = draw_engine.prepare_draw(contest, executed_by=actor, external_entropy=external_entropy)
    audit_log(actor, "raffle.draw.prepared", target_type="raffle_contest", target_id=str(contest.pk),
              metadata={"draw_id": draw.pk, "commitment": draw.seed_commitment})
    return draw


def run_draw(contest, actor=None, *, external_entropy: str = "", force: bool = False) -> RaffleDraw | None:
    """Execute the draw for a closed contest — locked, idempotent, audited.

    A cross-worker Redis lock plus the draw-row compare-and-set mean a retried beat
    or a double-click can never draw twice. Returns the completed draw, or None if a
    draw was already in progress.

    If the contest configured a minimum activity level and it has NOT been reached,
    the draw is HELD — ``run_draw`` raises :class:`ActivityNotMet` — so prizes aren't
    handed out for a dead event. Leadership can override with ``force=True`` (the
    manual "draw anyway" action).
    """
    if contest.status not in (RaffleContest.Status.CLOSED, RaffleContest.Status.COMPLETED):
        raise GrantBlocked(gettext("The contest must be closed before drawing."))
    lock_key = f"raffle:draw:lock:{contest.pk}"
    if not cache.add(lock_key, "1", _DRAW_LOCK_TTL):
        return None
    try:
        # Idempotent: if a current completed draw already exists, never draw again —
        # a re-draw is the explicit, audited redraw() path (which supersedes).
        existing = contest.draws.filter(
            status=RaffleDraw.Status.COMPLETED, superseded_by__isnull=True
        ).order_by("-created_at").first()
        if existing is not None:
            return existing
        # Activity safeguard: hold the draw until the minimum activity is met, unless
        # leadership forces it.
        activity = boosters.min_activity_status(contest)
        if activity["configured"] and not activity["met"] and not force:
            raise ActivityNotMet(
                gettext("This contest hasn't reached its minimum activity "
                   "(%(metric)s: %(value)s of %(threshold)s). "
                   "Use the manual override to draw anyway.")
                % {
                    "metric": activity["label"],
                    "value": f"{activity['value']:.0f}",
                    "threshold": f"{activity['threshold']:.0f}",
                }
            )
        draw = contest.draws.filter(status=RaffleDraw.Status.COMMITTED).order_by("-created_at").first()
        if draw is None:
            draw = draw_engine.prepare_draw(contest, executed_by=actor, external_entropy=external_entropy)
        draw = draw_engine.execute_draw(draw)
        if draw.status == RaffleDraw.Status.COMPLETED:
            if contest.status == RaffleContest.Status.CLOSED:
                set_status(contest, RaffleContest.Status.COMPLETED, actor, reason="draw completed")
            _announce_winners(contest, draw)
        audit_log(actor, "raffle.draw.executed", target_type="raffle_contest",
                  target_id=str(contest.pk),
                  metadata={"draw_id": draw.pk, "winners": draw.results.count(),
                            "eligible_tickets": draw.total_eligible_tickets,
                            "excluded_tickets": draw.total_excluded_tickets,
                            "auto": actor is None, "forced_below_minimum": draw.forced_below_minimum,
                            "prize_booster": float(draw.prize_booster_percent)})
        return draw
    finally:
        cache.delete(lock_key)


@transaction.atomic
def redraw(contest, actor, *, reason: str, external_entropy: str = "") -> RaffleDraw:
    """Audited redraw: supersede the current completed draw and run a fresh one."""
    current = contest.draws.filter(status=RaffleDraw.Status.COMPLETED).order_by("-created_at").first()
    new = draw_engine.prepare_draw(contest, executed_by=actor, external_entropy=external_entropy)
    if current is not None:
        current.superseded_by = new
        current.save(update_fields=["superseded_by", "updated_at"])
    new = draw_engine.execute_draw(new)
    audit_log(actor, "raffle.draw.redraw", target_type="raffle_contest", target_id=str(contest.pk),
              metadata={"draw_id": new.pk, "superseded": getattr(current, "pk", None),
                        "reason": reason[:200]})
    _announce_winners(contest, new)
    return new


def _announce_winners(contest, draw: RaffleDraw) -> None:
    results = list(draw.results.select_related("prize"))
    if not results:
        return
    lines = [f"#{r.prize.rank} {r.prize.name} → {r.winner_character_name}" for r in results]
    notify.announce(contest, title="🏆 Raffle winners: {contest_name}",
                    body="Congratulations!\n" + "\n".join(lines),
                    template="raffle.winners",
                    context={"contest_name": contest.name, "details": "\n".join(lines)},
                    suffix="winners")
    for r in results:
        if r.winner_user_id:
            notify.notify_user(contest, r.winner_user_id,
                               title="🏆 You won {prize_name}!",
                               body=f"You won #{r.prize.rank} in “{contest.name}”. "
                                    "Leadership will be in touch about delivery.",
                               template="raffle.win",
                               context={"prize_name": r.prize.name, "prize_rank": r.prize.rank,
                                        "contest_name": contest.name},
                               suffix="win")


# --------------------------------------------------------------------------- #
#  Prize fulfilment
# --------------------------------------------------------------------------- #
@transaction.atomic
def set_fulfilment(result: RaffleDrawResult, actor, *, status: str, notes: str = "") -> None:
    result.fulfil_status = status
    result.fulfilment_notes = notes or result.fulfilment_notes
    if status == RaffleDrawResult.FulfilStatus.DELIVERED:
        result.fulfilled_by = actor
        result.fulfilled_at = timezone.now()
        _credit_recognition(result)
    result.save()
    audit_log(actor, "raffle.fulfil", target_type="raffle_result", target_id=str(result.pk),
              metadata={"status": status, "prize": result.prize.name})


def _credit_recognition(result: RaffleDrawResult) -> None:
    if not result.winner_user_id:
        return
    try:
        from apps.pilots.services import record_contribution

        # Credit the effective (booster-adjusted) value the winner actually received.
        record_contribution(
            result.winner_user, kind="raffle",
            magnitude=result.awarded_value or result.prize.estimated_value,
            # The prize's own name — the "Raffle prize:" prefix restated the kind.
            unit="isk", description=result.prize.name,
            ref_type="raffle_result", ref_id=str(result.pk), points=0,
        )
    except Exception:  # noqa: BLE001 — recognition is best-effort
        _log.debug("recognition credit failed for raffle result %s", result.pk, exc_info=True)


# --------------------------------------------------------------------------- #
#  Leaderboard / participant summaries (precomputed read model)
# --------------------------------------------------------------------------- #
@transaction.atomic
def recompute_summaries(contest) -> int:
    """Rebuild the per-account summary rows from the ledger (leaderboard read model)."""
    agg: dict[int, dict] = {}
    # amount__gt=0: a reversal is a negative APPROVED row whose reversed original is
    # already excluded (status=REVERSED), so gross-positive rows match the draw weight.
    for e in RaffleTicketLedgerEntry.objects.filter(
        contest=contest, status=RaffleTicketLedgerEntry.Status.APPROVED, amount__gt=0,
    ).values("user_id", "character_id", "character_name", "source_key", "amount", "metadata"):
        uid = e["user_id"]
        if uid is None:
            continue
        row = agg.setdefault(uid, {
            "character_id": e["character_id"], "character_name": e["character_name"],
            "total": 0, "by_source": defaultdict(int),
            "pvp_kills": 0, "pvp_participation": 0, "pvp_final_blows": 0, "pvp_solo": 0,
            "manual": 0,
        })
        row["total"] += e["amount"]
        row["by_source"][e["source_key"]] += e["amount"]
        if e["source_key"] == "manual":
            row["manual"] += e["amount"]
        if e["source_key"] == "pvp":
            md = e["metadata"] or {}
            row["pvp_kills"] += 1
            if md.get("solo"):
                row["pvp_solo"] += 1
            elif md.get("final_blow"):
                row["pvp_final_blows"] += 1
            else:
                row["pvp_participation"] += 1

    ranked = sorted(agg.items(), key=lambda kv: kv[1]["total"], reverse=True)
    bulk = elig.for_users_bulk(contest, [uid for uid, _ in ranked])
    rows = []
    for rank, (uid, r) in enumerate(ranked, start=1):
        e = bulk.get(uid) or elig.Eligibility()
        rows.append(RaffleParticipantSummary(
            contest=contest, user_id=uid, character_id=r["character_id"],
            character_name=r["character_name"], total_tickets=r["total"],
            tickets_by_source=dict(r["by_source"]),
            pvp_kills=r["pvp_kills"], pvp_participation=r["pvp_participation"],
            pvp_final_blows=r["pvp_final_blows"], pvp_solo=r["pvp_solo"],
            manual_tickets=r["manual"], eligible=e.eligible, esi_status=e.esi_status,
            exclusion_reason="" if e.eligible else (e.reason_code or ""),
            rank=rank, last_recalc_at=timezone.now(),
        ))
    RaffleParticipantSummary.objects.filter(contest=contest).delete()
    if rows:
        RaffleParticipantSummary.objects.bulk_create(rows, batch_size=500)
    cache.delete(f"raffle:board:{contest.pk}")
    return len(rows)


def dashboard_summary(user) -> dict | None:
    """Command Center raffle card: the pilot's standing in the running contest plus
    any unfulfilled prize they've won. Returns None when there's nothing to show, so
    the card only appears when the raffle is actually live for this pilot.

    Drives the adoption flywheel by reaching every logged-in pilot where they already
    look — not just those who click into /raffle/ — and makes sure a pending win is
    never missed.
    """
    from django.db.models import Sum

    from .models import RaffleContest, RaffleDrawResult, RaffleParticipantSummary

    # A pending, unfulfilled win is the highest-value thing to surface.
    pending = (
        RaffleDrawResult.objects.filter(
            winner_user=user,
            status=RaffleDrawResult.Status.WON,
            fulfil_status=RaffleDrawResult.FulfilStatus.PENDING,
        )
        .select_related("prize", "draw__contest")
        .order_by("-created_at")
        .first()
    )
    pending_win = None
    if pending is not None:
        contest = pending.draw.contest
        pending_win = {
            "prize_name": pending.prize.name,
            "contest_name": contest.name,
            "url": contest.get_absolute_url(),
        }

    # The soonest-drawing active contest + this pilot's standing in it.
    contest = (
        RaffleContest.objects.filter(status=RaffleContest.Status.ACTIVE).order_by("draw_at").first()
    )
    active = None
    if contest is not None:
        total = (
            RaffleParticipantSummary.objects.filter(contest=contest)
            .aggregate(s=Sum("total_tickets"))["s"] or 0
        )
        mine = RaffleParticipantSummary.objects.filter(contest=contest, user=user).first()
        my_tickets = mine.total_tickets if mine else 0
        # Account-level odds = my tickets / all tickets in the contest.
        odds = round(100.0 * my_tickets / total, 1) if (total and my_tickets) else 0.0
        active = {
            "name": contest.name,
            "url": contest.get_absolute_url(),
            "draw_at": contest.draw_at,
            "accruing": contest.is_accruing,
            "my_tickets": my_tickets,
            "my_rank": mine.rank if mine else 0,
            "odds_pct": odds,
            "total_tickets": total,
        }

    if active is None and pending_win is None:
        return None
    return {"active": active, "pending_win": pending_win}


def _user(uid):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=uid).first()


# --------------------------------------------------------------------------- #
#  RAF-3 (3.9): one-click enrolment outreach from the ineligible report
# --------------------------------------------------------------------------- #
_OUTREACH_EVENT_KEY = "raffle.enrolment_outreach"


def _emit_outreach(contest, user_id: int, tickets: int) -> None:
    from django.urls import reverse

    from apps.pingboard import services as pingboard
    from apps.pingboard.models import AlertCategory

    try:
        enrol_url = reverse("raffle:detail", args=[contest.slug])
        opt_out_url = reverse("raffle:outreach_opt_out")
    except Exception:  # noqa: BLE001 — never let a URL hiccup abort the run
        enrol_url, opt_out_url = "/raffle/", "/raffle/outreach/opt-out/"
    body = ngettext(
        "You earned %(tickets)d would-be raffle ticket in “%(contest)s” but "
        "aren't enrolled yet — connect your ESI token and enrol to start claiming them: "
        "%(enrol)s\n\nPrefer not to be nudged? Opt out here: %(opt_out)s",
        "You earned %(tickets)d would-be raffle tickets in “%(contest)s” but "
        "aren't enrolled yet — connect your ESI token and enrol to start claiming them: "
        "%(enrol)s\n\nPrefer not to be nudged? Opt out here: %(opt_out)s",
        tickets,
    ) % {"tickets": tickets, "contest": contest.name, "enrol": enrol_url, "opt_out": opt_out_url}
    pingboard.emit_broadcast(
        category=AlertCategory.ANNOUNCEMENT,
        title="Enrol to claim your raffle tickets",
        body=body,
        # ``ngettext`` above resolves in the *worker's* locale and freezes into ``body`` (the
        # English audit column); the scaffold — one key per English plural form — is what actually
        # re-renders this nudge in the pilot's own language at delivery.
        template=("raffle.enrolment_outreach.one" if tickets == 1
                  else "raffle.enrolment_outreach.many"),
        context={"ticket_count": tickets, "contest_name": contest.name,
                 "link": enrol_url, "opt_out_link": opt_out_url},
        audience={"kind": "user", "id": user_id},
        source_service="raffle",
        source_object_id=f"outreach:{contest.id}:{user_id}",
        idempotency_key=f"raffle:outreach:{contest.id}:{user_id}",
    )


def send_enrolment_outreach(contest, actor=None) -> dict:
    """Nudge each ranked active-but-unenrolled pilot who earned would-be tickets to enrol.

    No-spam by construction: once per (contest, pilot) [``RaffleEnrolmentOutreach`` unique],
    skip global opt-outs [``RaffleOutreachOptOut``], skip anyone a live re-check now finds
    eligible (they enrolled since), and only pilots with a linked account we can actually DM.
    Future-only — reads current ineligible activity, sends an in-app/channel DM, moves no ISK.
    """
    from django.db.models import Sum

    from apps.pingboard.notifications import is_enabled
    from apps.sso.models import EveCharacter

    if not is_enabled(_OUTREACH_EVENT_KEY):
        return {"sent": 0, "skipped": 0, "reason": "event_disabled"}

    _CAP = 200
    ranked = list(
        RaffleIneligibleActivity.objects.filter(contest=contest)
        .values("character_id", "character_name")
        .annotate(tickets=Sum("would_be_tickets"))
        .filter(tickets__gt=0)
        .order_by("-tickets")[:_CAP]
    )
    if not ranked:
        return {"sent": 0, "skipped": 0, "capped": False}
    capped = len(ranked) >= _CAP  # a full page — there may be more we didn't consider

    opted_out = set(RaffleOutreachOptOut.objects.values_list("character_id", flat=True))
    already = set(
        RaffleEnrolmentOutreach.objects.filter(contest=contest)
        .values_list("character_id", flat=True)
    )
    cids = [r["character_id"] for r in ranked]
    user_by_cid = dict(
        EveCharacter.objects.filter(character_id__in=cids, user__isnull=False)
        .values_list("character_id", "user_id")
    )
    # Opt-out is account-level: a pilot who opted out on ANY character is off-limits on all of
    # them (so a later-linked alt with ineligible activity is still honoured).
    opted_out_user_ids = set(
        EveCharacter.objects.filter(character_id__in=opted_out, user__isnull=False)
        .values_list("user_id", flat=True)
    )
    # One bulk eligibility pass (prefetched) instead of a per-pilot query — eligibility is an
    # account property, so re-check by user.
    elig_by_user = elig.for_users_bulk(contest, list(set(user_by_cid.values())))

    sent = skipped = 0
    for r in ranked:
        cid = r["character_id"]
        uid = user_by_cid.get(cid)
        if cid in already or uid is None or cid in opted_out or uid in opted_out_user_ids:
            skipped += 1
            continue
        result = elig_by_user.get(uid)
        if result is not None and result.eligible:  # enrolled since — don't nudge
            skipped += 1
            continue
        # Claim the (contest, character) slot FIRST so two concurrent runs can't double-DM:
        # the loser's get_or_create returns created=False and skips before emitting.
        record, created = RaffleEnrolmentOutreach.objects.get_or_create(
            contest=contest, character_id=cid,
            defaults={"character_name": (r["character_name"] or "")[:200],
                      "would_be_tickets": r["tickets"], "sent_by": actor},
        )
        if not created:
            skipped += 1
            continue
        try:
            _emit_outreach(contest, uid, r["tickets"])
        except Exception:  # noqa: BLE001 — one failed DM must not abort the batch
            record.delete()  # release the claim so a later run can retry this pilot
            _log.exception("enrolment outreach DM failed (contest %s char %s)", contest.id, cid)
            skipped += 1
            continue
        sent += 1

    if actor is not None:
        audit_log(actor, "raffle.enrolment_outreach", target_type="raffle_contest",
                  target_id=str(contest.id),
                  metadata={"sent": sent, "skipped": skipped, "capped": capped})
    if capped:
        _log.warning("enrolment outreach for contest %s hit the %s-pilot cap", contest.id, _CAP)
    return {"sent": sent, "skipped": skipped, "capped": capped}


def opt_out_of_outreach(user) -> int:
    """Record a permanent, cross-contest 'don't nudge me' for all of ``user``'s characters."""
    from apps.sso.models import EveCharacter

    n = 0
    for cid, name in EveCharacter.objects.filter(user=user).values_list("character_id", "name"):
        _, created = RaffleOutreachOptOut.objects.get_or_create(
            character_id=cid, defaults={"character_name": name or ""})
        n += int(created)
    return n


# --------------------------------------------------------------------------- #
#  RAF-5 (3.14): cross-contest monthly prize-spend budget guard
# --------------------------------------------------------------------------- #
def _committed_statuses() -> list:
    """Contest states whose prizes count as committed spend (draft/cancelled don't)."""
    S = RaffleContest.Status
    return [S.SCHEDULED, S.ACTIVE, S.CLOSED, S.COMPLETED, S.ARCHIVED]


def contest_prize_total(contest) -> Decimal:
    from django.db.models import Sum

    return contest.prizes.aggregate(s=Sum("estimated_value"))["s"] or Decimal("0")


def monthly_prize_spend(when=None, *, exclude_contest_id=None) -> Decimal:
    """Total committed prize value for contests drawing in ``when``'s month (default now)."""
    from django.db.models import Sum

    from .models import RafflePrize

    when = when or timezone.now()
    qs = RafflePrize.objects.filter(
        contest__draw_at__year=when.year, contest__draw_at__month=when.month,
        contest__status__in=_committed_statuses(),
    )
    if exclude_contest_id is not None:
        qs = qs.exclude(contest_id=exclude_contest_id)
    return qs.aggregate(s=Sum("estimated_value"))["s"] or Decimal("0")


def budget_status(when=None) -> dict:
    """The current month's prize-spend standing vs the leadership ceiling (0 = off)."""
    cfg = active_config()
    ceiling = cfg.monthly_prize_budget or Decimal("0")
    spent = monthly_prize_spend(when)
    if ceiling <= 0:
        return {"enabled": False, "ceiling": Decimal("0"), "spent": spent, "pct": 0,
                "state": "off", "remaining": None, "warn_pct": cfg.budget_warn_pct}
    pct = int(spent / ceiling * 100)
    state = "over" if spent > ceiling else ("warn" if pct >= cfg.budget_warn_pct else "ok")
    return {"enabled": True, "ceiling": ceiling, "spent": spent, "pct": pct, "state": state,
            "remaining": ceiling - spent, "warn_pct": cfg.budget_warn_pct}


def budget_block_reason(contest) -> str:
    """A non-empty reason if activating ``contest`` would push its draw month past the ceiling."""
    cfg = active_config()
    ceiling = cfg.monthly_prize_budget or Decimal("0")
    if ceiling <= 0 or contest.draw_at is None:
        return ""
    this_total = contest_prize_total(contest)
    projected = monthly_prize_spend(contest.draw_at, exclude_contest_id=contest.id) + this_total
    if projected > ceiling:
        return (
            f"This contest's prizes ({this_total:,.0f} ISK) would put {contest.draw_at:%B %Y} "
            f"raffle spend at {projected:,.0f} ISK — over the {ceiling:,.0f} ISK monthly "
            f"ceiling. Raise the ceiling in raffle settings or trim the prizes."
        )
    return ""
