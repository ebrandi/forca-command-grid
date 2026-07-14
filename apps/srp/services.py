"""SRP logic: programme settings, eligibility, payout valuation, claim lifecycle.

Nothing here moves ISK — payouts are computed and recorded only. How a loss is
valued and what the pilot receives (a replacement hull, full ISK, or just the
gap above the official insurance) is driven by the single ``SrpProgram`` that
leadership tunes (see ``active_program``).
"""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.doctrines.models import Doctrine
from apps.doctrines.services import best_doctrine_fit
from apps.killboard.models import Killmail
from apps.market.pricing import price_for

from .models import POD_TYPE_IDS, SrpClaim, SrpProgram, SrpRule


def active_program() -> SrpProgram:
    """The live SRP programme, seeding a sensible default the first time."""
    program = SrpProgram.objects.filter(is_active=True).order_by("-updated_at").first()
    if program is None:
        program = SrpProgram.objects.create(name="Standard", is_active=True)
    return program


def _active_rule_for(doctrine: Doctrine | None) -> SrpRule | None:
    """Most specific active rule: a doctrine-specific rule beats an any-doctrine one."""
    if doctrine is not None:
        specific = SrpRule.objects.filter(active=True, doctrine=doctrine).first()
        if specific:
            return specific
    return SrpRule.objects.filter(active=True, doctrine__isnull=True).first()


def matched_doctrine(killmail: Killmail):
    """The active doctrine + fit that best matches the lost ship.

    Module-aware (4.2): for a hull with several doctrine fits, picks the variant whose
    modules best match what was actually fitted, so the DOCTRINE_FIT valuation is priced
    against the fit the pilot flew — not just the first same-hull fit by priority. Falls
    back to hull-only matching when the loss has no fitted-item data."""
    from apps.killboard.doctrine_tag import fitted_module_multiset

    fit = best_doctrine_fit(killmail.victim_ship_type_id, fitted_module_multiset(killmail))
    return (fit.doctrine, fit) if fit else (None, None)


def _doctrine_fit_value(fit) -> Decimal:
    """Hull + the doctrine fit's modules, at market."""
    value = price_for(fit.ship_type_id)
    for module in fit.modules or []:
        tid = module.get("type_id")
        if tid:
            value += price_for(int(tid)) * int(module.get("quantity", 1) or 1)
    return value


def loss_value(killmail: Killmail, fit, program: SrpProgram) -> Decimal:
    """Gross value of the loss under the programme's valuation basis.

    ``actual``  → what was destroyed (hull + destroyed modules), already priced on
                  the killmail (market values, BPCs zeroed — the accurate figure).
    ``doctrine``→ the matching doctrine fit's value; falls back to actual loss when
                  the ship isn't a doctrine hull.
    ``hull``    → the hull price only.
    """
    basis = program.valuation
    if basis == SrpProgram.Valuation.HULL_ONLY:
        return price_for(killmail.victim_ship_type_id)
    if basis == SrpProgram.Valuation.DOCTRINE_FIT and fit is not None:
        return _doctrine_fit_value(fit)
    # ACTUAL_LOSS, or DOCTRINE_FIT with no doctrine fit to value against.
    return Decimal(killmail.destroyed_value or 0)


def insurance_estimate(killmail: Killmail, program: SrpProgram) -> Decimal:
    """Assumed official in-game insurance payout for the hull (top-up mode only).

    Approximated as the hull's market value × the leadership-set fraction. (EVE's
    own per-hull insurance figures could later come from ESI /insurance/prices/;
    the tunable fraction keeps this honest and adjustable in the meantime.)
    """
    if not program.is_topup:
        return Decimal("0")
    hull = price_for(killmail.victim_ship_type_id)
    return (hull * Decimal(program.insurance_fraction)).quantize(Decimal("1"))


def _apply_cap(amount: Decimal, rule: SrpRule | None, program: SrpProgram) -> Decimal:
    """Cap a payout: a rule's own cap wins, else the programme default (0 = none)."""
    cap = Decimal("0")
    if rule and rule.max_payout:
        cap = Decimal(rule.max_payout)
    elif program.default_cap:
        cap = Decimal(program.default_cap)
    if cap and amount > cap:
        return cap
    return amount


def _net_payout(gross: Decimal, insurance: Decimal, rule: SrpRule | None,
                program: SrpProgram) -> Decimal:
    """The SRP payout: gross minus any insurance offset, then capped, floored at 0.

    Fixed-cap rules short-circuit to their flat amount. Replacement mode keeps the
    gross value as an informational figure (the corp hands over a hull, not ISK).
    """
    if rule and rule.basis == SrpRule.Basis.FIXED:
        return Decimal(rule.max_payout)
    amount = gross
    if program.is_topup:
        amount = max(Decimal("0"), gross - insurance)
    return _apply_cap(amount, rule, program)


def loss_on_sanctioned_op(killmail: Killmail, program: SrpProgram):
    """The sanctioned operation whose window covers this loss, or ``None`` (SRP-1 / 2.8).

    A *sanctioned* op is SRP-covered (``srp`` != none) and not draft/cancelled. The window
    is the op's ``target_at`` extended by its ``duration_minutes`` (or the programme default),
    with ``fleet_op_grace_minutes`` of slack on each side for form-up/travel. When
    ``fleet_op_require_attendance`` is on, the pilot must also have a recorded PAP on that op.
    """
    from datetime import timedelta

    from django.db.models import Max

    from apps.operations.models import Operation, OperationAttendance

    t = killmail.killmail_time
    if t is None or killmail.victim_character_id is None:
        return None

    grace = timedelta(minutes=program.fleet_op_grace_minutes)
    default_dur = timedelta(minutes=program.fleet_op_default_duration_minutes)
    # Prefilter to ops that plausibly cover ``t``: started no later than ``grace`` after it,
    # and no earlier than the longest window any op could have before it. The floor must bound
    # the *real* max duration (a deployment / war-prep can run days) or a long op that still
    # covers ``t`` would be dropped here and a valid claim wrongly denied.
    max_explicit = (
        Operation.objects.filter(duration_minutes__isnull=False)
        .aggregate(m=Max("duration_minutes"))["m"]
    )
    longest = max(
        default_dur,
        timedelta(hours=24),
        timedelta(minutes=max_explicit) if max_explicit else timedelta(0),
    )
    candidates = (
        Operation.objects.filter(
            target_at__isnull=False,
            target_at__gte=t - grace - longest,
            target_at__lte=t + grace,
        )
        .filter(srp__in=[
            Operation.Srp.ALLIANCE, Operation.Srp.CORP, Operation.Srp.ORGANISER,
        ])  # a positive SRP designation only — blank/"none" ops don't sanction a loss
        .exclude(status__in=[
            Operation.Status.DRAFT, Operation.Status.CANCELLED, Operation.Status.CANCELLED_AUTO,
        ])
        .order_by("-target_at")
    )
    for op in candidates:
        dur = timedelta(minutes=op.duration_minutes) if op.duration_minutes else default_dur
        if not (op.target_at - grace <= t <= op.target_at + dur + grace):
            continue
        if program.fleet_op_require_attendance and not OperationAttendance.objects.filter(
            operation=op, character_id=killmail.victim_character_id, confirmed=True
        ).exists():
            # OPS-2 (3.1) invariant: only CONFIRMED / ESI-verified PAP satisfies the gate —
            # a bare self-report must not unlock SRP eligibility.
            continue
        return op
    return None


def eligibility(killmail: Killmail, program: SrpProgram | None = None) -> dict:
    """Explainable eligibility + payout for a single loss under the programme."""
    program = program or active_program()
    if not program.enabled:
        return {"eligible": False, "reason": _("SRP is currently paused by leadership.")}
    if not (killmail.involves_home_corp and killmail.home_corp_role == Killmail.HomeRole.VICTIM):
        return {"eligible": False, "reason": _("Not a corp loss.")}
    if killmail.victim_ship_type_id in POD_TYPE_IDS and not program.cover_pod:
        return {"eligible": False, "reason": _("Pod losses aren't covered.")}

    doctrine, fit = matched_doctrine(killmail)
    if program.require_doctrine and not fit:
        return {"eligible": False, "reason": _("Loss isn't an active doctrine hull.")}

    rule = _active_rule_for(doctrine)
    if program.require_doctrine and not rule:
        return {"eligible": False, "reason": _("No active SRP rule."), "doctrine": doctrine}

    operation = None
    if program.require_fleet_op:
        operation = loss_on_sanctioned_op(killmail, program)
        if operation is None:
            return {"eligible": False, "reason": _("Loss wasn't on a sanctioned fleet op."),
                    "doctrine": doctrine}

    gross = loss_value(killmail, fit, program)
    insurance = insurance_estimate(killmail, program)
    payout = _net_payout(gross, insurance, rule, program)
    return {
        "eligible": True,
        "doctrine": doctrine,
        "fit": fit,
        "rule": rule,
        "operation": operation,
        "loss_value": gross,
        "insurance_estimate": insurance,
        "payout": payout,
        "payout_mode": program.payout_mode,
        "explanation": _explain(doctrine, program, gross, insurance, payout),
    }


def _explain(doctrine, program: SrpProgram, gross: Decimal, insurance: Decimal,
             payout: Decimal) -> str:
    """One-line human summary of how this claim was valued."""
    scope = doctrine.name if doctrine else "Non-doctrine loss"
    if program.is_replacement:
        return f"{scope} → replacement ship & fit (≈{gross:,.0f} ISK)"
    if program.is_topup:
        return (f"{scope} → {gross:,.0f} loss − {insurance:,.0f} insurance "
                f"= {payout:,.0f} ISK")
    return f"{scope} → {payout:,.0f} ISK"


def eligible_losses_for(char_ids, limit: int = 25) -> list[dict]:
    """A pilot's own losses that are eligible and not yet claimed."""
    program = active_program()
    if not program.enabled:
        return []
    claimed = set(SrpClaim.objects.values_list("killmail_id", flat=True))
    out = []
    qs = Killmail.objects.filter(
        involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.VICTIM,
        victim_character_id__in=list(char_ids),
    ).prefetch_related("items").order_by("-killmail_time")[: limit * 3]  # items: 4.2 module match
    for km in qs:
        if km.killmail_id in claimed:
            continue
        info = eligibility(km, program)
        if info.get("eligible"):
            out.append({"killmail": km, **info})
        if len(out) >= limit:
            break
    return out


def _persist_claim(killmail: Killmail, user, info: dict, *, auto_drafted: bool = False,
                   notify: bool = True) -> SrpClaim:
    """Create the SUBMITTED SrpClaim + (optionally) fire the submitted hook. Shared by the
    pilot's manual submit and the auto-draft sweep (4.6) so both build an identical claim;
    the only difference is the ``auto_drafted`` flag. Never sets a paid/approved status.

    ``notify=False`` skips the per-claim ``srp.submitted`` hook — the auto-draft sweep uses
    it so a first/post-arm batch can't fire up to ``limit`` officer alerts at once (review
    LOW); those drafts surface in the queue (auto-drafted badge) + the SRP SLA digest."""
    rule = info.get("rule")
    claim = SrpClaim.objects.create(
        killmail=killmail,
        claimant=user,
        status=SrpClaim.Status.SUBMITTED,
        auto_drafted=auto_drafted,
        basis=rule.basis if rule else SrpRule.Basis.FIT,
        payout_mode=info["payout_mode"],
        loss_value=info["loss_value"],
        insurance_estimate=info["insurance_estimate"],
        computed_payout=info["payout"],
        doctrine=info["doctrine"],
        explanation=info["explanation"],
    )
    if notify:
        from apps.pingboard import hooks

        hooks.fire("srp.submitted", source_object_id=claim.id, dedup_suffix="submitted",
                   context={"pilot_name": getattr(user, "display_name", "") or user.get_username(),
                            "ship_name": getattr(info.get("doctrine"), "name", "") or "",
                            "isk": str(info["payout"])})
    return claim


@transaction.atomic
def submit_claim(user, killmail: Killmail, char_ids) -> SrpClaim | None:
    """A pilot submits a claim for their own eligible loss. Returns None if invalid."""
    if killmail.victim_character_id not in set(char_ids):
        return None
    if SrpClaim.objects.filter(killmail=killmail).exists():
        return None
    info = eligibility(killmail)
    if not info.get("eligible"):
        return None
    try:
        return _persist_claim(killmail, user, info, auto_drafted=False)
    except IntegrityError:
        return None  # a concurrent sweep/tab won the per-killmail unique constraint → "already exists"


@transaction.atomic
def decide(claim: SrpClaim, officer, approve: bool, reason: str = "",
           approved_payout: Decimal | None = None) -> bool:
    """Officer approves or denies a *submitted* claim, optionally adjusting payout.

    Two controls apply to this ISK-accountability step:
      * Separation of duties — an officer may not decide their own claim
        (raises ``PermissionDenied``); a superuser is exempt as a break-glass.
      * State guard — only a ``SUBMITTED`` claim can be decided. The row is locked
        and re-read so a PAID/DENIED claim cannot be re-decided. Returns ``True``
        if the decision was applied, ``False`` if the claim had already left the
        submitted state (benign race / double click).

    ``approved_payout`` (approvals only) overrides the computed figure — leadership
    discretion, e.g. a partial payout or a negotiated amount.
    """
    if claim.claimant_id == officer.id and not officer.is_superuser:
        raise PermissionDenied("You cannot decide your own SRP claim.")
    locked = SrpClaim.objects.select_for_update().get(pk=claim.pk)
    if locked.status != SrpClaim.Status.SUBMITTED:
        return False
    locked.status = SrpClaim.Status.APPROVED if approve else SrpClaim.Status.DENIED
    fields = ["status", "decided_by", "reason", "decided_at", "updated_at"]
    if approve and approved_payout is not None and approved_payout >= 0:
        locked.approved_payout = approved_payout
        fields.append("approved_payout")
    locked.decided_by = officer
    locked.reason = reason
    locked.decided_at = timezone.now()
    locked.save(update_fields=fields)
    claim.status = locked.status  # keep the caller's instance in sync for messaging
    from apps.pingboard import hooks

    hooks.fire("srp.approved" if approve else "srp.denied", source_object_id=claim.id,
               dedup_suffix=locked.status, context={"target_user_id": claim.claimant_id})
    return True


@transaction.atomic
def mark_paid(claim: SrpClaim, officer, reference: str = "") -> bool:
    """Record that an approved claim was settled (no ISK is moved by the app).

    Separation of duties applies here too — an officer may not pay out their own
    claim. The row is locked and must still be ``APPROVED``; returns ``True`` on
    success, ``False`` if it was no longer awaiting payment. ``reference`` records
    how it was settled (wallet note, or what hull was handed over).
    """
    if claim.claimant_id == officer.id and not officer.is_superuser:
        raise PermissionDenied("You cannot pay out your own SRP claim.")
    locked = SrpClaim.objects.select_for_update().get(pk=claim.pk)
    if locked.status != SrpClaim.Status.APPROVED:
        return False
    locked.status = SrpClaim.Status.PAID
    locked.payment_reference = reference
    locked.decided_by = officer
    locked.decided_at = timezone.now()
    locked.save(update_fields=["status", "payment_reference", "decided_by",
                               "decided_at", "updated_at"])
    claim.status = locked.status
    # Credit the pilot's SRP contribution ledger (recognition of replaced ships).
    from apps.pilots.services import record_contribution

    record_contribution(
        claim.claimant,
        kind="srp",
        magnitude=claim.payout,
        unit="isk",
        # The loss this paid for. "SRP for" restated the kind ("Ship replacement"), which the
        # ledger already renders translated beside this line — the killmail ref is the only
        # thing here the kind cannot convey.
        description=f"#{claim.killmail_id}",
        ref_type="srp_claim",
        ref_id=str(claim.pk),
    )
    from apps.pingboard import hooks

    hooks.fire("srp.paid", source_object_id=claim.id, dedup_suffix="paid",
               context={"target_user_id": claim.claimant_id, "isk": str(claim.payout)})
    return True


def exposure() -> Decimal:
    """Open SRP liability: submitted + approved-but-unpaid payouts."""
    rows = SrpClaim.objects.filter(
        status__in=[SrpClaim.Status.SUBMITTED, SrpClaim.Status.APPROVED]
    )
    return sum((c.payout for c in rows), start=Decimal("0"))


def spent_for_period(period: str) -> Decimal:
    """ISK actually paid out in a calendar month, derived live from PAID claims.

    ``SrpBudget`` stores only the allocation; spend is computed here so it never
    drifts out of sync. ``mark_paid`` stamps ``decided_at`` at pay time, so
    attributing PAID claims by that month is correct. ``period`` is the ``%Y-%m``
    key the readiness SRP dimension also uses.
    """
    try:
        year, month = int(period[:4]), int(period[5:7])
    except (ValueError, IndexError):
        return Decimal("0")
    rows = SrpClaim.objects.filter(
        status=SrpClaim.Status.PAID, decided_at__year=year, decided_at__month=month
    )
    return sum((c.payout for c in rows), start=Decimal("0"))


# --------------------------------------------------------------------------- #
#  SRP-4 (3.17): batch review after a fleet wipe
# --------------------------------------------------------------------------- #
def batch_approve(officer) -> dict:
    """Approve every SUBMITTED claim the officer is allowed to, in one action — for the dozens
    of claims a single bad fight produces.

    Reuses ``decide()`` per claim, so each is still locked, validated and audited individually,
    and separation of duties is preserved: a claim the officer filed themselves raises
    ``PermissionDenied`` and is skipped (another officer must review it).
    """
    approved_ids: list[int] = []
    skipped = 0
    for claim in list(SrpClaim.objects.filter(status=SrpClaim.Status.SUBMITTED)):
        try:
            if decide(claim, officer, approve=True):
                approved_ids.append(claim.pk)
        except PermissionDenied:
            skipped += 1
    return {"approved": len(approved_ids), "skipped": skipped, "claim_ids": approved_ids}


def batch_pay(officer, reference: str = "") -> dict:
    """Settle every APPROVED claim against a single ``reference`` (one settlement for the whole
    op), skipping the officer's own claims (SoD). Reuses ``mark_paid()`` per claim."""
    paid_ids: list[int] = []
    skipped = 0
    for claim in list(SrpClaim.objects.filter(status=SrpClaim.Status.APPROVED)):
        try:
            if mark_paid(claim, officer, reference=reference):
                paid_ids.append(claim.pk)
        except PermissionDenied:
            skipped += 1
    return {"paid": len(paid_ids), "skipped": skipped, "claim_ids": paid_ids}
