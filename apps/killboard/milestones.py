"""KB-4 — newbro combat milestones (first kill / first solo / first final blow).

Reachable early wins that retain a new pilot before they can climb the prestige ladder.
Every home-corp pilot's firsts are *recorded* for display on their page, but a Pingboard
celebration only fires for a **recently** achieved first — so switching the feature on
never back-congratulates a veteran for a kill from years ago (future-only notifications).
"""
from __future__ import annotations

import datetime as dt
import logging

from django.conf import settings
from django.utils import timezone

from .models import Killmail, KillmailParticipant, PilotMilestone

logger = logging.getLogger("forca.killboard")

_EVENT_KEY = "killboard.newbro_milestone"
_NOTIFY_WINDOW_DAYS = 14  # only celebrate a first achieved within this window

_LABELS = dict(PilotMilestone.Kind.choices)
_BODY = {
    PilotMilestone.Kind.FIRST_KILL: "Congratulations on your first corp killmail! Welcome to the fight.",
    PilotMilestone.Kind.FIRST_SOLO: "Your first solo kill — you took one down all on your own. Nice work.",
    PilotMilestone.Kind.FIRST_FINAL_BLOW: "You landed your first final blow. The killing shot is yours.",
}


def _first_events(character_id: int) -> dict:
    """The character's first kill / solo / final-blow as home-corp attacker (or None each)."""
    base = KillmailParticipant.objects.filter(
        character_id=character_id,
        role=KillmailParticipant.Role.ATTACKER,
        corporation_id=settings.FORCA_HOME_CORP_ID,
        killmail__home_corp_role=Killmail.HomeRole.ATTACKER,
        killmail__is_npc=False,
    )

    def _first(qs):
        return (
            qs.order_by("killmail__killmail_time")
            .values("killmail__killmail_time", "killmail__killmail_id")
            .first()
        )

    return {
        PilotMilestone.Kind.FIRST_KILL: _first(base),
        PilotMilestone.Kind.FIRST_SOLO: _first(base.filter(killmail__is_solo=True)),
        PilotMilestone.Kind.FIRST_FINAL_BLOW: _first(base.filter(final_blow=True)),
    }


def _notify_milestone(user_id: int, milestone: PilotMilestone) -> None:
    if not user_id:
        return
    label = _LABELS.get(milestone.kind, milestone.kind)
    try:
        from apps.pingboard import services as pingboard

        pingboard.emit_broadcast(
            category="custom",
            title=f"Milestone unlocked: {label}!",
            body=_BODY.get(milestone.kind, f"You reached a milestone: {label}."),
            audience={"kind": "user", "id": user_id},
            source_service="killboard",
            source_object_id=f"milestone:{milestone.character_id}:{milestone.kind}",
            idempotency_key=f"killboard:milestone:{milestone.character_id}:{milestone.kind}",
        )
    except Exception:  # noqa: BLE001 — a notification must never break the scan
        logger.exception("milestone notification failed for character %s", milestone.character_id)


def scan_milestones() -> int:
    """Record + celebrate new newbro combat milestones. Returns the number celebrated.

    No-op when leadership turns the ``killboard.newbro_milestone`` event off. Only pilots
    still missing at least one milestone kind are scanned, so a corp of veterans is a
    cheap no-op after the first pass.
    """
    from apps.pingboard.notifications import is_enabled

    if not is_enabled(_EVENT_KEY):
        return 0

    from apps.sso.models import EveCharacter

    all_kinds = {k for k, _ in PilotMilestone.Kind.choices}
    chars = list(
        EveCharacter.objects.filter(is_corp_member=True, user__isnull=False).values(
            "character_id", "name", "user_id"
        )
    )
    if not chars:
        return 0

    existing: dict[int, set] = {}
    for m in PilotMilestone.objects.filter(
        character_id__in=[c["character_id"] for c in chars]
    ).values("character_id", "kind"):
        existing.setdefault(m["character_id"], set()).add(m["kind"])

    now = timezone.now()
    recent_cutoff = now - dt.timedelta(days=_NOTIFY_WINDOW_DAYS)
    notified = 0
    for c in chars:
        cid = c["character_id"]
        have = existing.get(cid, set())
        if have >= all_kinds:
            continue  # already has every milestone
        firsts = _first_events(cid)
        for kind, row in firsts.items():
            if kind in have or row is None:
                continue
            achieved = row["killmail__killmail_time"]
            # Mark-then-send: record (with notified_at set) first, then celebrate only a
            # recent first — so an old first is kept silently and there's no retry storm.
            # get_or_create (not create) so an overlapping/manual run can't raise an
            # IntegrityError on the unique (character_id, kind) constraint.
            milestone, created = PilotMilestone.objects.get_or_create(
                character_id=cid, kind=kind,
                defaults={
                    "character_name": c["name"] or "", "achieved_at": achieved,
                    "killmail_id": row["killmail__killmail_id"], "notified_at": now,
                },
            )
            if created and achieved >= recent_cutoff:
                _notify_milestone(c["user_id"], milestone)
                notified += 1
    return notified


def milestones_for(character_ids) -> list[PilotMilestone]:
    """A pilot's recorded milestones (for display), newest first."""
    ids = list(character_ids)
    if not ids:
        return []
    return list(PilotMilestone.objects.filter(character_id__in=ids))
