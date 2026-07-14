"""Anti-abuse / integrity scanning.

Flags (never silently deletes) suspicious ticket events for officer review. A flag
is advisory: tickets keep counting until an officer upholds the flag, which
disqualifies the entry (an audited status change). Thresholds are per-contest
(``RaffleContest.anti_abuse`` JSON) so leaders tune sensitivity.
"""
from __future__ import annotations

from collections import defaultdict

from .models import RaffleSuspiciousActivityFlag, RaffleTicketLedgerEntry

DEFAULTS = {
    "repeated_victim_limit": 5,   # same victim farmed this many times → flag
    "low_value_isk": 1_000_000,   # PVP tickets from kills under this ISK → flag
    "enabled": True,
}


def _cfg(contest, key):
    return (contest.anti_abuse or {}).get(key, DEFAULTS[key])


def scan_contest(contest) -> int:
    """Scan a contest's PVP ledger for suspicious patterns. Returns #new flags."""
    if not _cfg(contest, "enabled"):
        return 0
    from apps.killboard.models import Killmail

    pvp = list(
        RaffleTicketLedgerEntry.objects.filter(
            contest=contest, source_key="pvp",
            status=RaffleTicketLedgerEntry.Status.APPROVED,
        ).values("id", "character_id", "user_id", "metadata")
    )
    if not pvp:
        return 0

    # Resolve killmail_id → victim for repeated-victim detection.
    km_ids = {int((e["metadata"] or {}).get("killmail_id") or 0) for e in pvp}
    km_ids.discard(0)
    victims = dict(
        Killmail.objects.filter(killmail_id__in=km_ids)
        .values_list("killmail_id", "victim_character_id")
    )

    existing = {
        (f.ledger_entry_id, f.flag_type)
        for f in RaffleSuspiciousActivityFlag.objects.filter(contest=contest)
    }
    low_value_isk = _cfg(contest, "low_value_isk")
    repeat_limit = _cfg(contest, "repeated_victim_limit")

    new_flags: list[RaffleSuspiciousActivityFlag] = []
    victim_counts: dict[tuple[int, int], list[dict]] = defaultdict(list)

    for e in pvp:
        md = e["metadata"] or {}
        # Low-value kill.
        try:
            value = float(md.get("value", 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if low_value_isk and value and value < low_value_isk:
            keyf = (e["id"], RaffleSuspiciousActivityFlag.FlagType.LOW_VALUE)
            if keyf not in existing:
                # Seam B (apps/raffle/messages.py): persist the English prose (audit record +
                # fallback) AND a scaffold key/params, so the reviewing officer sees this in
                # THEIR locale. This runs on the beat — no user, no locale — so a gettext_lazy
                # proxy would be coerced to English at bulk_create and frozen for every reader.
                # The numbers are pre-formatted so the English stays byte-identical.
                new_flags.append(RaffleSuspiciousActivityFlag(
                    contest=contest, character_id=e["character_id"], user_id=e["user_id"],
                    ledger_entry_id=e["id"], flag_type=RaffleSuspiciousActivityFlag.FlagType.LOW_VALUE,
                    detail=f"Kill worth {value:,.0f} ISK (< {low_value_isk:,.0f}).",
                    detail_key="integrity.low_value",
                    detail_params={"value": f"{value:,.0f}", "limit": f"{low_value_isk:,.0f}"},
                ))
                existing.add(keyf)
        km = int(md.get("killmail_id") or 0)
        victim = victims.get(km)
        if victim:
            victim_counts[(e["character_id"], victim)].append(e)

    # Repeated-victim farming.
    for (_pilot, _victim), entries in victim_counts.items():
        if len(entries) < repeat_limit:
            continue
        for e in entries:
            keyf = (e["id"], RaffleSuspiciousActivityFlag.FlagType.REPEATED_VICTIM)
            if keyf not in existing:
                new_flags.append(RaffleSuspiciousActivityFlag(
                    contest=contest, character_id=e["character_id"], user_id=e["user_id"],
                    ledger_entry_id=e["id"],
                    flag_type=RaffleSuspiciousActivityFlag.FlagType.REPEATED_VICTIM,
                    detail=f"Same victim killed {len(entries)}× (limit {repeat_limit}).",
                    detail_key="integrity.repeated_victim",
                    detail_params={"count": len(entries), "limit": int(repeat_limit)},
                ))
                existing.add(keyf)

    if new_flags:
        RaffleSuspiciousActivityFlag.objects.bulk_create(new_flags, batch_size=500)
    return len(new_flags)


def resolve_flag(flag: RaffleSuspiciousActivityFlag, actor, *, uphold: bool, resolution: str = "") -> None:
    """Officer decision: uphold (disqualify the ticket) or dismiss (keep it). Audited."""
    from django.utils import timezone

    from core.audit import audit_log

    from . import services

    flag.status = (
        RaffleSuspiciousActivityFlag.Status.UPHELD if uphold
        else RaffleSuspiciousActivityFlag.Status.DISMISSED
    )
    flag.reviewed_by = actor
    flag.reviewed_at = timezone.now()
    flag.resolution = resolution
    flag.save(update_fields=["status", "reviewed_by", "reviewed_at", "resolution", "updated_at"])
    if uphold and flag.ledger_entry_id:
        services.set_entry_status(
            flag.ledger_entry, actor, RaffleTicketLedgerEntry.Status.DISQUALIFIED,
            reason=f"suspicious: {flag.flag_type}",
        )
    audit_log(actor, "raffle.flag.resolve", target_type="raffle_flag", target_id=str(flag.pk),
              metadata={"uphold": uphold, "type": flag.flag_type})
