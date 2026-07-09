"""KB-1 — one-time combat rank-up celebrations via Pingboard.

Distinct from the *reward* engine (``rewards.py``, which only fires for reward-bearing
rungs and creates a pending ISK/PLEX event): this fires the pilot-facing "you made
<rank>!" note when a pilot climbs the ladder — one notification per scan for the
**highest** new rung reached since the last run (a pilot who jumps two rungs overnight
gets a single DM for the top one, never a burst) — and only adds a "reward pending" line
when leadership has armed the reward engine, the rung carries a configured reward, and
the pilot is reward-eligible (enrolled + healthy ESI token).

Future-only: a pilot seen for the first time is *baselined silently* at their current
rung — enabling the feature never back-notifies ranks earned before it existed. The
``PilotRankNotification.last_notified_min_kills`` threshold is monotonic up the ladder,
so a rank-up is simply a higher threshold than the one on file; advancing it makes a
repeat impossible, and the per-rung ``idempotency_key`` on the alert is a second guard.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from . import ranks
from .models import PilotRankNotification, RankRewardSettings
from .ranks import _reward_configured
from .rewards import all_time_kills_map

logger = logging.getLogger("forca.killboard")

_EVENT_KEY = "killboard.rank_up"


def _rewards_note(cur: dict, ladder: list[dict], rewards_armed: bool) -> str:
    """" A rank reward is pending…" — only when rewards are armed AND this rung carries a
    configured reward. Never states a payment as made (rewards are approved by hand)."""
    if not rewards_armed:
        return ""
    entry = next((e for e in ladder if e["min_kills"] == cur["min_kills"]), None)
    if entry and _reward_configured(entry):
        return " A rank reward is pending leadership approval."
    return ""


def _send_rank_up(character_id: int, user_id: int, cur: dict, kills: int, note: str) -> bool:
    """Best-effort per-pilot DM. Returns True if the alert was emitted (or already was)."""
    if not user_id:
        return False
    rank_name = cur["name"]
    body = (
        f"Congratulations — you've reached the rank of {rank_name} "
        f"({kills:,} lifetime kills recorded).{note} "
        "Ranks update from the nightly combat rollup, so this can reflect kills from the "
        "last day or two rather than the exact moment."
    )
    try:
        from apps.pingboard import services as pingboard

        pingboard.emit_broadcast(
            category="custom",
            title=f"Combat rank achieved: {rank_name}",
            body=body,
            audience={"kind": "user", "id": user_id},
            source_service="killboard",
            source_object_id=f"rankup:{character_id}:{cur['min_kills']}",
            idempotency_key=f"killboard:rankup:{character_id}:{cur['min_kills']}",
        )
        return True
    except Exception:  # noqa: BLE001 — a notification problem must never break the scan
        logger.exception("rank-up notification failed for character %s", character_id)
        return False


def notify_rank_ups() -> int:
    """Notify enrolled pilots who climbed to a new combat rung since last seen.

    Returns the number of rank-up notifications sent. No-op when leadership has turned
    the ``killboard.rank_up`` event off in the notifications console.
    """
    from apps.pingboard.notifications import is_enabled

    if not is_enabled(_EVENT_KEY):
        return 0

    ladder = ranks.active_ladder()
    if not ladder:
        return 0

    from apps.sso.models import EveCharacter

    chars = list(
        EveCharacter.objects.filter(is_corp_member=True, user__isnull=False).values(
            "character_id", "name", "user_id"
        )
    )
    if not chars:
        return 0

    kills_map = all_time_kills_map()
    rewards_armed = RankRewardSettings.load().rewards_enabled
    # A "reward pending" note is only honest for pilots the reward engine will actually
    # create an event for — i.e. enrolled AND holding a healthy ESI token. A linked
    # member whose token later lapsed would otherwise be promised a reward
    # ``scan_and_award`` never generates. Compute the eligible set once (skip the cost
    # entirely when rewards are off).
    from .rewards import enrolled_eligible_character_ids

    reward_eligible = enrolled_eligible_character_ids() if rewards_armed else set()
    cids = [c["character_id"] for c in chars]
    trackers = {
        t.character_id: t
        for t in PilotRankNotification.objects.filter(character_id__in=cids)
    }

    now = timezone.now()
    new_baselines: list[PilotRankNotification] = []
    notified = 0
    for c in chars:
        cid, name, uid = c["character_id"], c["name"] or "", c["user_id"]
        kills = kills_map.get(cid, 0)
        cur = ranks.combat_rank(kills, ladder)
        cur_min = cur["min_kills"]
        tracker = trackers.get(cid)
        if tracker is None:
            # First sight → silent baseline (future-only, no retroactive spam).
            new_baselines.append(
                PilotRankNotification(
                    character_id=cid, character_name=name,
                    last_notified_min_kills=cur_min, last_notified_rank_name=cur["name"],
                    last_notified_at=None,
                )
            )
            continue
        if cur_min <= tracker.last_notified_min_kills:
            continue  # no new rung
        note = _rewards_note(cur, ladder, rewards_armed and cid in reward_eligible)
        if _send_rank_up(cid, uid, cur, kills, note):
            notified += 1
        # Advance the tracker even if the pilot has no linked user to DM — the rung is
        # "seen", so we never re-evaluate it. A future link starts from here (future-only).
        tracker.last_notified_min_kills = cur_min
        tracker.last_notified_rank_name = cur["name"]
        tracker.last_notified_at = now
        tracker.character_name = name
        tracker.save(update_fields=[
            "last_notified_min_kills", "last_notified_rank_name",
            "last_notified_at", "character_name", "updated_at",
        ])

    if new_baselines:
        PilotRankNotification.objects.bulk_create(new_baselines, ignore_conflicts=True)
    return notified
