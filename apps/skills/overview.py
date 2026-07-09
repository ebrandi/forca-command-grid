"""Per-character training overview: SP headline stats + the in-game skill queue.

ESI exposes the skill queue **read-only** (``esi-skills.read_skillqueue.v1``) — there is
no write endpoint, so the in-game queue can only be edited in the EVE client. This module
turns the already-imported :class:`SkillQueueSnapshot` into a display model (the skill
currently training with live progress, each queued skill's ETA, total queue time, and an
idle warning) and reads headline SP stats from the latest :class:`CharacterSkillSnapshot`.
The matching "apply" action a pilot *can* take from here is the clipboard export, which
pastes straight into the in-game skill planner.
"""
from __future__ import annotations

_ROMAN = {0: "0", 1: "I", 2: "II", 3: "III", 4: "IV", 5: "V"}


def _roman(level) -> str:
    try:
        return _ROMAN.get(int(level), str(level))
    except (TypeError, ValueError):
        return str(level or "")


def _parse(value):
    if not value:
        return None
    from django.utils.dateparse import parse_datetime

    return parse_datetime(value)


def is_queue_idle(character, now=None):
    """Whether a character's in-game skill queue is idle (empty or all finished).

    Returns ``True`` (idle), ``False`` (something is still/indefinitely training), or
    ``None`` when there is no queue data at all — we don't know, so callers should not
    nudge. Mirrors :func:`character_training`'s queue semantics (a finished entry is not
    training; an entry with no finish date counts as still queued) without the per-name
    SDE lookup, so it is cheap enough for a corp-wide sweep.
    """
    from django.utils import timezone

    now = now or timezone.now()
    qsnap = character.skillqueue_snapshots.filter(is_latest=True).first()
    if qsnap is None:
        return None
    for entry in (qsnap.entries or []):
        finish = _parse(entry.get("finish_date"))
        if finish is None or finish > now:
            return False  # still (or indefinitely) training
    return True


def character_training(character, now=None) -> dict:
    """Headline SP stats + the parsed in-game training queue for one character."""
    from django.utils import timezone

    from apps.characters.models import SkillQueueSnapshot  # noqa: F401 (related access)
    from apps.sde.models import SdeType

    now = now or timezone.now()

    snap = character.skill_snapshots.filter(is_latest=True).first()
    skills = (snap.skills if snap else {}) or {}
    n_at_v = sum(1 for v in skills.values() if int((v or {}).get("trained_level", 0)) == 5)

    qsnap = character.skillqueue_snapshots.filter(is_latest=True).first()
    raw = sorted((qsnap.entries if qsnap else []) or [], key=lambda e: e.get("queue_position", 0))
    name_by_id = dict(
        SdeType.objects.filter(type_id__in=[e.get("skill_id") for e in raw if e.get("skill_id")])
        .values_list("type_id", "name")
    )

    queue: list[dict] = []
    current = None
    last_finish = None
    for entry in raw:
        finish = _parse(entry.get("finish_date"))
        start = _parse(entry.get("start_date"))
        if finish and finish <= now:
            continue  # finished since the last sync — not part of what's still training
        remaining = (finish - now) if finish else None
        item = {
            "name": name_by_id.get(entry.get("skill_id"), f"Skill {entry.get('skill_id')}"),
            "level": _roman(entry.get("finished_level")),
            "finish": finish,
            "remaining_seconds": int(remaining.total_seconds()) if remaining else None,
            "is_current": False,
            "progress": None,
        }
        if current is None and start and start <= now < (finish or now):
            span = (finish - start).total_seconds()
            item["is_current"] = True
            item["progress"] = round(min(100, max(0, (now - start).total_seconds() / span * 100))) if span > 0 else 0
            current = item
        queue.append(item)
        if finish:
            last_finish = finish

    queue_remaining = (last_finish - now) if last_finish else None
    return {
        "character": character,
        "has_skills": snap is not None,
        "total_sp": snap.total_sp if snap else 0,
        "n_skills": len(skills),
        "n_at_v": n_at_v,
        "has_queue_data": qsnap is not None,
        "synced_at": getattr(qsnap, "fetched_at", None) if qsnap else None,
        "queue": queue,
        "current": current,
        "is_training": current is not None,
        "is_empty_queue": not queue,
        "queue_remaining_seconds": int(queue_remaining.total_seconds()) if queue_remaining else None,
        "queue_finish": last_finish,
    }
