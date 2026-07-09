"""SKL-4 — opt-in idle-skill-queue nudges via Pingboard.

A pilot who opts in (``PilotPreference.notify_idle_queue``) gets **one** DM when a
character's in-game skill queue runs dry, so they stop bleeding SP — and not another
until the queue is training again and then empties once more (a per-empty-transition
reminder, never a repeated nag).

Staleness guard: the corp-wide skill sync can be up to ~12 h old, so a character that
*looks* idle in the stored snapshot is re-checked with a fresh single-character
skillqueue pull before nudging. Only characters that already look idle are refreshed,
so the ESI cost is bounded to the (small) idle set among opted-in pilots.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from .models import IdleQueueNudge
from .overview import is_queue_idle

logger = logging.getLogger("forca.skills")

_EVENT_KEY = "skills.idle_queue"


def _confirm_idle(character):
    """Re-check idleness with a fresh pull when the stored snapshot looks idle.

    Returns the (possibly refreshed) idle verdict: ``True``/``False``/``None``.
    """
    idle = is_queue_idle(character)
    if idle is not True:
        return idle  # training or unknown — no need to spend an ESI call
    try:
        from apps.characters.services import import_character_skillqueue

        import_character_skillqueue(character)
    except Exception:  # noqa: BLE001 — no token/scope/ESI hiccup → fall back to the snapshot
        logger.debug("idle-queue fresh check failed for %s; using stored snapshot",
                     character.character_id)
    return is_queue_idle(character)


def _send_idle_nudge(character, tracker) -> None:
    """Best-effort per-pilot idle-queue DM (targeted, never corp-wide)."""
    try:
        from apps.pingboard import services as pingboard

        pingboard.emit_broadcast(
            category="custom",
            title=f"Your skill queue is empty — {character.name}",
            body=(
                f"{character.name}'s training queue has run dry, so it's not earning SP. "
                "Queue your next skills in the EVE client (the Skills page has a plan you "
                "can paste in). This is based on the last skill sync, so ignore it if you "
                "just queued something."
            ),
            audience={"kind": "user", "id": character.user_id},
            source_service="skills",
            # The tracker PK is minted fresh each idle period, so both the dedup hash
            # (category+audience+body+source_object_id) and the idempotency key are
            # unique per empty→ transition — a genuine later idle period nudges again,
            # while a same-period retry is still de-duplicated.
            source_object_id=f"idle:{character.character_id}:{tracker.pk}",
            idempotency_key=f"skills:idle:{character.character_id}:{tracker.pk}",
        )
    except Exception:  # noqa: BLE001 — a notification must never break the sweep
        logger.exception("idle-queue nudge failed for character %s", character.character_id)


def notify_idle_queues() -> int:
    """DM opted-in pilots one reminder per character whose skill queue just ran dry.

    Returns the number of nudges sent. No-op when leadership turns the
    ``skills.idle_queue`` event off or nobody has opted in.
    """
    from apps.pingboard.notifications import is_enabled

    if not is_enabled(_EVENT_KEY):
        return 0

    from apps.pilots.models import PilotPreference

    opted_user_ids = set(
        PilotPreference.objects.filter(notify_idle_queue=True).values_list("user_id", flat=True)
    )
    if not opted_user_ids:
        return 0

    from apps.sso.models import EveCharacter

    characters = list(
        EveCharacter.objects.filter(user_id__in=opted_user_ids, is_corp_member=True)
    )
    if not characters:
        return 0

    now = timezone.now()
    trackers = {
        t.character_id: t
        for t in IdleQueueNudge.objects.filter(
            character_id__in=[c.character_id for c in characters]
        )
    }
    sent = 0
    for ch in characters:
        idle = _confirm_idle(ch)
        tracker = trackers.get(ch.character_id)
        if idle is True:
            if tracker is None:
                # get_or_create (not create) so two overlapping sweeps can't raise an
                # IntegrityError on the unique character_id — the loser reuses the row.
                tracker, _ = IdleQueueNudge.objects.get_or_create(character_id=ch.character_id)
            if tracker.notified_at is not None:
                continue  # already nudged this idle period (incl. by a concurrent run)
            # Mark-then-send: persist the mark first so a delivery fault never causes a
            # retry storm, and the row's PK keys the alert idempotency.
            tracker.notified_at = now
            tracker.save(update_fields=["notified_at"])
            _send_idle_nudge(ch, tracker)
            sent += 1
        elif idle is False and tracker is not None:
            # Queue is training again → reset so the next empty transition nudges anew.
            tracker.delete()
        # idle is None (never synced) → do nothing
    return sent
