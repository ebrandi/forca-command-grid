"""KB-37 (WS-D3) — the trophy award engine: criteria DSL, evaluation, and pod-loss coaching.

The trophy catalogue is DB-configurable (``TrophyDefinition``), exactly like the combat rank
ladder. This module owns everything downstream of that config:

* **Criteria DSL** — each definition's ``criteria`` JSON is a tiny, documented rule evaluated by
  :func:`progress_for`. Supported metrics:

    ``{"metric": "kills", "threshold": 100}``            — all-time PvP kills
    ``{"metric": "solo_kills", "threshold": 10}``        — all-time solo kills
    ``{"metric": "final_blows", "threshold": 250}``      — all-time final blows
    ``{"metric": "kill_value_at_least", "isk": 1e10}``   — any single kill worth ≥ isk (at-kill)
    ``{"metric": "ship_class_kills", "class": "Capital", "threshold": 1}``
    ``{"metric": "sec_band_kills", "band": "nullsec", "threshold": 50}``
    ``{"metric": "role_on_kill", "role": "logi", "threshold": 25}``  — hull-approx (WS-D2 limits)

  The count metrics read the existing per-pilot aggregates (``CombatMetric`` via
  ``ranks.pilot_metric_counts``); the others are single, indexed, bounded queries computed only
  for a touched pilot who does not already hold the trophy, and memoised per pilot per scan.

* **Award engine** (:func:`scan_trophies`) — a cursor-consumer over the KB-29
  ``KillboardStreamEvent`` ring buffer (the same contract the outbound stream and per-pilot
  subscriptions use). It processes only pilots *touched* by fresh events — never a full board
  scan per mail — evaluates each enabled definition, and awards once (unique per pilot +
  definition). **Future-only:** a pilot is silently *baselined* on first sight
  (:class:`PilotTrophyBaseline`) — every trophy they already qualify for is recorded with
  ``notified=False`` and fires no ping / reward — so switching the feature on never
  back-congratulates a veteran. Only trophies earned *after* the baseline are celebrated.

* **On a celebrated award** — a per-pilot Pingboard ping (mirrors ``rank_notify``), a WS-B3
  ``trophy_awarded`` subscription fan-out (mirrors the rank-up hook exactly), and — when the
  trophy carries a configured payout and leadership has armed rewards — a pending
  ``RankRewardEvent`` through the EXISTING reward governance flow (``rewards.create_trophy_reward_event``).

* **Newbro pod coaching** (:func:`_coach_pod_losses`) — co-located on the same ring-buffer walk:
  when a still-new pilot loses a pod with zero implants, a gentle, i18n'd "fly with implants"
  nudge is DMed once (durably deduped by the Pingboard idempotency key). No ISK, no SRP change.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _l

from apps.doctrines.hulls import group_ids_for_class

from . import ranks, stream
from .models import (
    Killmail,
    KillmailItem,
    KillmailParticipant,
    NewbroConfig,
    PilotTrophy,
    PilotTrophyBaseline,
    SubscriptionEventType,
    TrophyDefinition,
    TrophyScanState,
)
from .valuation import at_kill_value_expr

log = logging.getLogger("forca.killboard")

_ATTACKER = Killmail.HomeRole.ATTACKER
_VICTIM = Killmail.HomeRole.VICTIM
_IMPLANT_FLAG = 89  # ESI inventory flag for an implant slot (see fitrender.slot_bucket)

_TROPHY_EVENT_KEY = "killboard.trophy_awarded"
_COACHING_EVENT_KEY = "killboard.newbro_no_implant"


# --------------------------------------------------------------------------- #
#  Settings accessors
# --------------------------------------------------------------------------- #
def _enabled() -> bool:
    return bool(getattr(settings, "KILLBOARD_TROPHIES_ENABLED", True))


def _batch() -> int:
    return int(getattr(settings, "KILLBOARD_TROPHY_SCAN_BATCH", 500))


def _home() -> int:
    return settings.FORCA_HOME_CORP_ID


# --------------------------------------------------------------------------- #
#  Criteria DSL — one definition's rule against a pilot's stats
# --------------------------------------------------------------------------- #
def _pilot_kill_base(character_id: int):
    """The pilot's home-corp PvP kill participations (attacker on a non-NPC home kill)."""
    return KillmailParticipant.objects.filter(
        role=KillmailParticipant.Role.ATTACKER,
        corporation_id=_home(),
        character_id=character_id,
        killmail__home_corp_role=_ATTACKER,
        killmail__is_npc=False,
    )


def _max_kill_value(character_id: int, ctx: dict) -> Decimal:
    """The pilot's single most valuable kill, at the price on the day it died (one Max query)."""
    if "max_kill_value" not in ctx:
        from django.db.models import Max

        row = _pilot_kill_base(character_id).aggregate(m=Max(at_kill_value_expr("killmail__")))
        ctx["max_kill_value"] = row["m"] or Decimal("0")
    return ctx["max_kill_value"]


def _ship_class_kills(character_id: int, hull_class: str, ctx: dict) -> int:
    """Distinct home kills whose victim hull folds into ``hull_class`` (one indexed count)."""
    key = f"class:{hull_class}"
    if key not in ctx:
        from apps.sde.models import SdeType

        gids = group_ids_for_class(hull_class)
        if not gids:
            ctx[key] = 0
        else:
            type_ids = SdeType.objects.filter(group_id__in=gids).values("type_id")
            ctx[key] = (
                _pilot_kill_base(character_id)
                .filter(killmail__victim_ship_type_id__in=type_ids)
                .values("killmail_id").distinct().count()
            )
    return ctx[key]


def _sec_band_kills(character_id: int, band: str, ctx: dict) -> int:
    """Distinct home kills in a security band (one indexed count)."""
    key = f"band:{band}"
    if key not in ctx:
        ctx[key] = (
            _pilot_kill_base(character_id)
            .filter(killmail__sec_band=band)
            .values("killmail_id").distinct().count()
        )
    return ctx[key]


# Attacker-side role inference is hull-only (WS-D2): only dedicated logi hulls and capitals are
# knowable from a bare ``ship_type_id``; tackle/ewar/links need the module list we don't have on
# an attacker row, so a trophy on those roles can never fire attacker-side (documented, returns 0).
_LOGI_HULL_GROUP_NAMES = frozenset({"logistics", "logistics frigate", "force auxiliary"})
_ROLE_HULL_CACHE: dict[str, frozenset[int]] = {}


def _role_hull_type_ids(role: str) -> frozenset[int] | None:
    """The set of hull type-ids that read as ``role`` from the hull alone, or ``None`` if the
    role is not attacker-inferable. Memoised at module scope (the SDE map is static)."""
    role = (role or "").lower()
    if role in _ROLE_HULL_CACHE:
        return _ROLE_HULL_CACHE[role]
    from apps.sde.models import SdeGroup, SdeType

    if role == "logi":
        gids = [
            gid for gid, name in SdeGroup.objects.values_list("group_id", "name")
            if (name or "").strip().lower() in _LOGI_HULL_GROUP_NAMES
        ]
    elif role == "capital":
        gids = list(group_ids_for_class("Capital"))
    else:
        return None  # not inferable from a hull alone
    type_ids = frozenset(
        SdeType.objects.filter(group_id__in=gids).values_list("type_id", flat=True)
    )
    # Only memoise a populated result — an empty answer (SDE not imported yet) must never be
    # frozen in, or a role would stay un-inferable after the hulls land.
    if type_ids:
        _ROLE_HULL_CACHE[role] = type_ids
    return type_ids


def _role_on_kill(character_id: int, role: str, ctx: dict) -> int:
    """Distinct home kills the pilot flew in a hull that reads as ``role`` (0 if not inferable)."""
    key = f"role:{role}"
    if key not in ctx:
        type_ids = _role_hull_type_ids(role)
        if not type_ids:
            ctx[key] = 0
        else:
            ctx[key] = (
                _pilot_kill_base(character_id)
                .filter(ship_type_id__in=type_ids)
                .values("killmail_id").distinct().count()
            )
    return ctx[key]


def progress_for(character_id: int, criteria: dict, aggregates: dict,
                 ctx: dict | None = None) -> tuple[int, bool, dict]:
    """Evaluate one criteria dict for a pilot → ``(value, met, progress)``.

    ``aggregates`` is ``ranks.pilot_metric_counts`` (kills/solo_kills/final_blows/active_days).
    ``ctx`` memoises the per-pilot bounded queries across a scan. ``progress`` is a small JSON-safe
    snapshot for display (the CV renders "value / target"). An unknown metric never matches.
    """
    ctx = ctx if ctx is not None else {}
    metric = (criteria or {}).get("metric")

    if metric in ("kills", "solo_kills", "final_blows"):
        value = int(aggregates.get(metric, 0) or 0)
        target = int(criteria.get("threshold", 0) or 0)
        return value, value >= target, {"metric": metric, "value": value, "target": target}

    if metric == "kill_value_at_least":
        target = int(criteria.get("isk", 0) or 0)
        value = int(_max_kill_value(character_id, ctx))
        return value, value >= target, {"metric": metric, "value": value, "target": target}

    if metric == "ship_class_kills":
        target = int(criteria.get("threshold", 0) or 0)
        value = _ship_class_kills(character_id, str(criteria.get("class", "")), ctx)
        return value, value >= target, {
            "metric": metric, "value": value, "target": target,
            "class": criteria.get("class", ""),
        }

    if metric == "sec_band_kills":
        target = int(criteria.get("threshold", 0) or 0)
        value = _sec_band_kills(character_id, str(criteria.get("band", "")), ctx)
        return value, value >= target, {
            "metric": metric, "value": value, "target": target, "band": criteria.get("band", ""),
        }

    if metric == "role_on_kill":
        target = int(criteria.get("threshold", 0) or 0)
        value = _role_on_kill(character_id, str(criteria.get("role", "")), ctx)
        return value, value >= target, {
            "metric": metric, "value": value, "target": target, "role": criteria.get("role", ""),
        }

    log.debug("trophy criteria has unknown metric %r (never matches)", metric)
    return 0, False, {"metric": metric, "value": 0, "target": 0}


# --------------------------------------------------------------------------- #
#  Award side-effects — ping + subscription fan-out + optional reward event
# --------------------------------------------------------------------------- #
def _notify_trophy(character_id: int, user_id: int | None, trophy: TrophyDefinition) -> bool:
    """Best-effort per-pilot celebration DM (mirrors ``rank_notify._send_rank_up``)."""
    if not user_id:
        return False
    from apps.pingboard.notifications import is_enabled

    if not is_enabled(_TROPHY_EVENT_KEY):
        return False
    body = (
        f"Trophy unlocked: {trophy.name}. {trophy.description or ''} "
        "Trophies update from the killboard's engagement sweep, so this can reflect a kill from "
        "the last little while rather than the exact moment."
    ).strip()
    try:
        from apps.pingboard import services as pingboard

        pingboard.emit_broadcast(
            category="custom",
            title="Trophy unlocked: {trophy_name}",
            body=body,
            template="killboard.trophy_awarded",
            context={"trophy_name": trophy.name, "trophy_desc": str(trophy.description or "")},
            audience={"kind": "user", "id": user_id},
            source_service="killboard",
            source_object_id=f"trophy:{character_id}:{trophy.id}",
            idempotency_key=f"killboard:trophy:{character_id}:{trophy.id}",
        )
        return True
    except Exception:  # noqa: BLE001 — a notification hiccup must never break the sweep
        log.exception("trophy notification failed for character %s", character_id)
        return False


def _fan_out_subscriptions(user_id: int | None, trophy: TrophyDefinition) -> None:
    """WS-B3: deliver the award to the pilot's own ``trophy_awarded`` subscriptions (mirrors the
    rank-up hook EXACTLY — the built-in DM has already fired; this only adds their chosen extra
    channels). Best-effort; never breaks the sweep."""
    if not user_id:
        return
    try:
        from .subscriptions import notify_user_event

        notify_user_event(
            event_type=SubscriptionEventType.TROPHY_AWARDED,
            user_id=user_id,
            title=_l("Trophy: %(name)s") % {"name": trophy.name},
            summary=_l("You earned the %(name)s trophy.") % {"name": trophy.name},
            payload={"event": "trophy_awarded", "trophy": trophy.slug,
                     "name": trophy.name, "tier": trophy.tier, "category": trophy.category},
        )
    except Exception:  # noqa: BLE001 — a subscription hiccup must never break the sweep
        log.exception("trophy subscription fan-out failed for user %s", user_id)


def _maybe_reward(character_id: int, character_name: str, user_id: int | None,
                  trophy: TrophyDefinition) -> None:
    """Create a pending reward event through the existing governance flow (no-op unless armed)."""
    try:
        from .rewards import create_trophy_reward_event

        create_trophy_reward_event(trophy, character_id, character_name, user_id)
    except Exception:  # noqa: BLE001 — a reward hiccup must never break the sweep
        log.exception("trophy reward-event creation failed for character %s", character_id)


def _award_side_effects(character_id: int, name: str, user_id: int | None,
                        trophy: TrophyDefinition) -> None:
    _notify_trophy(character_id, user_id, trophy)
    _fan_out_subscriptions(user_id, trophy)
    _maybe_reward(character_id, name, user_id, trophy)


# --------------------------------------------------------------------------- #
#  Per-pilot evaluation
# --------------------------------------------------------------------------- #
def evaluate_pilot(character_id: int, name: str, user_id: int | None,
                   definitions: list[TrophyDefinition], *, trigger_km_id: int | None) -> int:
    """Award every newly-qualified trophy for one pilot. Returns the number *celebrated*.

    Future-only: on first sight the pilot is baselined and all already-qualified trophies are
    recorded silently (no ping/reward); only trophies qualified after the baseline are
    celebrated. Idempotent — a re-run awards nothing new.
    """
    silent = not PilotTrophyBaseline.objects.filter(character_id=character_id).exists()
    if silent:
        PilotTrophyBaseline.objects.get_or_create(character_id=character_id)

    existing = set(
        PilotTrophy.objects.filter(character_id=character_id).values_list("definition_id", flat=True)
    )
    unearned = [d for d in definitions if d.id not in existing]
    if not unearned:
        return 0

    aggregates = ranks.pilot_metric_counts(character_id)
    ctx: dict = {}
    celebrated = 0
    for d in unearned:
        _value, met, progress = progress_for(character_id, d.criteria, aggregates, ctx)
        if not met:
            continue
        _pt, created = PilotTrophy.objects.get_or_create(
            character_id=character_id, definition=d,
            defaults={
                "character_name": name, "user_id": user_id, "notified": not silent,
                "killmail_id": trigger_km_id if not silent else None,
                "progress": progress, "awarded_at": timezone.now(),
            },
        )
        if created and not silent:
            _award_side_effects(character_id, name, user_id, d)
            celebrated += 1
    return celebrated


# --------------------------------------------------------------------------- #
#  Newbro pod coaching (co-located on the same ring-buffer walk)
# --------------------------------------------------------------------------- #
def _emit_coaching(character_id: int, user_id: int) -> bool:
    """Send the once-per-pilot 'fly with implants' nudge. Durable, once-ever dedup: a prior
    coaching alert for this pilot (by ``source_object_id``) short-circuits, since the Pingboard
    idempotency key only suppresses duplicates within its window, not for all time."""
    from apps.pingboard.models import Alert

    source_object_id = f"noimplant:{character_id}"
    if Alert.objects.filter(source_service="killboard", source_object_id=source_object_id).exists():
        return False
    try:
        from apps.pingboard import services as pingboard

        alert = pingboard.emit_broadcast(
            category="custom",
            title="Flying without implants",
            body=(
                "You lost a pod with no implants fitted — no harm done this time. When you're "
                "ready, even a cheap set of attribute implants speeds up your skill training. "
                "Ask a director for the corp implant guide."
            ),
            template="killboard.newbro_no_implant",
            audience={"kind": "user", "id": user_id},
            source_service="killboard",
            source_object_id=f"noimplant:{character_id}",
            idempotency_key=f"killboard:noimplant:{character_id}",  # once per pilot, ever
        )
        return alert is not None
    except Exception:  # noqa: BLE001 — a nudge must never break the sweep
        log.exception("newbro no-implant coaching failed for character %s", character_id)
        return False


def _coach_pod_losses(loss_events, members: dict[int, tuple[str, int | None]]) -> int:
    """Nudge still-new pilots who lost a pod with zero implants. Returns the count nudged.

    No-op unless leadership has armed the ``killboard.newbro_no_implant`` event. The "newbro
    window" is the corp's own newbro threshold: total engagements (kills + losses) below
    ``NewbroConfig.soften_below_events`` — the same number that softens the danger label — so no
    extra config is invented. Once per pilot (idempotency key), and only for a member with a
    linked account to DM.
    """
    from apps.pingboard.notifications import is_enabled

    if not is_enabled(_COACHING_EVENT_KEY):
        return 0
    from apps.srp.models import POD_TYPE_IDS

    from .leaderboards import pilot_combat_card

    pods = [
        ev for ev in loss_events
        if ev.victim_ship_type_id in POD_TYPE_IDS
        and ev.victim_character_id in members
        and members[ev.victim_character_id][1]  # has a linked user to DM
    ]
    if not pods:
        return 0
    cfg = NewbroConfig.load()
    coached = 0
    for ev in pods:
        cid = ev.victim_character_id
        _name, uid = members[cid]
        card = pilot_combat_card(cid)
        engagements = int(card.get("kills", 0)) + int(card.get("losses", 0))
        if engagements >= cfg.soften_below_events:
            continue  # past the newbro window — no coaching
        if KillmailItem.objects.filter(killmail_id=ev.killmail_id, flag=_IMPLANT_FLAG).exists():
            continue  # had implants — nothing to coach
        if _emit_coaching(cid, uid):
            coached += 1
    return coached


# --------------------------------------------------------------------------- #
#  The cursor-consumer beat entry
# --------------------------------------------------------------------------- #
def _attacker_char_ids(kill_km_ids: list[int]) -> dict[int, frozenset[int]]:
    """``{killmail_id: {attacker character ids}}`` for the batch's kill mails — one query."""
    if not kill_km_ids:
        return {}
    chars: dict[int, set] = {}
    rows = KillmailParticipant.objects.filter(
        killmail_id__in=kill_km_ids, role=KillmailParticipant.Role.ATTACKER,
        corporation_id=_home(), character_id__isnull=False,
    ).values_list("killmail_id", "character_id")
    for km_id, char_id in rows:
        chars.setdefault(km_id, set()).add(char_id)
    return {k: frozenset(v) for k, v in chars.items()}


def scan_trophies() -> dict:
    """Award trophies (and run pod coaching) for pilots touched by fresh stream events.

    A cursor-consumer over the KB-29 ring buffer: walks ``KillboardStreamEvent`` by ``seq`` from
    the stored cursor, gathers the home pilots on those fresh kills/losses, evaluates every
    enabled trophy for each, and advances the cursor past the batch. No-op when the feature is off
    or nothing new has landed. Cost is bounded by the batch size and the number of *distinct*
    touched pilots (not the board) — the count metrics reuse the nightly aggregate, and each other
    metric is one indexed query per touched pilot who lacks that trophy.
    """
    if not _enabled():
        return {"status": "disabled"}

    tip = stream.tip_seq()
    state = TrophyScanState.load()
    if state.last_seq >= tip:
        return {"status": "ok", "awarded": 0, "coached": 0}

    batch = list(
        stream.KillboardStreamEvent.objects.filter(seq__gt=state.last_seq).order_by("seq")[: _batch()]
    )
    if not batch:
        # The ring buffer was pruned past the cursor — fast-forward so we don't re-scan history.
        if state.last_seq < tip:
            state.last_seq = tip
            state.save(update_fields=["last_seq", "updated_at"])
        return {"status": "ok", "awarded": 0, "coached": 0}
    processed_tip = batch[-1].seq

    kill_km_ids = [ev.killmail_id for ev in batch if ev.home_role == _ATTACKER]
    loss_events = [ev for ev in batch if ev.home_role == _VICTIM]
    attackers = _attacker_char_ids(kill_km_ids)

    # Touched pilot → the (latest) triggering mail. A later event in the batch wins, so the award's
    # killmail context is the most recent qualifying mail for that pilot.
    touched: dict[int, int] = {}
    for ev in batch:
        if ev.home_role == _ATTACKER:
            for cid in attackers.get(ev.killmail_id, ()):  # type: ignore[union-attr]
                touched[cid] = ev.killmail_id
        elif ev.home_role == _VICTIM and ev.victim_character_id:
            touched[ev.victim_character_id] = ev.killmail_id

    from apps.sso.models import EveCharacter

    members = {
        c["character_id"]: (c["name"] or "", c["user_id"])
        for c in EveCharacter.objects.filter(
            character_id__in=list(touched), is_corp_member=True
        ).values("character_id", "name", "user_id")
    }

    definitions = list(TrophyDefinition.objects.filter(enabled=True))
    awarded = 0
    if definitions:
        for cid, (name, uid) in members.items():
            awarded += evaluate_pilot(cid, name, uid, definitions, trigger_km_id=touched[cid])

    coached = _coach_pod_losses(loss_events, members)

    state.last_seq = processed_tip
    state.save(update_fields=["last_seq", "updated_at"])
    return {"status": "ok", "awarded": awarded, "coached": coached, "scanned": len(batch)}


# --------------------------------------------------------------------------- #
#  Display helpers (CV + hall)
# --------------------------------------------------------------------------- #
def pilot_trophies(character_id: int) -> list[dict]:
    """A pilot's earned trophies (newest first), each with its definition for display."""
    rows = (
        PilotTrophy.objects.filter(character_id=character_id)
        .select_related("definition").order_by("-awarded_at")
    )
    return [
        {
            "slug": pt.definition.slug, "name": pt.definition.name,
            "description": pt.definition.description, "tier": pt.definition.tier,
            "category": pt.definition.category, "color": pt.definition.color_class,
            "icon": pt.definition.badge_icon, "awarded_at": pt.awarded_at,
            "killmail_id": pt.killmail_id, "progress": pt.progress,
        }
        for pt in rows
    ]


def trophy_progress_toward_next(character_id: int, *, limit: int = 6) -> list[dict]:
    """The pilot's nearest unearned trophies with progress toward each (for the CV).

    Bounded, on-demand (one pilot): evaluates each enabled, not-yet-earned definition and returns
    the closest ones by completion percentage.
    """
    earned = set(
        PilotTrophy.objects.filter(character_id=character_id).values_list("definition_id", flat=True)
    )
    defs = [d for d in TrophyDefinition.objects.filter(enabled=True) if d.id not in earned]
    if not defs:
        return []
    aggregates = ranks.pilot_metric_counts(character_id)
    ctx: dict = {}
    out = []
    for d in defs:
        value, met, progress = progress_for(character_id, d.criteria, aggregates, ctx)
        if met:
            continue  # will be awarded on the next scan; not "toward next"
        target = progress.get("target", 0) or 0
        pct = min(99.0, round(value / target * 100.0, 1)) if target else 0.0
        out.append({
            "slug": d.slug, "name": d.name, "description": d.description, "tier": d.tier,
            "category": d.category, "color": d.color_class, "icon": d.badge_icon,
            "value": value, "target": target, "progress_pct": pct,
        })
    out.sort(key=lambda r: r["progress_pct"], reverse=True)
    return out[:limit]


def trophy_leaderboard(*, limit: int = 20) -> list[dict]:
    """Top trophy earners (count of trophies per pilot) — the corp Trophy Leaderboard."""
    from django.db.models import Count

    rows = (
        PilotTrophy.objects.values("character_id")
        .annotate(n=Count("id")).order_by("-n", "character_id")[:limit]
    )
    return [{"character_id": r["character_id"], "count": r["n"]} for r in rows]
