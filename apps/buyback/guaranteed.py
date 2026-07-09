"""Corp-funded guaranteed buyback (4.20) — the corp commits to buy a member's lot at the
quoted price; a treasurer pays in-game and the app reconciles that payment READ-ONLY against
the corp wallet journal. The highest-risk, financial feature, so every safety rail lives
here and the whole thing is INERT until leadership arms it:

  * ``enabled`` False + ``audience`` DISABLED by default (double-off);
  * per-lot cap + rolling-24h budget ceiling enforced at approval;
  * separation of duties — an officer can't approve/settle their own request;
  * the app NEVER moves ISK — settlement is an ESI wallet-journal match (default) or an
    explicit officer confirmation.
"""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import Audience, GuaranteedBuybackConfig, GuaranteedBuyout

log = logging.getLogger("forca.buyback")
_DONATION_REF_TYPES = ("player_donation",)


def active_config() -> GuaranteedBuybackConfig:
    return GuaranteedBuybackConfig.get_solo()


def is_live(config: GuaranteedBuybackConfig | None = None) -> bool:
    """The feature does nothing at all unless BOTH the master switch is on and an audience
    is opened — the built-OFF guarantee."""
    config = config or active_config()
    return config.enabled and config.audience != Audience.DISABLED


def can_request(user, config: GuaranteedBuybackConfig | None = None) -> bool:
    """Whether this user may request a guaranteed buyout — the same audience gate the rest of
    the buyback service uses, on top of the live check."""
    config = config or active_config()
    if not is_live(config):
        return False
    from .services import _audience_allows

    return _audience_allows(user, config.audience)


def committed_last_24h(now=None) -> Decimal:
    """ISK the corp has newly committed (approved or settled) in the rolling 24h — the budget
    denominator."""
    now = now or timezone.now()
    total = (
        GuaranteedBuyout.objects.filter(
            status__in=[GuaranteedBuyout.Status.APPROVED, GuaranteedBuyout.Status.SETTLED],
            decided_at__gte=now - dt.timedelta(hours=24),
        ).aggregate(s=Sum("quoted_value"))["s"]
    )
    return total or Decimal("0")


def request_buyout(user, *, seller_character_id, items, item_count, volume_m3, jita_value,
                   quoted_value, location_name="", notes="") -> GuaranteedBuyout | None:
    """A member asks the corp to guarantee-buy a lot. Returns None if the feature is off, the
    user isn't in the audience, or the lot exceeds the per-lot cap (a member can't commit the
    corp to a whale)."""
    config = active_config()
    if not can_request(user, config):
        return None
    quoted_value = Decimal(quoted_value)
    if quoted_value <= 0 or quoted_value > config.per_lot_cap:
        return None
    return GuaranteedBuyout.objects.create(
        seller=user, seller_character_id=seller_character_id, items=items,
        item_count=item_count, volume_m3=volume_m3, jita_value=Decimal(jita_value),
        quoted_value=quoted_value, location_name=location_name[:200], notes=notes[:300],
        status=GuaranteedBuyout.Status.REQUESTED,
    )


def approval_blocker(buyout: GuaranteedBuyout, officer, config=None, now=None) -> str | None:
    """Why this officer can't approve this buyout right now, or None if they can. Pure check
    (no writes) so the queue can render disabled buttons + reasons."""
    config = config or active_config()
    now = now or timezone.now()
    if not is_live(config):
        return "Guaranteed buyback is turned off."
    if buyout.status != GuaranteedBuyout.Status.REQUESTED:
        return "This request is no longer pending."
    if buyout.seller_id and buyout.seller_id == getattr(officer, "id", None):
        # No self-approval on a real-ISK commitment — not even a superuser (MED).
        return "You can't approve your own request — another officer must."
    if buyout.quoted_value > config.per_lot_cap:
        return "Above the per-lot cap."
    if committed_last_24h(now) + buyout.quoted_value > config.daily_budget:
        return "Would exceed the rolling 24h budget."
    return None


@transaction.atomic
def approve_buyout(buyout_id: int, officer, reason: str = "") -> tuple[bool, str]:
    """Officer approves a pending request: re-checks the budget + SoD under a row lock, then
    commits the corp. Returns (ok, message)."""
    config = active_config()
    # Serialize the rolling-budget check across concurrent approvals of DIFFERENT buyouts by
    # making every approval contend on ONE lock (the singleton config row) — otherwise two
    # officers could each pass the budget check on separate row locks and overshoot (MED).
    GuaranteedBuybackConfig.objects.select_for_update().filter(pk=config.pk).first()
    buyout = (
        GuaranteedBuyout.objects.select_for_update()
        .filter(pk=buyout_id).first()
    )
    if buyout is None:
        return False, "No such request."
    blocker = approval_blocker(buyout, officer, config)
    if blocker:
        return False, blocker
    buyout.status = GuaranteedBuyout.Status.APPROVED
    buyout.decided_by = officer
    buyout.decided_at = timezone.now()
    buyout.decision_reason = reason[:200]
    buyout.save(update_fields=["status", "decided_by", "decided_at", "decision_reason", "updated_at"])
    return True, f"Approved — pay {buyout.seller_character_id or 'the seller'} and note “{buyout.payment_token}”."


@transaction.atomic
def reject_buyout(buyout_id: int, officer, reason: str = "") -> bool:
    buyout = GuaranteedBuyout.objects.select_for_update().filter(
        pk=buyout_id, status=GuaranteedBuyout.Status.REQUESTED).first()
    if buyout is None:
        return False
    buyout.status = GuaranteedBuyout.Status.REJECTED
    buyout.decided_by = officer
    buyout.decided_at = timezone.now()
    buyout.decision_reason = reason[:200]
    buyout.save(update_fields=["status", "decided_by", "decided_at", "decision_reason", "updated_at"])
    return True


@transaction.atomic
def cancel_buyout(buyout_id: int, user) -> bool:
    """The seller withdraws their own still-pending request."""
    buyout = GuaranteedBuyout.objects.select_for_update().filter(
        pk=buyout_id, seller=user, status=GuaranteedBuyout.Status.REQUESTED).first()
    if buyout is None:
        return False
    buyout.status = GuaranteedBuyout.Status.CANCELLED
    buyout.save(update_fields=["status", "updated_at"])
    return True


@transaction.atomic
def mark_settled_manual(buyout_id: int, officer, reference: str) -> tuple[bool, str]:
    """Officer confirms a corp payment out-of-band (only when ESI reconcile is off). SoD: an
    officer can't settle their own request."""
    config = active_config()
    if config.require_esi_reconcile:
        return False, "Manual settlement is off — this settles automatically from the corp wallet."
    buyout = GuaranteedBuyout.objects.select_for_update().filter(
        pk=buyout_id, status=GuaranteedBuyout.Status.APPROVED).first()
    if buyout is None:
        return False, "Not awaiting payment."
    if buyout.seller_id and buyout.seller_id == officer.id:
        return False, "You can't settle your own request."  # no self-settle, superuser included
    _settle(buyout, GuaranteedBuyout.SettlementKind.MANUAL, reference[:64])
    return True, "Marked settled."


def _settle(buyout: GuaranteedBuyout, kind: str, ref: str) -> None:
    buyout.status = GuaranteedBuyout.Status.SETTLED
    buyout.settled_at = timezone.now()
    buyout.settlement_kind = kind
    buyout.settlement_ref = ref
    buyout.save(update_fields=["status", "settled_at", "settlement_kind", "settlement_ref", "updated_at"])


def reconcile_settlements(*, limit: int = 200) -> dict:
    """Match APPROVED buyouts to a corp-wallet donation to the seller that carries the
    buyout's payment token — read-only ESI, never a coincidental donation. No-op unless the
    feature is live and set to ESI reconciliation."""
    from apps.corporation.models import CorpWalletJournalEntry

    config = active_config()
    if not (is_live(config) and config.require_esi_reconcile):
        return {"status": "disabled"}
    used = set(
        GuaranteedBuyout.objects.filter(status=GuaranteedBuyout.Status.SETTLED)
        .exclude(settlement_ref="").values_list("settlement_ref", flat=True)
    )
    settled = 0
    for buyout in GuaranteedBuyout.objects.filter(
        status=GuaranteedBuyout.Status.APPROVED
    ).order_by("decided_at")[:limit]:
        if not buyout.seller_character_id or not buyout.decided_at:
            continue
        token = buyout.payment_token
        # A donation OUT to this seller, on/after approval, carrying THIS buyout's delimited
        # token, for at LEAST the full committed quote (review MED — no underpayment slack;
        # the corp guaranteed the quoted price). ``amount`` is negative = money leaving.
        entry = (
            CorpWalletJournalEntry.objects.filter(
                ref_type__in=_DONATION_REF_TYPES,
                second_party_id=buyout.seller_character_id,
                date__gte=buyout.decided_at,
                amount__lte=-buyout.quoted_value,
                reason__icontains=token,
            ).order_by("date").first()
        )
        if entry is None or str(entry.entry_id) in used:
            continue
        _settle(buyout, GuaranteedBuyout.SettlementKind.ESI, str(entry.entry_id))
        used.add(str(entry.entry_id))
        settled += 1
    return {"settled": settled}
