"""Watchlist intel: recent killboard activity for watched entities.

A watched entity (character / corporation / alliance) "appears" in a killmail as
a participant. We surface its recent kills and losses so officers can read an
adversary's tempo without leaving the app.
"""
from __future__ import annotations

from django.db.models import Count, Q

from .models import Killmail, KillmailParticipant, WatchlistEntry

_FIELD = {
    WatchlistEntry.EntityType.CHARACTER: "character_id",
    WatchlistEntry.EntityType.CORPORATION: "corporation_id",
    WatchlistEntry.EntityType.ALLIANCE: "alliance_id",
}


def entry_activity(entry: WatchlistEntry, limit: int = 10) -> dict:
    """Recent killmails and a kill/loss tally for one watched entity.

    Everything stays in SQL, deliberately. A watchlist entry can name a whole alliance,
    which has touched tens of thousands of mails; the previous shape pulled that entire
    id set into Python and sent it straight back as an ``IN (…)`` bind list, so the row
    set crossed the wire twice for a page that renders ``limit`` rows. The recent-mail
    lookup now hands the participant query to Postgres as a subquery, and the three
    tallies collapse into one FILTERed aggregate. The numbers are unchanged: ``total``
    is still the count of DISTINCT mails the entity appears on (not participant rows),
    which is why every count carries ``distinct=True`` — an entity that fielded five
    pilots on one mail must still count that mail once.
    """
    field = _FIELD[entry.entity_type]
    parts = KillmailParticipant.objects.filter(**{field: entry.entity_id})
    killmails = list(
        Killmail.objects.filter(killmail_id__in=parts.values("killmail_id"))
        .order_by("-killmail_time")[:limit]
    )
    tally = parts.aggregate(
        kills=Count(
            "killmail_id", distinct=True,
            filter=Q(role=KillmailParticipant.Role.ATTACKER),
        ),
        losses=Count(
            "killmail_id", distinct=True,
            filter=Q(role=KillmailParticipant.Role.VICTIM),
        ),
        total=Count("killmail_id", distinct=True),
    )
    return {
        "killmails": killmails,
        "kills": tally["kills"] or 0,
        "losses": tally["losses"] or 0,
        "total": tally["total"] or 0,
    }


def watchlist_overview(watchlist, per_entry: int = 3) -> list[dict]:
    """Per-entry activity summary for a whole watchlist."""
    return [
        {"entry": entry, **entry_activity(entry, limit=per_entry)}
        for entry in watchlist.entries.all()
    ]
