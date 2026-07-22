"""Celery tasks for killmail discovery and ingestion."""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

from apps.sso.models import EveCharacter
from apps.sso.services import character_is_corp_director
from apps.sso.token_service import NoValidToken, get_valid_access_token
from core.esi.client import ESIClient, ESIError
from core.mixins import Source

from .ingest import ingest_killmail
from .stats import rebuild_corp_metrics, rebuild_member_metrics

log = logging.getLogger("forca.killboard")

# Scope a Director (or role-holder) token must carry for the corp killmail feed.
CORP_KILLMAILS_SCOPE = "esi-killmails.read_corporation_killmails.v1"


def _find_corp_killmail_director(corp_id: int) -> EveCharacter | None:
    """A corp character that holds the in-game **Director** role *and* a token with
    the corp-killmails scope.

    The scope alone is not enough: ``esi-killmails.read_corporation_killmails.v1``
    ships in ``EVE_SSO_DEFAULT_SCOPES`` so every member grants it at login — but
    ``/corporations/{id}/killmails/recent/`` requires the Director role. Trusting the
    scope alone returns the first member (usually not a Director), and CCP then
    answers ``403 "Character does not have required role(s)"``. So we verify the role
    with :func:`character_is_corp_director` (``is True`` only — an unknown ``None`` is
    never trusted). Other corp syncs avoid this trap only because their scopes are
    Director-only opt-in features, so scope-presence happens to imply the role.
    """
    for character in EveCharacter.objects.filter(corporation_id=corp_id, is_corp_member=True):
        if not character.tokens.filter(revoked_at__isnull=True).exists():
            continue
        try:
            get_valid_access_token(character, [CORP_KILLMAILS_SCOPE])
        except NoValidToken:
            continue
        if character_is_corp_director(character) is True:
            return character
    return None


def corp_killmail_feed_token_present(corp_id: int) -> bool:
    """Cheap, ESI-free "does a corp-killmail feed token even exist?" check.

    This is the DB half of :func:`_find_corp_killmail_director` — a live, non-revoked token
    on a corp member that carries the corp-killmails scope — **without** the ESI Director-role
    verification (which fans out network calls per candidate). The setup wizard uses this so a
    page load never triggers ESI: it pairs the cheap "a candidate token exists" signal with the
    authoritative "the corp feed actually polled successfully" signal from ``IngestSourceHealth``.
    Sharing :data:`CORP_KILLMAILS_SCOPE` keeps the two checks in lockstep.
    """
    from apps.sso.models import AuthToken

    return AuthToken.objects.filter(
        character__corporation_id=corp_id,
        character__is_corp_member=True,
        revoked_at__isnull=True,
        scopes__contains=[CORP_KILLMAILS_SCOPE],
    ).exists()


@shared_task(name="killboard.rebuild_stats")
def rebuild_stats() -> int:
    return rebuild_corp_metrics()


@shared_task(name="killboard.rebuild_member_stats")
def rebuild_member_stats() -> int:
    """Recompute the per-member combat rollup (all-time). Runs nightly."""
    return rebuild_member_metrics()


@shared_task(name="killboard.scan_watchlist_activity")
def scan_watchlist_activity() -> dict:
    """4.4: opt-in tripwire — alert when a watched entity appears on a fresh killmail.
    No-op unless the governance event is armed + a watchlist has alerts enabled."""
    from .watchlist_alerts import scan_watchlist_activity as _scan

    return _scan()


@shared_task(name="killboard.warm_caches")
def warm_caches() -> int:
    """Keep the public dashboard / killfeed / rankings caches warm (every 5 min)."""
    from .analytics import warm_caches as _warm

    return _warm()


@shared_task(name="killboard.prune_stream_events")
def prune_stream_events() -> dict:
    """KB-29: trim the outbound-stream ring buffer to its retention window (hourly)."""
    from .stream import prune_events

    return prune_events()


@shared_task(name="killboard.dispatch_subscriptions")
def dispatch_subscriptions() -> dict:
    """KB-30: match fresh stream events against enabled per-pilot subscriptions and deliver.

    A cursor-consumer over the KB-29 ring buffer (my_kill/my_loss/my_loss_srp_pending/
    filter_match); rank_up and watchlist_hit are pushed from their own emitters. No-op when
    the feature is off or nothing new has landed."""
    from .subscriptions import dispatch_subscriptions as _dispatch

    return _dispatch()


@shared_task(name="killboard.refresh_monthly_stats")
def refresh_monthly_stats() -> int:
    """Incrementally refresh the current + previous month's per-pilot ranking
    aggregate (MonthlyPilotKillStat) so historical rankings stay live as new
    killmails arrive. The full history is filled once by ``backfill_monthly_stats``."""
    from .aggregation import refresh_current_months

    return refresh_current_months()


@shared_task(name="killboard.scan_rank_rewards")
def scan_rank_rewards() -> int:
    """Generate pending combat-rank reward events for enrolled pilots who crossed a
    reward-enabled rank above their baseline. No-op unless leadership enabled rewards."""
    from .rewards import scan_and_award

    return scan_and_award()


@shared_task(name="killboard.run_history_import")
def run_history_import(import_id: int) -> str:
    """KB-38: execute one queued setup-wizard history import (WS-D5).

    Thin wrapper over ``history_import.run_import`` — the launcher enqueues this so the heavy,
    network-bound EVE Ref / zKill backfill runs off the request. Returns the terminal state."""
    from .history_import import run_import

    return run_import(import_id)


@shared_task(name="killboard.notify_rank_ups")
def notify_rank_ups() -> int:
    """Send the one-time 'you reached <rank>' celebration to pilots who climbed a
    combat rung since the last scan. Runs nightly after the member rollup; deduped
    per rung; future-only (first-seen pilots are baselined silently)."""
    from .rank_notify import notify_rank_ups as _notify

    return _notify()


@shared_task(name="killboard.scan_trophies")
def scan_trophies() -> dict:
    """KB-37: award trophies (and run newbro pod coaching) for pilots touched by fresh stream
    events. A cursor-consumer over the KB-29 ring buffer — cheap when idle, future-only, awards
    once per pilot per trophy. No-op unless the feature is enabled."""
    from .trophies import scan_trophies as _scan

    return _scan()


@shared_task(name="killboard.pick_kill_of_the_week")
def pick_kill_of_the_week() -> dict:
    """KB-37: pick the most recently completed ISO week's Kill of the Week (top home kill by
    at-kill value). Idempotent; never clobbers an officer override; fires a corp ping on a fresh
    pick."""
    from .kotw import pick_kill_of_the_week as _pick

    return _pick()


@shared_task(name="killboard.snapshot_seasons")
def snapshot_seasons() -> int:
    """KB-37: freeze any recently-completed quarter's leaderboards into a SeasonSnapshot (composed
    from the monthly aggregate). Bounded to recent quarters; idempotent."""
    from .seasons import snapshot_completed_seasons

    return snapshot_completed_seasons()


@shared_task(name="killboard.scan_milestones")
def scan_milestones() -> int:
    """Record + celebrate newbro combat milestones (first kill / solo / final blow).
    Recorded for all; only a recently-achieved first is celebrated (future-only)."""
    from .milestones import scan_milestones as _scan

    return _scan()


@shared_task(name="killboard.discover_all_member_killmails")
def discover_all_member_killmails() -> int:
    """Fan out per-character killmail discovery for all corp members."""
    total = 0
    for character_id in EveCharacter.objects.filter(is_corp_member=True).values_list(
        "character_id", flat=True
    ):
        total += discover_character_killmails(character_id)
    return total


@shared_task(name="killboard.import_from_zkill")
def import_from_zkill(entity_type: str, entity_id: int) -> int:
    """Optional enrichment: pull (id, hash) pairs for a corporation or
    character from zKillboard and ingest the canonical bodies from ESI.

    Used when ESI's own recent-killmail endpoints don't cover the entity's
    history (zKillboard is more complete). entity_type is 'corporation' or
    'character'.
    """
    from core.esi.adapters import zkill

    if entity_type == "corporation":
        refs = zkill.corporation_killmail_refs(entity_id)
    elif entity_type == "character":
        refs = zkill.character_killmail_refs(entity_id)
    else:
        raise ValueError(f"unknown entity_type {entity_type!r}")

    client = ESIClient()
    count = 0
    for killmail_id, killmail_hash in refs:
        try:
            ingest_killmail(killmail_id, killmail_hash, source=Source.ZKILL, client=client)
            count += 1
        except Exception as exc:  # noqa: BLE001 - one bad mail must not stop the batch
            log.warning("zkill ingest failed for %s: %s", killmail_id, exc)

    from core.esi.names import backfill_killmail_names

    backfill_killmail_names()  # resolve pilot/corp names for the new mails
    return count


@shared_task(name="killboard.import_home_corp_from_zkill")
def import_home_corp_from_zkill() -> int:
    """Periodic intraday pull of the home corp's recent killmails from zKillboard.

    zKill is the most complete source: it lists every mail the corp appears on —
    final blows, losses, AND kills the corp was merely *involved* in (an attacker
    without the final blow), which ESI's own feeds under-report. Page 1 (~200 most
    recent) covers far more than one interval's worth of kills, and ``ingest`` is
    idempotent (already-stored mails are skipped before any ESI fetch), so re-pulling
    every cycle only fetches genuinely new bodies. This is the workhorse that keeps
    the killboard current between the daily EVE Ref archive runs.
    """
    home = getattr(settings, "FORCA_HOME_CORP_ID", 0)
    if not home:
        return 0
    from .models import IngestSourceHealth

    try:
        n = import_from_zkill("corporation", home)
        IngestSourceHealth.record("zkill_query", count=n)  # KB-20: observability only
        return n
    except Exception as exc:  # noqa: BLE001 - a zKill blip must not fail the beat cycle
        log.warning("periodic zKill corp import failed: %s", exc)
        IngestSourceHealth.record("zkill_query", error=str(exc))
        return 0


@shared_task(name="killboard.discover_home_corp_killmails")
def discover_home_corp_killmails() -> int:
    """Periodic authoritative pull of the home corp's killmails via ESI.

    Uses a Director/role-holder token with the corp-killmails scope to read CCP's
    own ``/corporations/{id}/killmails/recent/`` feed — the canonical, low-latency
    source for the corp's final blows and losses. Complements the zKill import
    (which is broader but can lag a few minutes). No-op (logged) when no corp token
    with the scope is available.
    """
    home = getattr(settings, "FORCA_HOME_CORP_ID", 0)
    if not home:
        return 0
    director = _find_corp_killmail_director(home)
    if not director:
        log.info("corp killmail discovery: no corp token with the killmails scope")
        return 0
    from .models import IngestSourceHealth

    # KB-20: record health on the primary's own return WITHOUT altering its behaviour — the
    # discovery call keeps propagating any unexpected error exactly as before (it already
    # swallows ESIError/NoValidToken internally and returns a count).
    n = discover_corporation_killmails(home, director.character_id)
    IngestSourceHealth.record("esi_corp", count=n)
    return n


@shared_task(name="killboard.consume_killstream")
def consume_killstream() -> dict:
    """KB-20: OPTIONAL realtime killmail fallback via zKillboard's R2Z2 sequence feed.

    A no-op unless leadership has enabled it (dark-launched off). The authoritative feeds
    (ESI Director corp feed, the zKill query poll, EVE Ref archives) always run regardless;
    this only lowers latency for recent home-corp kills when switched on.
    """
    from .killstream import consume_killstream as _consume

    return _consume()


@shared_task(name="killboard.resolve_names")
def resolve_names() -> int:
    """Resolve any unresolved character/corp/alliance names referenced by killmails."""
    from core.esi.names import backfill_killmail_names

    return backfill_killmail_names()


@shared_task(name="killboard.ingest_killmail")
def ingest_killmail_task(killmail_id: int, killmail_hash: str, source: str = Source.ESI_CORP) -> None:
    ingest_killmail(killmail_id, killmail_hash, source=source)


@shared_task(name="killboard.discover_character_killmails")
def discover_character_killmails(character_id: int) -> int:
    """Discover and ingest a character's recent killmails (kills + losses)."""
    character = EveCharacter.objects.filter(character_id=character_id).first()
    if not character:
        return 0
    try:
        access = get_valid_access_token(character, ["esi-killmails.read_killmails.v1"])
    except NoValidToken:
        return 0
    client = ESIClient()
    try:
        refs = client.get_paged(
            f"/characters/{character_id}/killmails/recent/", token=access
        )
    except ESIError as exc:
        log.warning("char killmail discovery failed for %s: %s", character_id, exc)
        return 0
    count = 0
    for ref in refs:
        ingest_killmail(
            ref["killmail_id"], ref["killmail_hash"], source=Source.ESI_CHAR, client=client
        )
        count += 1
    return count


@shared_task(name="killboard.discover_corporation_killmails")
def discover_corporation_killmails(corporation_id: int, director_character_id: int) -> int:
    """Discover and ingest a corporation's recent killmails (Director token)."""
    director = EveCharacter.objects.filter(character_id=director_character_id).first()
    if not director:
        return 0
    try:
        access = get_valid_access_token(
            director, ["esi-killmails.read_corporation_killmails.v1"]
        )
    except NoValidToken:
        return 0
    client = ESIClient()
    try:
        refs = client.get_paged(
            f"/corporations/{corporation_id}/killmails/recent/", token=access
        )
    except ESIError as exc:
        log.warning("corp killmail discovery failed for %s: %s", corporation_id, exc)
        return 0
    count = 0
    for ref in refs:
        ingest_killmail(
            ref["killmail_id"], ref["killmail_hash"], source=Source.ESI_CORP, client=client
        )
        count += 1
    return count


@shared_task(name="killboard.run_kill_feed")
def run_kill_feed() -> dict:
    """Post qualifying corp kills/losses to Discord. No-op unless an officer enabled it."""
    from .killfeed import run_kill_feed as _run

    return _run()


@shared_task(name="killboard.auto_cluster_battles")
def auto_cluster_battles(window_hours: int = 6, min_kills: int = 5, report_hours: int = 24) -> int:
    """Auto-generate battle reports for systems with a recent home-corp killmail
    cluster (KB-12), so officers don't have to spot and file each engagement by hand.

    A system is clustered when at least ``min_kills`` home-corp killmails landed
    there in the last ``window_hours``; the report itself spans ``report_hours``.
    Idempotent: a system already covered by a report created within this window is
    skipped, so repeated beat runs don't spam near-duplicate reports.
    """
    from datetime import timedelta

    from django.db.models import Count
    from django.utils import timezone

    from .battle import generate_battle_report
    from .models import BattleReport, Killmail

    home = getattr(settings, "FORCA_HOME_CORP_ID", 0)
    if not home:
        return 0
    now = timezone.now()
    since = now - timedelta(hours=window_hours)
    # Dedup against the REPORT span, not the (shorter) cluster window: a 24h report
    # must suppress re-runs for as long as it covers the system, or a sustained
    # battle would spawn a fresh overlapping report once `since` passes its creation.
    dedup_since = now - timedelta(hours=max(window_hours, report_hours))
    active = (
        Killmail.objects.filter(involves_home_corp=True, killmail_time__gte=since)
        .values("solar_system_id")
        .annotate(n=Count("killmail_id"))
        .filter(n__gte=min_kills)
    )
    created = 0
    for row in active:
        system_id = row["solar_system_id"]
        # Dedup: an existing report covering this system from the report window is
        # treated as the ongoing battle; on-demand regeneration handles updates.
        if BattleReport.objects.filter(
            system_ids__contains=[system_id], created_at__gte=dedup_since
        ).exists():
            continue
        try:
            report = generate_battle_report(system_id, hours=report_hours)
        except Exception:  # noqa: BLE001 — one bad system must not abort the cycle
            log.exception("auto-cluster failed for system %s", system_id)
            continue
        if report is not None:
            created += 1
    return created


@shared_task(name="killboard.signature_tick")
def signature_tick_task() -> dict:
    """Combat Signatures refresh tick (every 10 min): mark the live signatures of pilots touched by
    fresh kills dirty, run the membership freeze/unfreeze sweep, and re-render the due banners off
    the request path. A cursor-consumer over the KB-29 ring buffer — cheap when idle; no-op unless
    the feature is armed. Coalesced + debounced + mutex-guarded so overlapping beats never storm."""
    from .signature_pipeline import signature_tick

    return signature_tick()


@shared_task(name="killboard.signature_render")
def signature_render_task(signature_id: int) -> str:
    """Render one Combat Signature now — the manual-regenerate path (WS-6 editor button / admin
    console). Clears the per-signature debounce and resets the failure ledger before rendering."""
    from .signature_pipeline import force_render

    return force_render(signature_id)


@shared_task(name="killboard.signature_cleanup")
def signature_cleanup_task() -> int:
    """Delete orphaned signature artifacts (no row, or a disabled signature) from media. Daily.
    Never removes an active/frozen signature's live image; returns -1 if the media dir is missing."""
    from .signature_pipeline import cleanup_orphans

    return cleanup_orphans()
