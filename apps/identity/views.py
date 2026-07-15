"""Identity views: dashboards and privacy/data settings."""
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy, ngettext
from django.views.decorators.http import require_POST

from apps.doctrines.services import readiness_summary_for_character
from core import pilots, rbac
from core.audit import client_ip

from .services import delete_user_data


def _combat_window(days: int) -> dict:
    """Corp kills/losses and ISK over a recent window.

    Corp-wide numbers, identical for every member, so compute once and share via a
    short-lived cache. NOTE: intentionally counts *all* home-corp killmails (no
    ``is_npc`` filter) — same predicate as before.
    """
    from django.conf import settings
    from django.core.cache import cache
    from django.db.models import Count, Q

    from apps.killboard.models import Killmail

    home = getattr(settings, "FORCA_HOME_CORP_ID", 0)
    key = f"dash:combat7:{days}:{home}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    since = timezone.now() - timedelta(days=days)
    agg = (
        Killmail.objects.filter(involves_home_corp=True, killmail_time__gte=since)
        .aggregate(
            kills=Count("killmail_id", filter=Q(home_corp_role=Killmail.HomeRole.ATTACKER)),
            losses=Count("killmail_id", filter=Q(home_corp_role=Killmail.HomeRole.VICTIM)),
            isk_destroyed=Sum("total_value", filter=Q(home_corp_role=Killmail.HomeRole.ATTACKER)),
            isk_lost=Sum("total_value", filter=Q(home_corp_role=Killmail.HomeRole.VICTIM)),
        )
    )
    payload = {
        "days": days,
        "kills": agg["kills"],
        "losses": agg["losses"],
        "isk_destroyed": agg["isk_destroyed"] or 0,
        "isk_lost": agg["isk_lost"] or 0,
    }
    cache.set(key, payload, 300)
    return payload


def _combat_rank_for(character) -> dict | None:
    from core.features import feature_enabled

    if not feature_enabled("killboard"):
        return None  # the chip links into the killboard — no rank when it's off
    """The main character's all-time combat rank + danger, for the dashboard hero."""
    if not character:
        return None
    from apps.killboard.leaderboards import pilot_combat_card

    card = pilot_combat_card(character.character_id)
    return card if card.get("has_record") else None


def _combat_rank_progress_for(character, char_ids) -> dict | None:
    """The main character's rank-progression card: current/next title, kills-to-go,
    progress bar, the full ladder, kills this month and reward eligibility.

    Shown even at zero kills (the first title is one kill away), so it never
    discourages new pilots. All reads are cheap/cached — it sits on the dashboard.
    """
    from core.features import feature_enabled

    if not feature_enabled("killboard") or not character:
        return None
    from apps.killboard import ranks, rewards
    from apps.killboard.leaderboards import pilot_combat_card

    kills = pilot_combat_card(character.character_id).get("kills", 0)
    prog = ranks.rank_progress(kills)
    prog["kills_this_month"] = _kills_this_month(character.character_id)
    prog["reward"] = rewards.reward_dashboard(char_ids)
    # Attach a display label to the next reward-bearing rung, only worth computing when
    # rewards are enabled and one lies ahead (keeps the item-name lookup off the hot path).
    nr = prog.get("next_reward")
    if nr and prog["reward"].get("enabled"):
        nr["label"] = _reward_label(nr["reward_type"], nr["reward_amount"], nr["reward_item_type_id"])
    return prog


def _combat_tracks_for(character) -> list | None:
    """4.3: the pilot's parallel support-role rank tracks (solo / final blows / active
    days) alongside the headline KILLS rank — so a zero-kill logi/support pilot still has
    a rung to climb. Display-only, cheap (one cached card + one aggregate). None when the
    killboard is off or the pilot has no linked character."""
    from core.features import feature_enabled

    if not feature_enabled("killboard") or not character:
        return None
    from apps.killboard import ranks

    counts = ranks.pilot_metric_counts(character.character_id)
    return ranks.pilot_track_standings(counts)


def _compact_isk(value) -> str:
    """Compact ISK for the dashboard (1.2B, 340M, 12k)."""
    v = float(value or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(v) >= div:
            s = f"{v / div:.1f}".rstrip("0").rstrip(".")
            return f"{s}{unit}"
    return f"{v:.0f}"


def _reward_label(reward_type: str, amount, item_type_id) -> str:
    """A human, one-line label for a rank reward (ISK/PLEX/item/manual)."""
    if reward_type == "isk":
        return f"{_compact_isk(amount)} ISK"
    if reward_type == "plex":
        return f"{int(amount or 0)} PLEX"
    if reward_type == "item":
        from apps.sde.models import SdeType

        name = (
            SdeType.objects.filter(type_id=item_type_id).values_list("name", flat=True).first()
            if item_type_id else None
        )
        return name or gettext("an item reward")
    return gettext("a special reward")  # manual / other


def _kills_this_month(character_id: int) -> int:
    from apps.killboard.models import MonthlyPilotKillStat

    now = timezone.now()
    return (
        MonthlyPilotKillStat.objects.filter(
            character_id=character_id, year=now.year, month=now.month
        ).values_list("kills", flat=True).first()
        or 0
    )


def _my_combat_week(char_ids: list[int]) -> dict | None:
    """The pilot's own 7-day PvP line (all linked characters) for the combat
    panel's "you this week" row. Indexed killmail-table reads only."""
    from apps.killboard.models import Killmail, KillmailParticipant

    if not char_ids:
        return None
    since = timezone.now() - timedelta(days=7)
    # distinct() because two of the user's characters can share one killmail.
    kill_ids = list(
        KillmailParticipant.objects.filter(
            role=KillmailParticipant.Role.ATTACKER,
            character_id__in=char_ids,
            killmail__is_npc=False,
            killmail__killmail_time__gte=since,
        )
        .values_list("killmail_id", flat=True)
        .distinct()
    )
    isk_destroyed = 0
    if kill_ids:
        isk_destroyed = (
            Killmail.objects.filter(killmail_id__in=kill_ids).aggregate(s=Sum("total_value"))["s"]
            or 0
        )
    losses = Killmail.objects.filter(
        home_corp_role=Killmail.HomeRole.VICTIM,
        victim_character_id__in=char_ids,
        killmail_time__gte=since,
    ).count()
    return {"kills": len(kill_ids), "losses": losses, "isk_destroyed": isk_destroyed}


def _my_service_rows(user) -> list[dict]:
    """In-flight ISK with the member services, as My-work rows.

    Freight/Buyback/Corp Store are audience-gated (NOT core.features keys —
    feature_enabled() would silently pass for unknown keys), so each block
    rides its service's can_access(user); the audience value is cached 300s
    per service, so the checks are cheap.
    """
    rows: list[dict] = []
    from apps.buyback.services import can_access as buyback_access
    from apps.logistics.services import can_access as freight_access
    from apps.store.services import can_access as store_access

    if buyback_access(user):
        from apps.buyback.models import BuybackOffer

        n = BuybackOffer.objects.filter(
            seller=user,
            status__in=[BuybackOffer.Status.OPEN, BuybackOffer.Status.PURCHASED],
        ).count()
        if n:
            rows.append(
                {
                    "url_name": "buyback:board",
                    "icon": "#i-coin",
                    "text": ngettext(
                        "%(n)d buyback offer awaiting a buyer or payout",
                        "%(n)d buyback offers awaiting a buyer or payout",
                        n,
                    ) % {"n": n},
                }
            )
    if store_access(user):
        from apps.store.models import StoreOrder

        # The "active" set matches store.views.my_orders exactly (inline there,
        # not a model constant) — dropping READY/DEPOSIT_PAID loses real orders.
        n = StoreOrder.objects.filter(
            buyer=user,
            status__in=[
                StoreOrder.Status.OPEN,
                StoreOrder.Status.CLAIMED,
                StoreOrder.Status.DEPOSIT_PAID,
                StoreOrder.Status.IN_PRODUCTION,
                StoreOrder.Status.READY,
            ],
        ).count()
        if n:
            rows.append(
                {
                    "url_name": "store:my_orders",
                    "icon": "#i-cube",
                    "text": ngettext(
                        "%(n)d corp-store order in progress",
                        "%(n)d corp-store orders in progress",
                        n,
                    ) % {"n": n},
                }
            )
    if freight_access(user):
        from django.db.models import Q

        from apps.logistics.models import CourierContract

        n = (
            CourierContract.objects.filter(
                Q(
                    created_by=user,
                    status__in=[
                        CourierContract.Status.OUTSTANDING,
                        CourierContract.Status.IN_PROGRESS,
                    ],
                )
                | Q(assigned_user=user, status=CourierContract.Status.IN_PROGRESS)
            )
            .distinct()
            .count()
        )
        if n:
            rows.append(
                {
                    "url_name": "logistics:contracts",
                    "icon": "#i-truck",
                    "text": ngettext(
                        "%(n)d freight contract in flight",
                        "%(n)d freight contracts in flight",
                        n,
                    ) % {"n": n},
                }
            )
    return rows


def _next_op_payload(character) -> dict:
    """Cache-safe summary of the nearest scheduled op for the pinned dashboard
    row; {} when nothing is coming up.

    upcoming_for_pilot walks character_readiness per doctrine fit (~1s cold),
    so the result is cached 600s — compute-on-miss with cache-only writes on
    GET (the onboarding contract); pilots.warm_briefings re-warms it on the
    same 10-minute cadence as the digest.
    """
    from django.core.cache import cache

    from apps.operations.models import Operation
    from apps.operations.services import upcoming_for_pilot

    key = f"dashboard:next_op:{character.character_id}"
    payload = cache.get(key)
    if payload is None:
        info = upcoming_for_pilot(character)
        if info:
            op = info["op"]
            payload = {
                "id": op.pk,
                "name": op.name,
                "target_at": op.target_at,
                "ready": info["ready"],
                "total": info["total"],
            }
        else:
            # The nearest op may carry no OperationDoctrine rows (ship-slot-only
            # ops) — upcoming_for_pilot returns None for those; still show the
            # fleet, just without a readiness chip.
            op = (
                Operation.objects.filter(
                    status__in=[Operation.Status.PLANNED, Operation.Status.ACTIVE]
                )
                .order_by("target_at", "-created_at")
                .first()
            )
            payload = (
                {"id": op.pk, "name": op.name, "target_at": op.target_at, "ready": None, "total": None}
                if op is not None
                else {}
            )
        cache.set(key, payload, 600)
    return payload


# Command-Center panels a pilot may hide (PCC-4). Only panels whose template guard is a
# single ``and`` chain are listed, so hiding is a pure additive gate — no reordering and
# no risky rework of the ~360-line context builder. Persisted in
# ``PilotPreference.dashboard_layout["hidden"]``; absent/empty = the default full layout.
# NOTE: only the *label* (element [1]) is translated. Element [0] is the persisted key
# (``dashboard_layout["hidden"]``, a JSONField) and is string-compared in dashboard.html —
# translating it would poison every saved layout.
HIDEABLE_PANELS = (
    ("raffle", gettext_lazy("Raffle")),
    ("combat_log", gettext_lazy("Combat log")),
    ("onboarding", gettext_lazy("Getting started")),
    ("pilot_stats", gettext_lazy("Pilot stats")),
    ("doctrines", gettext_lazy("Doctrine readiness")),
    ("campaigns", gettext_lazy("Campaign Command")),
    ("capsuleer", gettext_lazy("Capsuleer Path")),
)
_HIDEABLE_KEYS = frozenset(k for k, _label in HIDEABLE_PANELS)


def _capsuleer_panel(user):
    """The Command-Center Capsuleer Path panel context, or ``None`` when the feature is off or the
    pilot has no active goal (the panel is omitted, never rendered hollow — doc 10 §5.11)."""
    from core.features import feature_enabled

    if not feature_enabled("capsuleer"):
        return None
    from apps.capsuleer import services as capsuleer_services

    return capsuleer_services.dashboard_panel(user)


def _campaigns_panel(user):
    """The Command-Center Campaign Command panel context, or ``None`` when the feature is off or
    the pilot has nothing to show (the panel is omitted, never rendered hollow — doc 10 §3.3)."""
    from core.features import feature_enabled

    if not feature_enabled("campaigns"):
        return None
    from apps.campaigns import services as campaign_services

    panel = campaign_services.pilot_panel(user)
    return panel if panel["has_content"] else None


def _hidden_panels(user) -> set:
    """The set of dashboard panel keys this pilot has chosen to hide."""
    from apps.pilots.services import get_prefs

    hidden = (get_prefs(user).dashboard_layout or {}).get("hidden", [])
    return {h for h in hidden if h in _HIDEABLE_KEYS}


def _dashboard_context(request: HttpRequest) -> dict:
    from apps.industry.models import IndustryProject
    from apps.killboard.models import Killmail
    from apps.onboarding.services import next_actions
    from apps.pilots.services import monthly_summary, recognition_feed
    from apps.skills.models import SkillPlan, SkillPlanStep
    from apps.skills.services import closest_doctrines, remaining_seconds
    from apps.stockpile.models import HaulingTask
    from apps.stockpile.services import shortfalls_against_targets

    user = request.user
    characters = list(user.characters.select_related("corporation").all())
    # The pilot this page is ABOUT. It follows the selector, not the account's main: the rail
    # portrait, the greeting and every panel below must describe the same pilot, or the page is
    # lying about whose readiness, training and orders these are (LP-3).
    main_character = pilots.acting_pilot(user)
    char_ids = [c.character_id for c in characters]

    # --- The three-page merge: signals + unified quest queue + readiness ------
    # The Command Center absorbed the Daily Briefing and My Readiness; each
    # section keeps its own feature toggle, the page itself is always-on.
    from django.core.cache import cache

    from apps.pilots.briefing import (
        KIND_FEATURE,
        partition_briefing,
        pilot_briefing,
        unified_quest_queue,
    )
    from core.features import feature_enabled

    show_digest = feature_enabled("briefing")
    show_orders = feature_enabled("command_intel_pilot")
    show_readiness = feature_enabled("readiness")
    show_boards = feature_enabled("recommendations")

    def _kind_ok(it: dict) -> bool:
        key = KIND_FEATURE.get(it.get("kind"))
        return key is None or feature_enabled(key)

    signals: list[dict] = []
    advice: list[dict] = []
    if show_digest:
        raw_signals, advice, _claimable = partition_briefing(pilot_briefing(user))
        # Task rows (assigned + claimable pointers) are covered by the My-work
        # card and the pick-up boards on this page — signals carry only the
        # genuinely expiring kinds (op prep, SRP).
        signals = [it for it in raw_signals if it.get("kind") != "task" and _kind_ok(it)]
        advice = [it for it in advice if _kind_ok(it)]

    # "When's the next fleet" gets a PERMANENT home: a pinned row at the top of
    # the signals panel — even the quiet "no ops scheduled" state renders.
    next_op = None
    show_next_op = feature_enabled("operations") and main_character is not None
    if show_next_op:
        next_op = _next_op_payload(main_character)
        if next_op:
            # "started" flips within the cache TTL, so stamp it per-request.
            next_op = dict(next_op)
            next_op["started"] = bool(
                next_op["target_at"] and next_op["target_at"] <= timezone.now()
            )
        # The pinned row is the canonical op surface — drop the digest's
        # 'operation' signal so the same fleet doesn't appear twice (dedup
        # contract: one home per row, enforced in the view layer).
        signals = [it for it in signals if it.get("kind") != "operation"]

    directives: list = []
    if show_orders and main_character is not None:
        from apps.command_intel import pilot as ci_pilot

        # No-write-on-GET (matching the readiness contract below): the
        # pilots.warm_briefings beat owns persistence; a cold-cache GET
        # recomputes read-only EXCEPT the one-time seed for a brand-new member
        # the beat hasn't reached, so the interactive queue isn't empty.
        if cache.get(ci_pilot.cache_key(main_character.character_id)) is None:
            from apps.command_intel.models import PilotDirective

            needs_seed = not PilotDirective.objects.filter(
                user=user, character=main_character
            ).exists()
            ci_pilot.compute_directives(user, main_character, persist=needs_seed)
        # This pilot's orders, not the account's (LP-3).
        directives = ci_pilot.open_directives(user, main_character)

    recos: list = []
    pilot_readiness = None
    if show_readiness and main_character is not None:
        from django.db.models import Q
        from django.utils import timezone as tz

        from apps.readiness.models import PilotRecommendation
        from apps.readiness.pilot import cache_key as rd_cache_key
        from apps.readiness.pilot import compute_pilot

        # The beat (readiness.warm_pilots) owns persistence; a GET recomputes
        # read-only on cache miss EXCEPT the one-time seed for a brand-new main
        # (same contract the old /readiness/me/ view kept — never churn on GET).
        payload = cache.get(rd_cache_key(main_character.character_id, user.pk))
        if payload is None:
            # Cold cache (e.g. a pilot's first post-login visit, or after a deploy flush):
            # serve the last persisted snapshot — a single cheap read — instead of the full
            # ~2s compute_pilot recompute in the request. warm_pilot_after_login (fired at
            # login) and the readiness.warm_pilots beat repopulate the real cache. Only a
            # brand-new pilot with no snapshot pays a fresh compute (cheap with no data yet).
            from apps.readiness.models import PilotReadinessSnapshot

            # Scope by user too (matching compute_pilot._score_trend) so a re-linked
            # character never surfaces the previous owner's readiness scores.
            snap = (
                PilotReadinessSnapshot.objects
                .filter(character_id=main_character.character_id, user=user)
                .order_by("-created_at")
                .first()
            )
            if snap is not None:
                payload = {"facets": snap.facets or {}, "overall": snap.overall,
                           "trend": [], "week_delta": None}
            else:
                needs_seed = not PilotRecommendation.objects.filter(
                    user=user, character_id=main_character.character_id
                ).exists()
                payload = compute_pilot(main_character, persist=needs_seed)
        facets = payload["facets"]
        scored = {k: v for k, v in facets.items() if v is not None}
        overall = payload["overall"]
        pilot_readiness = {
            "overall": overall,
            "ring_dash": round(overall * 3.52, 1),  # r=56 ring, C≈352
            "facets": [{"key": k, "score": facets.get(k)} for k in
                       ("doctrine", "combat", "logistics", "strategic", "activity", "contribution")],
            "lowest": min(scored, key=scored.get) if scored else None,
            # Computed inside compute_pilot and cached with the payload; .get()
            # defaults tolerate a stale pre-deploy cache entry.
            "trend": payload.get("trend") or [],
            "week_delta": payload.get("week_delta"),
        }
        recos = list(
            PilotRecommendation.objects.filter(
                user=user, character_id=main_character.character_id,
                state=PilotRecommendation.State.OPEN,
            ).filter(Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=tz.now()))
        )
        # Seam B read side: the quest log was frozen in English by the readiness beat (no reader,
        # no locale). Re-render each row under THIS reader's language from the persisted scaffold
        # key before it feeds the unified quest queue. In-memory only — never saved, so the
        # English audit column is untouched.
        for r in recos:
            r.title = r.title_i18n
            r.detail = r.detail_i18n

    career = []
    hidden_panels = _hidden_panels(user)
    capsuleer_panel = None
    if feature_enabled("capsuleer"):
        from apps.capsuleer import services as capsuleer_services

        # One shared active-goal fetch feeds both the quest row and the panel; the panel is skipped
        # entirely when the pilot has hidden it, keeping the dashboard delta within budget (finding 22).
        bundle = capsuleer_services.dashboard_bundle(
            user, include_panel="capsuleer" not in hidden_panels
        )
        career = bundle["quests"]
        capsuleer_panel = bundle["panel"]
    quests = unified_quest_queue(directives, recos, career=career)
    if quests or show_orders or show_readiness or career:
        # The queue is the canonical advice — the digest fallback renders only
        # when BOTH engines are off. A drained queue must show the earned
        # "you're current" state, not resurface just-dismissed advice as
        # un-dismissable digest rows.
        advice = []

    # Doctrine readiness for the main character.
    readiness = readiness_summary_for_character(main_character) if main_character else []
    # Status counts feeding the sidebar's capacitor-readout bar (one segmented
    # bar replaced the old 54-chip wall — the template renders these counts).
    doctrine_breakdown = {
        status: sum(1 for r in readiness if r["status"] == status)
        for status in ("optimal", "viable", "not_ready", "unknown")
    }

    # Industry / logistics / supply at a glance.
    active_projects = IndustryProject.objects.filter(status=IndustryProject.Status.ACTIVE)
    open_hauls = HaulingTask.objects.filter(status=HaulingTask.Status.OPEN)
    my_hauls = HaulingTask.objects.filter(
        claimed_by_character_id__in=char_ids
    ).exclude(status=HaulingTask.Status.DONE)
    shortfalls = shortfalls_against_targets()

    # Live in-game skill queue for the main — read from the snapshot the
    # characters.sync_all_member_skills beat maintains (never ESI on GET).
    training = None
    if main_character is not None:
        from apps.skills.overview import character_training

        training = character_training(main_character)

    # Your training: the plan with the most work left.
    skill_plan = None
    for plan in SkillPlan.objects.filter(character__user=user).select_related("character").prefetch_related("steps"):
        pending = [s for s in plan.steps.all() if s.status != SkillPlanStep.Status.DONE]
        if pending:
            cand = {"plan": plan, "remaining": remaining_seconds(plan), "next": pending[0], "left": len(pending)}
            if skill_plan is None or cand["remaining"] > skill_plan["remaining"]:
                skill_plan = cand

    # Getting started — the cached all-characters variant the warmer maintains.
    onboarding: list = []
    if feature_enabled("onboarding") and main_character is not None:
        from core.i18n import i18n_cache_key
        # Language-scoped to match the warmer (pilots.tasks.warm_briefings) — next_actions()
        # returns translated prose, so the key must carry the reader's language.
        onboarding_key = i18n_cache_key(f"briefing:onboarding:{user.pk}")
        onboarding = cache.get(onboarding_key)
        if onboarding is None:
            onboarding = [
                {"character": character, "action": action}
                for character in characters
                for action in next_actions(character, limit=2)
            ]
            cache.set(onboarding_key, onboarding, 600)

    # Pilot progression: doctrines you're closest to flying but can't yet.
    # STATUS + tooling (build-plan buttons) — the quest queue is the advice.
    closest = closest_doctrines(main_character, limit=3) if main_character else []

    # Pick-up work boards — each gated by the feature that owns its destination.
    hauls_board: list = []
    projects_board: list = []
    claimable_board: list = []
    if show_boards and main_character is not None:
        if feature_enabled("stockpile"):
            hauls_board = list(
                HaulingTask.objects.filter(status=HaulingTask.Status.OPEN)
                .select_related("source_location", "dest_location")[:6]
            )
        if feature_enabled("industry"):
            projects_board = list(
                IndustryProject.objects.filter(
                    status=IndustryProject.Status.ACTIVE, assigned_to__isnull=True
                ).order_by("-created_at")[:6]
            )
        if feature_enabled("tasks"):
            from apps.tasks.models import Task as _Task

            claimable_board = list(
                _Task.objects.filter(
                    is_open=True, assignee__isnull=True, status=_Task.Status.OPEN
                )[:6]
            )

    # (The old 'Prep for op' hero card was absorbed by the digest's operation
    # signal — same upcoming_for_pilot source, one surface.)

    # Your recent losses → what it would cost to get back in the fight, with
    # SRP eligibility surfaced inline. Killboard-derived AND an SRP surface, so
    # it renders (and is computed) while either feature is on.
    my_losses = []
    if feature_enabled("killboard") or feature_enabled("srp"):
        from apps.srp.models import SrpClaim
        from apps.srp.services import active_program
        from apps.srp.services import eligibility as srp_eligibility

        loss_qs = list(
            Killmail.objects.filter(
                involves_home_corp=True,
                home_corp_role=Killmail.HomeRole.VICTIM,
                victim_character_id__in=char_ids,
            ).order_by("-killmail_time")[:5]
        )
        claims = {
            c.killmail_id: c
            for c in SrpClaim.objects.filter(
                killmail_id__in=[k.killmail_id for k in loss_qs]
            )
        }
        # Load the SRP programme ONCE and pass it in — it's identical for every loss, so
        # eligibility()'s default active_program() would otherwise re-query it per loss.
        srp_program = active_program()
        for km in loss_qs:
            info = srp_eligibility(km, program=srp_program)
            claim = claims.get(km.killmail_id)
            my_losses.append(
                {
                    "km": km,
                    "srp_eligible": info.get("eligible", False) and claim is None,
                    "srp_payout": info.get("payout"),
                    # Full claim (or None) so the template can show the whole
                    # lifecycle: pending → approved → paid · amount / denied.
                    "srp_claim": claim,
                }
            )

        # A payout landing is time-sensitive good news — surface it beside the
        # expiring signals for a few days. decided_at doubles as the payment
        # timestamp for PAID claims (srp.services.mark_paid re-stamps it).
        if feature_enabled("srp"):
            from apps.sde.templatetags.eve import isk as _isk
            from apps.sde.templatetags.eve import type_name as _type_name

            recently_paid = SrpClaim.objects.filter(
                claimant=user,
                status=SrpClaim.Status.PAID,
                decided_at__gte=timezone.now() - timedelta(days=4),
            ).select_related("killmail").order_by("-decided_at")[:2]
            for c in recently_paid:
                signals.append(
                    {
                        "kind": "srp",
                        "text": gettext(
                            "SRP payout landed: %(isk)s ISK for your %(ship)s."
                        ) % {
                            "isk": _isk(c.payout),
                            "ship": _type_name(c.killmail.victim_ship_type_id),
                        },
                        "url": "/srp/",
                    }
                )

    # Your contribution this month, in native units (no composite score).
    contribution = monthly_summary(user)
    # Corp-wide recognition: recent contributions from members who allow it —
    # part of the contribution ledger feature, so it follows that toggle.
    # Capped at 3 since the recognition rows share a panel with the pilot's own
    # contribution chips now — social proof, not a feed.
    recognition = recognition_feed(limit=3) if feature_enabled("contributions") else []

    # Your open tasks + how many are claimable in the corp pool.
    from apps.tasks.models import Task

    my_tasks = list(
        Task.objects.filter(assignee=user).exclude(
            status__in=[Task.Status.DONE, Task.Status.CANCELLED]
        )[:5]
    )
    claimable_count = Task.objects.filter(
        is_open=True, assignee__isnull=True, status=Task.Status.OPEN
    ).count()

    # In-flight ISK with the member services (buyback / store / freight).
    my_services = _my_service_rows(user) if main_character is not None else []

    # Raffle standing + any pending win — surfaced where every pilot already looks
    # (the adoption flywheel the raffle exists to drive). Gated by the feature flag.
    raffle_card = None
    if feature_enabled("raffle"):
        from apps.raffle.services import dashboard_summary as _raffle_summary

        raffle_card = _raffle_summary(user)

    ctx = {
        "raffle": raffle_card,
        "characters": characters,
        "main_character": main_character,
        "closest": closest,
        "my_losses": my_losses,
        "contribution": contribution,
        "recognition": recognition,
        "my_tasks": my_tasks,
        "claimable_count": claimable_count,
        "combat7": _combat_window(7),
        "combat_mine": _my_combat_week(char_ids) if feature_enabled("killboard") else None,
        "combat_rank": _combat_rank_for(main_character),
        "combat_rank_progress": _combat_rank_progress_for(main_character, char_ids),
        "combat_tracks": _combat_tracks_for(main_character),
        "recent_kms": list(
            Killmail.objects.filter(involves_home_corp=True).order_by("-killmail_time")[:6]
        ),
        # The three-page merge zones.
        "signals": signals,
        "next_op": next_op,
        "show_next_op": show_next_op,
        "advice": advice,
        "quests": quests,
        "pilot_readiness": pilot_readiness,
        "show_digest": show_digest,
        "show_orders": show_orders,
        "show_readiness": show_readiness,
        "show_boards": show_boards,
        "hauls_board": hauls_board,
        "projects_board": projects_board,
        "claimable_board": claimable_board,
        "readiness": readiness,
        "ready_count": sum(1 for r in readiness if r["status"] in ("optimal", "viable")),
        "doctrine_breakdown": doctrine_breakdown,
        "industry": {
            "active": active_projects.count(),
            "mine": active_projects.filter(assigned_to=user).count(),
            "unclaimed": active_projects.filter(assigned_to__isnull=True).count(),
        },
        "logistics": {
            "open": open_hauls.count(),
            "mine": my_hauls.count(),
        },
        "shortfalls_count": len(shortfalls),
        "skill_plan": skill_plan,
        "training": training,
        "my_services": my_services,
        "onboarding": onboarding,
        "campaigns_panel": _campaigns_panel(user),
        "capsuleer_panel": capsuleer_panel,
        "hidden_panels": hidden_panels,
        "hideable_panels": HIDEABLE_PANELS,
    }

    ctx.update(_officer_deck_context(
        user,
        unclaimed=ctx["industry"]["unclaimed"],
        shortfalls=ctx["shortfalls_count"],
        open_hauls=ctx["logistics"]["open"],
    ))
    return ctx


def _officer_deck_context(user, *, unclaimed=None, shortfalls=None, open_hauls=None) -> dict:
    """The command deck's role-gated strata, shared by both dashboard branches.

    The officer quick-nav (counts + links) is ROLE-gated — it must survive the
    'briefing' toggle, which only owns the corp-metric tiles (``leadership``).
    Directors additionally get the integrations health chip.
    """
    from core.features import feature_enabled

    if not rbac.has_role(user, rbac.ROLE_OFFICER):
        return {}

    from apps.corporation.roster import pending_registration_count
    from apps.recommendations.models import Recommendation

    if unclaimed is None:
        from apps.industry.models import IndustryProject

        unclaimed = IndustryProject.objects.filter(
            status=IndustryProject.Status.ACTIVE, assigned_to__isnull=True
        ).count()
    if shortfalls is None:
        from apps.stockpile.services import shortfalls_against_targets

        shortfalls = len(shortfalls_against_targets())
    if open_hauls is None:
        from apps.stockpile.models import HaulingTask

        open_hauls = HaulingTask.objects.filter(status=HaulingTask.Status.OPEN).count()

    deck: dict = {
        "officer": {
            "open_recs": Recommendation.objects.filter(
                state__in=[Recommendation.State.NEW, Recommendation.State.ACKNOWLEDGED],
                required_permission__in=["officer", "director"],
            ).count(),
            "unclaimed_projects": unclaimed,
            "shortfalls": shortfalls,
            "open_hauls": open_hauls,
            "roster_pending": pending_registration_count(),
        }
    }
    if feature_enabled("briefing"):
        from apps.pilots.briefing import leadership_briefing

        deck["leadership"] = leadership_briefing()
    if rbac.has_role(user, rbac.ROLE_DIRECTOR):
        from apps.admin_audit.health import integration_health

        health = integration_health()
        deck["integrations"] = {
            "ok": health["ok"],
            "has_asset_token": health["has_asset_token"],
            "stale": [f["label"] for f in health["feeds"] if f["status"] != "ok"],
        }
    return deck


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    """The Command Center — the pilot's single home page.

    Absorbed "My Readiness" (/readiness/me/) and the "Daily Briefing"
    (/pilots/briefing/), which both redirect here now. Always-on; each merged
    zone gates itself on its own feature key inside the template.
    """
    if not request.user.characters.exists():
        # Link-a-character CTA replaces the pilot body; officers keep their deck.
        ctx: dict = {
            "characters": [], "campaigns_panel": _campaigns_panel(request.user),
            "capsuleer_panel": _capsuleer_panel(request.user),
            **_officer_deck_context(request.user),
        }
        return render(request, "identity/dashboard.html", ctx)
    return render(request, "identity/dashboard.html", _dashboard_context(request))


@login_required
@require_POST
def save_dashboard_layout(request: HttpRequest) -> HttpResponse:
    """Persist which Command-Center panels the pilot wants shown (PCC-4).

    The form posts the panels to *show* (checkbox = visible); every hideable panel not
    ticked is stored as hidden. All ticked (the default) stores no hides, so a pilot who
    never customises keeps the full default layout.
    """
    from apps.pilots.services import get_prefs

    shown = set(request.POST.getlist("show"))
    hidden = [k for k in _HIDEABLE_KEYS if k not in shown]
    prefs = get_prefs(request.user)
    layout = dict(prefs.dashboard_layout or {})
    layout["hidden"] = hidden
    prefs.dashboard_layout = layout
    prefs.save(update_fields=["dashboard_layout", "updated_at"])
    messages.success(request, gettext("Dashboard layout saved."))
    return redirect("identity:dashboard")


def _grouped_skills(snapshot) -> list[dict]:
    """The character's trained skills grouped by SDE group (a browsable list, not a
    flat 200-item dump). One batched query resolves names + group per skill."""
    if not snapshot or not snapshot.skills:
        return []
    from apps.sde.models import SdeType

    meta = {
        tid: (name, grp or gettext("Other"))
        for tid, name, grp in SdeType.objects.filter(type_id__in=[int(k) for k in snapshot.skills])
        .select_related("group").values_list("type_id", "name", "group__name")
    }
    groups: dict[str, list] = {}
    for sid, info in snapshot.skills.items():
        name, grp = meta.get(
            int(sid),
            (gettext("Skill %(id)s") % {"id": sid}, gettext("Other")),
        )
        groups.setdefault(grp, []).append(
            {"id": int(sid), "name": name, "level": int(info.get("trained_level", 0))}
        )
    out = []
    for grp in sorted(groups):
        skills = sorted(groups[grp], key=lambda s: s["name"])
        out.append({
            "group": grp,
            "skills": skills,
            "count": len(skills),
            "at_v": sum(1 for s in skills if s["level"] == 5),
        })
    return out


@login_required
def character_dashboard(request: HttpRequest, character_id: int) -> HttpResponse:
    """One rich page per character: SP overview, the live in-game training queue,
    this character's training plans, and a browsable skill list. Doctrine *flyability*
    is summarised here and detailed on the Doctrines page."""
    from apps.doctrines.models import Doctrine
    from apps.skills.models import SkillPlanStep
    from apps.skills.overview import character_training
    from apps.skills.services import remaining_seconds

    character = get_object_or_404(request.user.characters, character_id=character_id)
    training = character_training(character)
    snapshot = character.skill_snapshots.filter(is_latest=True).first()

    # Compact doctrine-readiness summary — the full per-doctrine breakdown lives on
    # the Doctrines page, so here we only headline "how many can I fly".
    readiness = readiness_summary_for_character(character)
    known = [r for r in readiness if r["status"] != "unknown"]
    flyable = sum(1 for r in known if r["status"] in ("viable", "optimal"))

    plans = []
    for plan in (character.skill_plans.select_related("target_doctrine")
                 .prefetch_related("steps").order_by("-created_at")):
        steps = list(plan.steps.all())
        done = sum(1 for s in steps if s.status == SkillPlanStep.Status.DONE)
        plans.append({"plan": plan, "done": done, "total": len(steps),
                      "remaining": remaining_seconds(plan)})

    return render(request, "identity/character.html", {
        "character": character,
        "training": training,
        "snapshot": snapshot,
        "skill_groups": _grouped_skills(snapshot),
        "skill_count": len(snapshot.skills) if snapshot else 0,
        "flyable": flyable,
        "doctrine_total": len(known),
        "plans": plans,
        "all_characters": list(request.user.characters.all()),
        "doctrines": Doctrine.objects.filter(status=Doctrine.Status.ACTIVE).order_by("-priority", "name"),
    })


@login_required
def privacy(request: HttpRequest) -> HttpResponse:
    from apps.pilots.services import get_prefs

    return render(
        request,
        "identity/privacy.html",
        {"characters": request.user.characters.all(), "prefs": get_prefs(request.user)},
    )


@login_required
@require_POST
def delete_my_data(request: HttpRequest) -> HttpResponse:
    """Member self-service erasure: delete private data, detach characters,
    then log out. Killmails (public EVE facts) are retained."""
    delete_user_data(request.user, actor=request.user, ip=client_ip(request))
    logout(request)
    return redirect("/")
