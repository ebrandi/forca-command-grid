"""Watchlist intel: recent killboard activity for watched entities.

A watched entity (character / corporation / alliance) "appears" in a killmail as
a participant. We surface its recent kills and losses so officers can read an
adversary's tempo without leaving the app.
"""
from __future__ import annotations

from .models import Killmail, KillmailParticipant, WatchlistEntry

_FIELD = {
    WatchlistEntry.EntityType.CHARACTER: "character_id",
    WatchlistEntry.EntityType.CORPORATION: "corporation_id",
    WatchlistEntry.EntityType.ALLIANCE: "alliance_id",
}


def entry_activity(entry: WatchlistEntry, limit: int = 10) -> dict:
    """Recent killmails and a kill/loss tally for one watched entity."""
    field = _FIELD[entry.entity_type]
    parts = KillmailParticipant.objects.filter(**{field: entry.entity_id})
    km_ids = list(parts.values_list("killmail_id", flat=True).distinct())
    killmails = list(
        Killmail.objects.filter(killmail_id__in=km_ids).order_by("-killmail_time")[:limit]
    )
    kills = parts.filter(role=KillmailParticipant.Role.ATTACKER).values("killmail_id").distinct().count()
    losses = parts.filter(role=KillmailParticipant.Role.VICTIM).values("killmail_id").distinct().count()
    return {"killmails": killmails, "kills": kills, "losses": losses, "total": len(km_ids)}


def watchlist_overview(watchlist, per_entry: int = 3) -> list[dict]:
    """Per-entry activity summary for a whole watchlist."""
    return [
        {"entry": entry, **entry_activity(entry, limit=per_entry)}
        for entry in watchlist.entries.all()
    ]
