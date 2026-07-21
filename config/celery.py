"""Celery application for background ESI sync and recommendation jobs."""
from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("forca")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Periodic schedule. Cadences are at/above ESI cache TTLs and staggered; tune
# in production. Per-character syncs fan out from the orchestrator tasks.
app.conf.beat_schedule = {
    "refresh-affiliations": {
        "task": "sso.refresh_affiliations",
        "schedule": crontab(minute=7, hour="*/6"),
    },
    # Bounded, staleness-filtered in-game Director-role reconcile (4.8), decoupled from
    # the affiliation sweep so the Director ESI check can't fan out with alt count.
    "reconcile-director-roles": {
        "task": "sso.reconcile_director_roles",
        "schedule": crontab(minute=37, hour="*/6"),
    },
    # Collapse redundant OAuth tokens (a newer token with equal-or-wider
    # scopes supersedes older ones) — keeps the health page's token table at
    # one meaningful row per character per scope set.
    "prune-superseded-tokens": {
        "task": "sso.prune_superseded_tokens",
        "schedule": crontab(minute=10, hour=4),
    },
    # Reconcile recorded scope grants against CCP-authoritative token claims (4.7).
    # Staleness-filtered + per-run capped, so it protects (not spends) the ESI budget.
    "reconcile-scopes": {
        "task": "sso.reconcile_scopes",
        "schedule": crontab(minute=35, hour="*/6"),
    },
    "discover-member-killmails": {
        "task": "killboard.discover_all_member_killmails",
        "schedule": crontab(minute="*/10"),
    },
    # Intraday corp killmail feed from zKillboard — the primary backfill that keeps
    # the board current (kills, losses AND involved-but-not-final-blow mails that
    # the ESI feeds under-report). Every 15 min; page 1 covers far more than that.
    "import-home-corp-zkill": {
        "task": "killboard.import_home_corp_from_zkill",
        "schedule": crontab(minute="*/15"),
    },
    # Authoritative corp killmail feed via a Director token (ESI). Complements the
    # zKill pull above; offset by a few minutes so the two don't fire together.
    "discover-home-corp-killmails": {
        "task": "killboard.discover_home_corp_killmails",
        "schedule": crontab(minute="7-59/15"),
    },
    "sync-member-skills": {
        "task": "characters.sync_all_member_skills",
        "schedule": crontab(minute=23, hour="*/12"),
    },
    "rebuild-stats": {
        "task": "killboard.rebuild_stats",
        # Offset off minute 0 so the heavy warmers don't all contend for cores at the
        # top of the hour (same 15-min cadence, phase-shifted). See also the warmers below.
        "schedule": crontab(minute="1-59/15"),
    },
    # Opt-in watchlist activity tripwire (4.4): alert when a watched entity appears on a
    # fresh killmail. Inert unless leadership arms a watchlist + the governance event.
    "scan-watchlist-activity": {
        "task": "killboard.scan_watchlist_activity",
        "schedule": crontab(minute="9-59/15"),
    },
    # Cross-check self-reported courier deliveries against the in-game contracts
    # (corp or hauler token) so a haul only earns full credit once verified.
    "reconcile-courier-contracts": {
        "task": "logistics.reconcile_courier_contracts",
        "schedule": crontab(minute="13-59/20"),
    },
    # Snapshot all corp contracts for the oversight board, hourly. No-op (logged)
    # until a director grants the corp-contracts scope.
    "sync-corp-contracts": {
        "task": "logistics.sync_corp_contracts",
        "schedule": crontab(minute=48),
    },
    # LOG-1 (3.2): remind haulers before their deadline and auto-release overdue hauls back
    # to the pool (every 15 min). The release is unconditional; the DMs are gated by the
    # logistics.haul_overdue event.
    "logistics-sweep-hauls": {
        "task": "logistics.sweep_hauls",
        "schedule": crontab(minute="7,22,37,52"),
    },
    # P6: flip freight batches ARRIVED from a verified contract / completed haul and
    # flag late ones. SHIPS INERT (FreightConfig.eta_sweep_enabled default False → one
    # cheap config read per firing). Hourly at the least-contended rung: :29 shares only
    # with the per-minute pingboard dispatch ticks + raffle-recompute-summaries
    # (14-59/15), and is odd so it dodges the */2 pingboard retry. (The plan's earlier
    # :49 suggestion is now taken by procurement-match-contracts — the beat ledger lies;
    # re-scanned at implementation time.)
    "logistics-sweep-freight-batches": {
        "task": "logistics.sweep_freight_batches",
        "schedule": crontab(minute=29),
    },
    # Keep the public killboard caches (dashboard, killfeed, rankings) continuously
    # warm so visitors never trigger a cold full-table aggregation. Cadence is
    # below the 900s cache TTL so a warmed key never lapses between cycles.
    "warm-killboard-caches": {
        "task": "killboard.warm_caches",
        "schedule": crontab(minute="*/5"),
    },
    # Post sizeable corp kills/losses to Discord. No-op until an officer enables the
    # feed; thresholds are leadership-tunable. Every 10 min, below the fresh window.
    "run-kill-feed": {
        "task": "killboard.run_kill_feed",
        "schedule": crontab(minute="*/10"),
    },
    # Auto-cluster recent home-corp killmails into battle reports (KB-12) so
    # engagements are filed without an officer spotting each one. Dedup keeps
    # repeated runs from creating near-duplicate reports. Staggered off the */15
    # and */10 killboard jobs.
    "auto-cluster-battles": {
        "task": "killboard.auto_cluster_battles",
        "schedule": crontab(minute="11-59/20"),
    },
    # KB-20: OPTIONAL realtime killmail fallback (zKillboard R2Z2). Safe to always
    # schedule — the task no-ops instantly (one DB read, zero HTTP) unless leadership has
    # enabled it. The authoritative ESI/zKill-query/EVE Ref feeds keep the board current
    # regardless; this only lowers latency for recent home-corp kills when switched on.
    "consume-killstream": {
        "task": "killboard.consume_killstream",
        "schedule": crontab(minute="*"),
    },
    # KB-29: trim the outbound-stream (SSE/poll) event ring buffer to its retention window.
    # A cheap seq-threshold delete; hourly is ample. Minute 34 is otherwise unclaimed by the
    # recurring patterns (its hour-3 neighbours are the nightly rollups, not this hourly job).
    "prune-killboard-stream-events": {
        "task": "killboard.prune_stream_events",
        "schedule": crontab(minute=34),
    },
    # KB-30: per-pilot subscriptions — the cursor-consumer that matches fresh stream events
    # (my_kill/my_loss/my_loss_srp_pending/filter_match) against enabled subscriptions and
    # delivers. Cheap when idle (one indexed read); every 2 min keeps personal notifications
    # timely without holding a worker. rank_up/watchlist_hit are pushed from their own emitters.
    "dispatch-killboard-subscriptions": {
        "task": "killboard.dispatch_subscriptions",
        "schedule": crontab(minute="*/2"),
    },
    # Incrementally refresh the per-pilot monthly ranking aggregate (current +
    # previous calendar month) so /killboard/rankings/ historical filters stay live
    # as new kills arrive. The full 15-year history is filled once by the
    # backfill_monthly_stats command. Bounded (two months) — every 30 min, off :00.
    "refresh-monthly-ranking-stats": {
        "task": "killboard.refresh_monthly_stats",
        "schedule": crontab(minute="16,46"),
    },
    # Generate pending combat-rank reward events for enrolled pilots who crossed a
    # reward-enabled rank. No-op (cheap) unless leadership enabled rewards. Every
    # 30 min, off the other killboard cadences.
    "scan-combat-rank-rewards": {
        "task": "killboard.scan_rank_rewards",
        "schedule": crontab(minute="26,56"),
    },
    # Keep the current Hall of Fame warm in cache (it aggregates millions of
    # killmail-participant rows). Cadence under the 300s read-cache TTL so a viewer
    # never triggers the cold recompute.
    "warm-hall-of-fame": {
        "task": "pilots.warm_hall_of_fame",
        "schedule": crontab(minute="3-59/4"),
    },
    # Daily safety net: freeze the weights of any newly-completed Hall-of-Fame month
    # (4.15) so past boards stop shifting when leadership retunes weights.
    "freeze-hof-months": {
        "task": "pilots.freeze_hof_months",
        "schedule": crontab(minute=20, hour=2),
    },
    # Keep every active pilot's Daily Briefing warm (digest + quest-log scan +
    # onboarding milestones stack on the member landing page). Cadence under the
    # 10-min digest TTL, offset to minute 8 to dodge the */4, */5 and */10 warmers.
    "warm-pilot-briefings": {
        "task": "pilots.warm_briefings",
        "schedule": crontab(minute="8-59/10"),
    },
    # Keep the corp readiness index warm (it scans every member × doctrine). Under
    # the 15-min read-cache TTL so the leadership dashboard is always a cache read.
    "warm-readiness": {
        "task": "readiness.warm",
        "schedule": crontab(minute="4-59/10"),
    },
    # Persist a durable readiness snapshot every 6 hours (00:09/06:09/12:09/18:09 UTC)
    # so the timeline, week-over-week deltas, forecast (needs ≥5 points) and weekly-
    # report movers populate. warm-readiness keeps the cache fresh every 10 min but
    # deliberately does not write history; this dedicated task does (4 rows/day). At
    # minute 9 to avoid the */10 warm ticks and the :14 generate-tasks run.
    "readiness-snapshot": {
        "task": "readiness.snapshot",
        "schedule": crontab(minute=9, hour="*/6"),
    },
    # Auto-generate prep tasks from open findings whose alert rule opts in, and
    # reconcile cleared findings' tasks. Trails the warm tick by ~4 min so it reads
    # fresh findings. Ships inert (no alert rules configured ⇒ no auto-tasks).
    "readiness-generate-tasks": {
        "task": "readiness.generate_tasks",
        "schedule": crontab(minute="14-59/30"),
    },
    # Per-pilot readiness (the lighter compute_pilot pipeline) for the quest log.
    # Offset to minute 6 so it trails the corp warm and avoids the */5 and */10 warmers.
    "readiness-warm-pilots": {
        "task": "readiness.warm_pilots",
        "schedule": crontab(minute="6-59/30"),
    },
    # Evaluate alert rules over fresh findings (~4 min after a warm tick). Cooldown
    # dedupes; ships inert until leadership configures readiness.alerts rules.
    "readiness-evaluate-alerts": {
        "task": "readiness.evaluate_alerts",
        "schedule": crontab(minute="4-59/15"),
    },
    # Weekly executive report, Monday 06:00 UTC.
    "readiness-weekly-report": {
        "task": "readiness.weekly_report",
        "schedule": crontab(minute=0, hour=6, day_of_week=1),
    },
    # Retention pruning for readiness history/output, daily 03:40 UTC (offset 40 min
    # after the corp-wide retention sweep so they don't contend).
    "readiness-housekeeping": {
        "task": "readiness.housekeeping",
        "schedule": crontab(minute=40, hour=3),
    },
    # Command Intelligence weekly briefing, Monday 12:00 UTC (offset from the readiness
    # weekly at 06:00 so the heavier LLM run doesn't collide). Inert until
    # command_intel.notifications.scheduled_enabled; deduped against a recent scheduled run.
    "command-intel-scheduled-report": {
        "task": "command_intel.scheduled_report",
        "schedule": crontab(minute=0, hour=12, day_of_week=1),
    },
    # CI retention pruning (resolved directives + orphan snapshots), daily 03:50 UTC —
    # 10 min after the readiness sweep so the nightly prunes stagger.
    "command-intel-housekeeping": {
        "task": "command_intel.housekeeping",
        "schedule": crontab(minute=50, hour=3),
    },
    # CMD-1 (2.11): auto-AAR — scan recent battles and queue an AAR for notable ones.
    # The beat fires every 30 min but the task no-ops unless battle.auto_aar_enabled is
    # armed; cost-safe (one per battle, per-run cap, LLM rate caps).
    "command-intel-auto-aar": {
        "task": "command_intel.auto_aar",
        "schedule": crontab(minute="12,42"),
    },
    # Doctrine XML-import staging prune (abandoned previews + old terminal batches),
    # daily 03:55 UTC — staggered after the other nightly prunes.
    "doctrines-housekeeping": {
        "task": "doctrines.housekeeping",
        "schedule": crontab(minute=55, hour=3),
    },
    # Relayed defensive-alert history retention (CorpNotification + RelayedMail
    # headers, 90-day window), daily 04:00 UTC.
    "recommendations-housekeeping": {
        "task": "recommendations.housekeeping",
        "schedule": crontab(minute=0, hour=4),
    },
    # Guard-railed autonomous COA proposal, daily 04:10 UTC. INERT until a director arms
    # command_intel.autonomous.enabled (the kill switch) — the beat fires but the task
    # returns "disabled" and proposes nothing until then; calibration-gated when armed.
    "command-intel-autonomous-propose": {
        "task": "command_intel.autonomous_propose",
        "schedule": crontab(minute=10, hour=4),
    },
    # Nudge opted-in pilots whose skill queue ran dry (SKL-4). Hourly: queues change
    # slowly, and only characters that look idle get a fresh ESI re-check. No-op unless
    # someone opted in and the skills.idle_queue event is enabled.
    "skills-idle-queue-nudge": {
        "task": "skills.notify_idle_queues",
        "schedule": crontab(minute=25),
    },
    # Per-member combat rollup is heavier and not time-critical — once a night.
    "rebuild-member-stats": {
        "task": "killboard.rebuild_member_stats",
        "schedule": crontab(minute=20, hour=3),
    },
    # Fire one-time combat rank-up celebrations off the fresh member rollup (15 min
    # after it), so a pilot's "you made <rank>!" note reflects the night's kills.
    "notify-combat-rank-ups": {
        "task": "killboard.notify_rank_ups",
        "schedule": crontab(minute=35, hour=3),
    },
    # Record + celebrate newbro combat milestones (first kill / solo / final blow).
    # Only pilots still missing a milestone are scanned, so it's cheap; nightly.
    "scan-newbro-milestones": {
        "task": "killboard.scan_milestones",
        "schedule": crontab(minute=45, hour=3),
    },
    # Resolve pilot/corp/alliance names for newly-discovered killmails. The
    # 10-minute discovery only ingests bodies; without this, names referenced by
    # fresh mails would render as raw ids until something else backfills them.
    "resolve-killmail-names": {
        "task": "killboard.resolve_names",
        "schedule": crontab(minute=35, hour="*/2"),
    },
    "run-recommendations": {
        "task": "recommendations.run",
        "schedule": crontab(minute="2-59/30"),
    },
    # Public market history is cached ~24h and rolls over after the daily
    # downtime (~11:00 UTC); refresh once a day at 11:30 — faster is wasted work.
    "sync-market-history": {
        "task": "market.sync_history",
        "schedule": crontab(minute=30, hour=11),
    },
    # Catch-up guard: if the 11:30 run failed (worker down, ESI outage) this
    # re-runs the sync until the health stamp is <20h old, keeping worst-case
    # history staleness inside ~24h. No-ops (one AppSetting read) when fresh.
    "ensure-market-history-fresh": {
        "task": "market.ensure_history_fresh",
        "schedule": crontab(minute=50, hour="*/4"),
    },
    # CCP recomputes adjusted/average reference prices once a day (after the
    # ~11:00 UTC downtime). Refresh just after so the price_for fallback stays
    # current for off-market types and historical killmail items.
    "sync-adjusted-prices": {
        "task": "market.sync_adjusted_prices",
        "schedule": crontab(minute=45, hour=11),
    },
    # Live Jita price refresh (JITA_SELL from Fuzzwork) — the authoritative signal
    # behind price_for (store quotes, SRP payouts, industry costs, killmail values).
    # Without it these silently fell back to the once-daily CCP adjusted reference;
    # daily at 12:15 UTC (after the adjusted sync + downtime) it re-prices all
    # referenced types, then re-values the killboard + recomputes BOMs.
    "sync-jita-prices": {
        "task": "market.sync_jita_prices",
        "schedule": crontab(minute=15, hour=12),
    },
    # Keep the market dashboard's expensive trade signals (margins + build-vs-buy)
    # warm in cache so a visitor never triggers the full computation. Cadence is
    # under the 30-min cache TTL so a warmed key never lapses.
    "warm-market-dashboard": {
        "task": "market.warm_dashboard",
        "schedule": crontab(minute="5-59/20"),
    },
    # Pre-fetch the public ESI map overlays (jumps/kills/sov/FW) so a map page
    # never makes ESI calls in-request on a cold cache. Under the ~1h overlay TTL.
    "warm-map-overlays": {
        "task": "navigation.warm_map_overlays",
        "schedule": crontab(minute="2,52"),
    },
    # Opt-in saved-route camp/incursion push (4.5). Runs a few minutes after the overlay
    # warm so it reads a fresh camp/incursion cache. Inert unless routes are watched.
    "scan-route-watches": {
        "task": "navigation.scan_route_watches",
        "schedule": crontab(minute="7,37"),
    },
    # Corp assets are cached ~1h server-side; every 6h keeps the stockpile fresh
    # without hammering ESI. No-op (logged) until a Director grants the scope.
    "sync-corp-assets": {
        "task": "stockpile.sync_corp_assets",
        "schedule": crontab(minute=15, hour="*/6"),
    },
    # Personal assets (same ~1h cache); offset from the corp sync so they don't
    # burst together. Only pilots who granted the asset scope are touched.
    "sync-personal-assets": {
        "task": "stockpile.sync_personal_assets",
        "schedule": crontab(minute=40, hour="*/6"),
    },
    # Member tracking is cached ~1h server-side; refresh every 6h. No-op (logged)
    # until a Director grants the member-tracking scope.
    "sync-corp-members": {
        "task": "corporation.sync_members",
        "schedule": crontab(minute=50, hour="*/6"),
    },
    "enforce-retention": {
        "task": "admin_audit.enforce_retention",
        "schedule": crontab(minute=0, hour=3),
    },
    # Apply the on-member-leave retention policy to departed members' data, daily
    # 03:05 UTC. Ships DISARMED — report-only (writes a report, deletes nothing) until
    # a Director arms it on the retention page.
    "enforce-member-leave": {
        "task": "admin_audit.enforce_member_leave",
        "schedule": crontab(minute=5, hour=3),
    },
    # Weekly dependency-vulnerability scan (pip-audit). Surfaces a newly-disclosed
    # CVE in an installed package as a director Recommendation without waiting for a
    # manual review. Mondays 06:30 UTC — off-peak and staggered from the nightly jobs.
    "audit-dependencies": {
        "task": "admin_audit.audit_dependencies",
        "schedule": crontab(minute=30, hour=6, day_of_week=1),
    },
    # ADM-3 (2.2): watch the integration-health surface (stopped beats, stale SDE,
    # dependency CVEs) and fire one deduped director alert on state change. Every
    # 30 min, offset from the other jobs. Deduped + no-op when leadership disables it.
    "scan-integration-health": {
        "task": "admin_audit.scan_integration_health",
        "schedule": crontab(minute="17,47"),
    },
    # SRP-2 (2.7): watch the SRP queue against its readiness SLA/solvency thresholds
    # and fire one deduped SRP-officer digest on a breach-set change. Every 2h.
    "srp-scan-sla": {
        "task": "srp.scan_sla",
        "schedule": crontab(minute=40, hour="*/2"),
    },
    # SRP-4 (4.6): auto-draft SUBMITTED claims for eligible attended-op losses (never
    # auto-pays). Hourly; inert unless leadership arms auto-draft.
    "srp-auto-draft": {
        "task": "srp.auto_draft_claims",
        "schedule": crontab(minute=18),
    },
    # 4.20: reconcile corp-funded guaranteed buyouts against the corp wallet journal
    # (read-only). Every 20 min; inert unless the feature is armed for ESI reconcile.
    "buyback-reconcile-guaranteed": {
        "task": "buyback.reconcile_guaranteed",
        "schedule": crontab(minute="11-59/20"),
    },
    # SSO-2 (2.1): nudge the owning director when a corp-ingestion token dies. Every
    # 30 min, offset from the integration-health scan; one alert per token death.
    "scan-ingestion-tokens": {
        "task": "sso.scan_ingestion_tokens",
        "schedule": crontab(minute="9,39"),
    },
    # CORP-3 (2.3): watch structure fuel + sov ADM against the leadership-set thresholds
    # and fire one deduped officer digest on a breach-set change. Every 2h.
    "scan-infrastructure-alerts": {
        "task": "corporation.scan_infrastructure_alerts",
        "schedule": crontab(minute=25, hour="*/2"),
    },
    # Ansiblex + cyno-beacon network changes rarely — refresh once a day. No-op
    # (logged) until a Director grants the structures scope.
    "sync-jump-network": {
        "task": "navigation.sync_jump_network",
        "schedule": crontab(minute=25, hour=4),
    },
    # Relay in-game notifications (structure attacks, wars, sov) every 30 min. No-op
    # (logged) until a role-holder grants the notifications scope.
    "sync-notifications": {
        "task": "recommendations.sync_notifications",
        "schedule": crontab(minute="*/30"),
    },
    # Relay corp/alliance mailing-list mail to Discord every 15 min. No-op (logged)
    # until the subscribed character grants the mail scope.
    "relay-corp-mail": {
        "task": "recommendations.relay_mail",
        "schedule": crontab(minute="9-59/15"),
    },
    # Corp wallet balances + journal. No-op (logged) until a role-holder grants the
    # corp-wallet scope.
    "sync-corp-wallets": {
        "task": "corporation.sync_wallets",
        "schedule": crontab(minute=45, hour="*/6"),
    },
    # Keep the default Corp Finance dashboard (30d) warm so it's a cache read.
    "warm-finance-dashboard": {
        "task": "corporation.warm_finance",
        "schedule": crontab(minute="15-59/20"),
    },
    # Corp standings/contacts. No-op (logged) until a role-holder grants the scope.
    "sync-corp-contacts": {
        "task": "corporation.sync_contacts",
        "schedule": crontab(minute=55, hour="*/12"),
    },
    # Scheduled moon extractions. No-op (logged) until a role-holder grants the scope.
    "sync-moon-extractions": {
        "task": "corporation.sync_extractions",
        "schedule": crontab(minute=5, hour="*/6"),
    },
    # Opt-in chunk-arrival reminders ahead of each fracture (MIN-3 / 3.13). Every 20 min so
    # the 24h/1h offsets are hit promptly; inert until leadership arms the event + channels.
    "sweep-chunk-reminders": {
        "task": "corporation.sweep_chunk_reminders",
        "schedule": crontab(minute="*/20"),
    },
    # Corp structures — fuel + state + reinforcement timers. Cached ~1h server-side;
    # hourly keeps fuel countdowns honest. No-op (logged) until corp_structures granted.
    "sync-corp-structures": {
        "task": "corporation.sync_structures",
        "schedule": crontab(minute=33),
    },
    # Corp mining ledger (participation). No-op (logged) until the mining scope is granted.
    "sync-mining-ledger": {
        "task": "mining.sync_ledger",
        "schedule": crontab(minute=15, hour="*/6"),
    },
    # MIN-4 (3.10): award recognition for newly-crossed mining milestones (after the ledger
    # sync). First run baselines everyone future-only; thereafter new crossings credit.
    "scan-mining-milestones": {
        "task": "mining.scan_milestones",
        "schedule": crontab(minute=40, hour="*/6"),
    },
    # Our alliance's sovereignty structures (ADM). Public ESI, no token; cached ~1h.
    # No-op (count 0) unless the home corp's alliance holds sov.
    "sync-sovereignty": {
        "task": "operations.sync_sovereignty",
        "schedule": crontab(minute=52),
    },
    # Auto-cancel operations whose RSVP deadline passed without the minimum pilots
    # / required composition (unless the organiser overrode it). Records analytics.
    "operations-auto-cancel": {
        "task": "operations.auto_cancel_expired",
        "schedule": crontab(minute="*/10"),
    },
    # One T-minus form-up reminder to each committed pilot before their op. Frequent
    # cadence so the lead window is hit promptly; each commitment is reminded once.
    "operations-formup-reminders": {
        "task": "operations.formup_reminders",
        "schedule": crontab(minute="*/5"),
    },
    # Spawn upcoming op instances from active recurring templates (OPS-4 / 3.12). Idempotent;
    # once an hour is ample for a days-ahead lead window.
    "operations-materialize-recurring": {
        "task": "operations.materialize_recurring_ops",
        "schedule": crontab(minute=25),
    },
    # Corp owned blueprints (ME/TE) — cached ~1h server-side; every 6h keeps
    # blueprint coverage honest. No-op (logged) until a Director grants corp_industry.
    "sync-corp-blueprints": {
        "task": "erp.sync_blueprints",
        "schedule": crontab(minute=20, hour="*/6"),
    },
    # Corp industry jobs (what's in production right now); offset from the blueprint
    # sync. No-op (logged) until a Director grants corp_industry.
    "sync-corp-industry-jobs": {
        "task": "erp.sync_industry_jobs",
        "schedule": crontab(minute=35, hour="*/3"),
    },
    # Per-pilot industry jobs + owned blueprints; only pilots who granted the opt-in
    # my_industry scope are imported, others skipped. Offset from the corp syncs.
    "sync-character-industry": {
        "task": "erp.sync_character_industry",
        "schedule": crontab(minute=50, hour="*/6"),
    },
    # Daily leadership-briefing digest out to Discord + email. Fires at 12:00 UTC
    # (mid-EU evening / US daytime). No-op until a webhook channel or briefing
    # email recipients are configured.
    "deliver-leadership-briefing": {
        "task": "pilots.deliver_leadership_briefing",
        "schedule": crontab(minute=0, hour=12),
    },
    # --- Mentorship Program ------------------------------------------------
    # Recompute mentor/mentee eligibility off the request path (public ESI,
    # heavily cached). Daily is plenty — character age & corp tenure barely move.
    "mentorship-refresh-eligibility": {
        "task": "mentorship.refresh_eligibility",
        "schedule": crontab(minute=40, hour=5),
    },
    # Auto-suggest a best-match mentor for each unpaired active cadet (idempotent —
    # propose_pairing refuses duplicates, and each suggestion DMs the counterparty).
    # Daily 05:45 UTC, just after the eligibility refresh so it matches on fresh data.
    "mentorship-auto-suggest": {
        "task": "mentorship.auto_suggest_pairings",
        "schedule": crontab(minute=45, hour=5),
    },
    # Re-run auto-checks for tasks waiting on ESI/internal data to land (skills,
    # killmails, mining, industry sync on their own cadences). Every 30 min.
    "mentorship-sweep-api-validations": {
        "task": "mentorship.sweep_api_validations",
        "schedule": crontab(minute="*/30"),
    },
    # Anomaly sweep (farming / rubber-stamping / stale pairs) → leader flags. Hourly.
    "mentorship-scan-anomalies": {
        "task": "mentorship.scan_anomalies",
        "schedule": crontab(minute=15, hour="*"),
    },
    # Expire suggested/requested pairings past their TTL. Daily.
    "mentorship-expire-stale-pairings": {
        "task": "mentorship.expire_stale_pairings",
        "schedule": crontab(minute=50, hour=5),
    },
    # Grant "pairing stayed active N days" rewards. Daily.
    "mentorship-reward-active-days": {
        "task": "mentorship.reward_active_days",
        "schedule": crontab(minute=55, hour=5),
    },
    # Optional live presence check during scheduled sessions (opt-in scope only).
    # Real-time only, so poll frequently while sessions may be running.
    "mentorship-poll-session-presence": {
        "task": "mentorship.poll_session_presence",
        "schedule": crontab(minute="*/10"),
    },
    # Refresh imported PI colonies for pilots who granted the opt-in planets scope.
    # ESI only refreshes layout when the pilot opens the colony in-client, so a few
    # times a day is plenty. Pilots without the scope are skipped (no ESI call).
    "planetary-sync-colonies": {
        "task": "planetary.sync_colonies",
        "schedule": crontab(minute=35, hour="*/6"),
    },
    # Re-cost active PI plans from current prices so plan cards stay fresh. Daily,
    # after the market adjusted-price sync (11:45 UTC).
    "planetary-recost-active-plans": {
        "task": "planetary.recost_active_plans",
        "schedule": crontab(minute=20, hour=12),
    },
    # Pingboard: fire scheduled alerts whose time has come. A cheap due-table sweep
    # (the house idiom — not per-alert apply_async(eta=)); tolerates missed ticks.
    "pingboard-dispatch-due": {
        "task": "pingboard.dispatch_due",
        "schedule": crontab(minute="*"),
    },
    # Pingboard: re-dispatch retryable failed deliveries with backoff, under the cap.
    "pingboard-retry-failed": {
        "task": "pingboard.retry_failed",
        "schedule": crontab(minute="*/2"),
    },
    # Pingboard Calendar: sweep the source tables (ops, moon, structure, industry,
    # mentorship) into idempotent calendar events. A sweep (not signals) so it is
    # reliable even for bulk-written sources (corp industry jobs). Every 10 min.
    "pingboard-sync-calendar": {
        "task": "pingboard.sync_calendar",
        "schedule": crontab(minute="*/10"),
    },
    # Pingboard Calendar: materialise due reminder schedules into alerts (draft-until-
    # approved by default). Frequent due-table sweep.
    "pingboard-materialise-reminders": {
        "task": "pingboard.materialise_reminders",
        "schedule": crontab(minute="*"),
    },
    # Pingboard: evaluate threshold/scan automation rules (structure fuel-low, moon
    # fracture-ready, industry job completing). No-op unless a rule is armed. Every 15 min.
    "pingboard-evaluate-automation": {
        "task": "pingboard.evaluate_automation",
        "schedule": crontab(minute="*/15"),
    },
    # Pingboard: prune old terminal alerts, past calendar events and sync records.
    # Age-based (a missed night self-heals). Nightly, staggered with the other prunes.
    "pingboard-housekeeping": {
        "task": "pingboard.housekeeping",
        "schedule": crontab(minute=45, hour=3),
    },
    # Comms access sync: periodic full reconcile of external-platform roles (Discord/
    # Slack/Mumble) against corp-membership + RBAC. Catches grant EXPIRIES (no push signal
    # fires at expiry). A cheap early-return unless COMMS_ACCESS_ENABLED and a platform is
    # armed. Every 30 min, offset off :00.
    "commsaccess-reconcile-all": {
        "task": "commsaccess.reconcile_all",
        "schedule": crontab(minute="17,47"),
    },
    # Raffle: open scheduled contests / close ended ones (freezes the ledger). Cheap
    # state sweep, tolerant of missed ticks. Offset off :00.
    "raffle-lifecycle": {
        "task": "raffle.lifecycle",
        "schedule": crontab(minute="2-59/5"),
    },
    # Raffle: sweep enabled ticket sources (PVP, mining, fleet, …) for active
    # contests into the append-only ledger. Idempotent re-scan; every 15 min offset
    # off the killboard warmers.
    "raffle-process-sources": {
        "task": "raffle.process_sources",
        "schedule": crontab(minute="9-59/15"),
    },
    # Raffle: rebuild leaderboard summaries (precomputed read model). Every 15 min,
    # phase-shifted from the source sweep so they don't contend.
    "raffle-recompute-summaries": {
        "task": "raffle.recompute_summaries",
        "schedule": crontab(minute="14-59/15"),
    },
    # Raffle: execute automatic draws for closed contests past their draw time. A
    # due-table sweep + a cross-worker lock make a duplicate/missed beat safe.
    "raffle-draw-due": {
        "task": "raffle.draw_due",
        "schedule": crontab(minute="*/5"),
    },
    # Raffle: flag suspicious ticket events for officer review. Hourly.
    "raffle-integrity-scan": {
        "task": "raffle.integrity_scan",
        "schedule": crontab(minute=38, hour="*"),
    },
    # Raffle: warm the ESI-adoption + contest stat caches. Every 30 min, off-peak minute.
    "raffle-refresh-adoption": {
        "task": "raffle.refresh_adoption",
        "schedule": crontab(minute="21,51"),
    },
    # Campaign Command: refresh auto-measured objective values, recompute progress/health.
    # A due-table sweep gated per-source by min refresh intervals; feature-gated, inert
    # when Campaign Command is disabled.
    "campaigns-refresh-metrics": {
        "task": "campaigns.refresh_metrics",
        "schedule": crontab(minute="7-59/15"),
    },
    # Campaign Command: due-soon/overdue/blocked/manual-stale notification sweep.
    # Bucketed idempotency keys make re-runs no-ops.
    "campaigns-sweep-deadlines": {
        "task": "campaigns.sweep_deadlines",
        "schedule": crontab(minute=23),
    },
    # Campaign Command: retention pruning, staggered with the other
    # nightly housekeeping prunes.
    "campaigns-housekeeping": {
        "task": "campaigns.housekeeping",
        "schedule": crontab(minute=35, hour=3),
    },
    # Capsuleer Path: hourly non-skill evidence sweep (contributions, combat firsts, ship
    # ownership). Skill-driven credit rides the import hook, so this beat only covers stores with
    # their own sync cadences. Minute 41 is unused by every existing recurring minute pattern
    # (docs/capsuleer-path/12-background-jobs.md §3); feature-gated, inert when disabled.
    "capsuleer-reconcile-progress": {
        "task": "capsuleer.reconcile_progress",
        "schedule": crontab(minute=41),
    },
    # Capsuleer Path: daily pilot-scoped suggestion generation. 06:22 UTC follows the nightly
    # rollups and the mentorship morning block so every generator reads recent data; minute 22 at
    # hour 6 collides only with cheap due-table sweeps (docs/capsuleer-path/12-background-jobs.md §3.2).
    "capsuleer-generate-suggestions": {
        "task": "capsuleer.generate_suggestions",
        "schedule": crontab(minute=22, hour=6),
    },
    # Capsuleer Path: nightly retention + stalled/review-due evaluation, on the 03:00–04:10 prune
    # ladder — the free rung five minutes after enforce-member-leave (03:05), ten before the
    # member combat rollup (03:20).
    "capsuleer-housekeeping": {
        "task": "capsuleer.housekeeping",
        "schedule": crontab(minute=10, hour=3),
    },
    # Shipyard: release stock reservations of doctrine-fit orders nobody claimed within
    # the leadership-set window. Inert (one policy read) while the shipped default of
    # 0 days keeps the feature off. Hourly at minute 28 — a rung unused by the other
    # recurring patterns.
    "store-expire-reservations": {
        "task": "store.expire_reservations",
        "schedule": crontab(minute=28),
    },
    # MRP v1 (P3): nightly corp-wide net-requirements planning run. INERT until
    # leadership arms MrpConfig.auto_run_enabled (one config read per firing —
    # the store-expire-reservations precedent); manual runs from the Material
    # Plan page are the v1 workflow. 04:05 UTC: the nightly-ladder neighbours
    # are the every-6-hour syncs at 00:05/06:05 — 04:50 and 04:35 LOOK free but
    # are not (market.ensure_history_fresh at :50/*/4 and killboard
    # resolve_names at :35/*/2 both fire in hour 4).
    "industry-run-mrp": {
        "task": "industry.run_mrp",
        "schedule": crontab(minute=5, hour=4),
    },
    # Shipyard demand planning (P2): weekly composed-demand snapshot per fit +
    # 26-week retention prune. Monday 05:35 UTC sits in the Monday-morning weekly
    # block ahead of readiness (06:00) and clear of the 05:40-05:55 mentorship
    # dailies — minute 35 at hour 5 is otherwise unclaimed. Pure internal data
    # collection; deliberately armed (see the task docstring for the stated
    # deviation from the inert-by-default convention).
    "store-snapshot-demand": {
        "task": "store.snapshot_demand",
        "schedule": crontab(minute=35, hour=5, day_of_week=1),
    },
    # Cost & profitability (cross-cutting). Both ship INERT behind MarginConfig flags —
    # one config read until armed. Slots re-verified against the FULL crontab expansion at
    # implementation time (incl. the uncommitted P4/P5/P6 beats), not HEAD:
    #   :53/*/6 — trails the 6-hourly wallet sync (:45/*/6) so fresh journal lines match
    #     same-cycle; the only recurring co-tenant is logistics-reconcile-courier-contracts
    #     (13-59/20 → :53) plus the per-minute pingboard ticks. Odd minute → dodges the
    #     */2 pingboard retry.
    "store-reconcile-order-settlements": {
        "task": "store.reconcile_order_settlements",
        "schedule": crontab(minute=53, hour="*/6"),
    },
    #   04:21 daily — every hour-4 minute carries a recurring co-tenant, so this is the
    #     least-contended, not free: raffle-refresh-adoption (:21,:51) and the P4 inert
    #     procurement-reconcile-payments (1-59/20 → :21, a one-read no-op) share it, plus
    #     the pingboard ticks. (The plan's draft claimed "only raffle" — the P4 beat was
    #     added after; the ledger lies, re-scanned.) Trails the daily Jita sync so the
    #     re-price snapshot is warm.
    "store-check-quote-drift": {
        "task": "store.check_quote_drift",
        "schedule": crontab(minute=21, hour=4),
    },
    # Procurement (P4). All four ship INERT behind ProcurementConfig flags; the beat
    # runs but each task is one config read until armed. Slots re-verified against the
    # full crontab expansion:
    #   :49 hourly — right behind the :48 corp-contracts snapshot so the matcher reads
    #     a fresh snapshot with NO second contracts pull; the only recurring co-tenant
    #     is readiness.evaluate_alerts (4-59/15 → :49), itself a cooldown-deduped inert scan.
    "procurement-match-contracts": {
        "task": "procurement.match_contracts",
        "schedule": crontab(minute=49),
    },
    #   1-59/20 (:01/:21/:41) — a distinct phase from the buyback /20 cadences; co-tenants
    #     are single-minute jobs (killboard.rebuild_stats :01, raffle.refresh_adoption :21,
    #     capsuleer.reconcile_progress :41). A one-read no-op unless armed.
    "procurement-reconcile-payments": {
        "task": "procurement.reconcile_payments",
        "schedule": crontab(minute="1-59/20"),
    },
    #   04:43 daily — the only recurring co-tenant is pilots.warm_hall_of_fame (3-59/4 → :43).
    #     Sweep and reliability share the rung; both are independent no-ops unless armed.
    "procurement-sweep-overdue": {
        "task": "procurement.sweep_overdue",
        "schedule": crontab(minute=43, hour=4),
    },
    "procurement-rollup-reliability": {
        "task": "procurement.rollup_reliability",
        "schedule": crontab(minute=43, hour=4),
    },
    # Supply Command board (cross-cutting): warm the cache + fire the officer problem-set
    # digest. Ships INERT behind BoardConfig.sweep_enabled (one config read until armed).
    # 16-59/20 (:16/:36/:56) — phase-shifted off warm-finance-dashboard's 15-59/20 (adjacent
    # but never coincident); co-tenants re-verified against the full crontab (incl. P4/P5/P6):
    # :16 refresh-monthly-ranking-stats (16,46) + rebuild-stats (1-59/15); :36 readiness-
    # warm-pilots (6-59/30); :56 scan-rank-rewards (26,56). No P4/P5/P6 beat lands on these.
    "supplyboard-sweep": {
        "task": "supplyboard.sweep",
        "schedule": crontab(minute="16-59/20"),
    },
}


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> str:  # pragma: no cover - smoke task
    return f"request: {self.request!r}"
