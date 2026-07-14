# Background Jobs Reference

[FORCA] Command Grid runs its scheduled work through **Celery Beat** (the `beat`
service) dispatching tasks to **Celery workers** (the `worker` service). The schedule is
defined in [`config/celery.py`](../../config/celery.py). Web requests never call ESI or an
LLM directly — all such work happens here.

Key design properties of the schedule:

- **Cadences sit at or above ESI cache TTLs** and are **staggered** across the hour so
  heavy jobs never contend for cores at once.
- **Many jobs ship inert**: they are scheduled but no-op (a cheap early return) until a
  director grants the relevant ESI scope, arms a feature, or configures a rule.
- **Cache warmers** run just under their cache TTL so a visitor never triggers a cold
  recomputation.
- **Results are ignored globally** (no task result is consumed), keeping Redis lean.
- **Workers have no ambient locale**: a task runs without a request, so a job that emits
  prose resolves the language at send time. A per-recipient leg (in-app, EVE-mail, DM) is
  bucketed by each pilot's `User.language` and rendered once per bucket; a leg with no single
  recipient (a Discord webhook, a configured group channel) is rendered once in the corp's
  broadcast locale. That locale is also the fallback for a pilot who has chosen no language,
  and a director sets it at `/ops/admin/i18n/`.

## Table of contents

- [Reading the cadence column](#reading-the-cadence-column)
- [Identity, SSO, and roles](#identity-sso-and-roles)
- [Killboard and combat](#killboard-and-combat)
- [Pilots and Hall of Fame](#pilots-and-hall-of-fame)
- [Readiness](#readiness)
- [Command Intelligence](#command-intelligence)
- [Recommendations and relays](#recommendations-and-relays)
- [Corporation data syncs](#corporation-data-syncs)
- [Industry, market, and assets](#industry-market-and-assets)
- [Operations](#operations)
- [Logistics and SRP](#logistics-and-srp)
- [Navigation](#navigation)
- [Mining and planetary](#mining-and-planetary)
- [Mentorship](#mentorship)
- [Pingboard](#pingboard)
- [Raffle](#raffle)
- [Comms access, doctrines, and housekeeping](#comms-access-doctrines-and-housekeeping)

## Reading the cadence column

Cadences derive from cron expressions. "Every 15 min (offset)" means the job runs four
times an hour on staggered minutes to avoid collisions. Nightly jobs are UTC.

## Identity, SSO, and roles

| Task | Cadence | What it does |
|---|---|---|
| `sso.refresh_affiliations` | Every 6h | Refresh character corp/alliance affiliations |
| `sso.reconcile_director_roles` | Every 6h | Reconcile in-game Director role → app Director role (bounded, staleness-filtered) |
| `sso.prune_superseded_tokens` | Daily 04:10 | Collapse redundant OAuth tokens to one per character/scope set |
| `sso.reconcile_scopes` | Every 6h | Reconcile recorded scope grants against CCP token claims |
| `sso.scan_ingestion_tokens` | Every 30 min | Alert the owning director when a corp-ingestion token dies |

## Killboard and combat

| Task | Cadence | What it does |
|---|---|---|
| `killboard.discover_all_member_killmails` | Every 10 min | Discover member killmails via ESI |
| `killboard.import_home_corp_from_zkill` | Every 15 min | Intraday corp killmail feed from zKillboard |
| `killboard.discover_home_corp_killmails` | Every 15 min (offset) | Authoritative corp killmails via a Director ESI token |
| `killboard.rebuild_stats` | Every 15 min (offset) | Rebuild combat stat rollups |
| `killboard.warm_caches` | Every 5 min | Keep public killboard/killfeed/rankings caches warm |
| `killboard.run_kill_feed` | Every 10 min | Post sizeable kills/losses to Discord (inert until enabled) |
| `killboard.scan_watchlist_activity` | Every 15 min (offset) | Watchlist tripwire alerts (inert until armed) |
| `killboard.auto_cluster_battles` | Every 20 min (offset) | Auto-cluster killmails into battle reports |
| `killboard.refresh_monthly_stats` | Every 30 min | Refresh current+previous month ranking aggregates |
| `killboard.scan_rank_rewards` | Every 30 min | Generate pending combat-rank reward events (inert unless enabled) |
| `killboard.rebuild_member_stats` | Nightly 03:20 | Per-member combat rollup |
| `killboard.notify_rank_ups` | Nightly 03:35 | One-time combat rank-up celebrations |
| `killboard.scan_milestones` | Nightly 03:45 | Record/celebrate newbro combat milestones |
| `killboard.resolve_names` | Every 2h | Resolve pilot/corp/alliance names on fresh killmails |

## Pilots and Hall of Fame

| Task | Cadence | What it does |
|---|---|---|
| `pilots.warm_hall_of_fame` | Every 4 min | Keep the Hall of Fame warm in cache |
| `pilots.freeze_hof_months` | Nightly 02:20 | Freeze completed Hall-of-Fame months' weights |
| `pilots.warm_briefings` | Every 10 min (offset) | Keep each active pilot's Command Center digest warm |
| `pilots.deliver_leadership_briefing` | Daily 12:00 | Deliver the daily leadership briefing to Discord + email |

## Readiness

| Task | Cadence | What it does |
|---|---|---|
| `readiness.warm` | Every 10 min | Recompute and cache the corp readiness index |
| `readiness.snapshot` | Every 6h | Persist a durable readiness history snapshot |
| `readiness.warm_pilots` | Every 30 min (offset) | Per-pilot readiness for the quest log |
| `readiness.evaluate_alerts` | Every 15 min (offset) | Evaluate alert rules over fresh findings (inert until configured) |
| `readiness.generate_tasks` | Every 30 min (offset) | Turn open findings into prep tasks (inert until rules opt in) |
| `readiness.weekly_report` | Monday 06:00 | Weekly executive report |
| `readiness.housekeeping` | Nightly 03:40 | Retention pruning of readiness history/output |

## Command Intelligence

All inert unless `LLM_API_KEY` is set and the relevant feature is armed.

| Task | Cadence | What it does |
|---|---|---|
| `command_intel.scheduled_report` | Monday 12:00 | Weekly strategic briefing |
| `command_intel.auto_aar` | Every 30 min | Scan recent battles, queue after-action reviews (off by default) |
| `command_intel.autonomous_propose` | Nightly 04:10 | Guard-railed autonomous COA proposal (kill-switched off by default) |
| `command_intel.housekeeping` | Nightly 03:50 | Prune resolved directives and orphan snapshots |

## Recommendations and relays

| Task | Cadence | What it does |
|---|---|---|
| `recommendations.run` | Every 30 min (offset) | Run the recommendation engine and dispatch alerts |
| `recommendations.sync_notifications` | Every 30 min | Relay in-game notifications (inert until scope granted) |
| `recommendations.relay_mail` | Every 15 min (offset) | Relay corp/alliance mailing-list mail (inert until scope granted) |
| `recommendations.housekeeping` | Nightly 04:00 | 90-day prune of relayed notifications/mail |

## Corporation data syncs

Each is a cheap logged no-op until a director grants the relevant scope.

| Task | Cadence | What it does |
|---|---|---|
| `corporation.sync_members` | Every 6h (offset) | Member tracking (location/ship/last login) |
| `corporation.sync_wallets` | Every 6h (offset) | Corp wallet balances and journal |
| `corporation.warm_finance` | Every 20 min (offset) | Keep the finance dashboard warm |
| `corporation.sync_contacts` | Every 12h | Corp standings/contacts |
| `corporation.sync_extractions` | Every 6h (offset) | Scheduled moon extractions |
| `corporation.sweep_chunk_reminders` | Every 20 min | Opt-in moon chunk-arrival reminders (inert until armed) |
| `corporation.sync_structures` | Hourly (:33) | Structure fuel/state/reinforcement timers |
| `corporation.scan_infrastructure_alerts` | Every 2h | Deduped structure fuel + sov ADM breach digest |

## Industry, market, and assets

| Task | Cadence | What it does |
|---|---|---|
| `market.sync_history` | Daily 11:30 | Public market history |
| `market.ensure_history_fresh` | Every 4h (offset) | Catch-up guard if the daily history sync failed |
| `market.sync_adjusted_prices` | Daily 11:45 | CCP adjusted/average reference prices |
| `market.sync_jita_prices` | Daily 12:15 | Live Jita prices; re-values killboard and BOMs |
| `market.warm_dashboard` | Every 20 min (offset) | Keep market trade signals warm |
| `stockpile.sync_corp_assets` | Every 6h (offset) | Corp assets (inert until scope granted) |
| `stockpile.sync_personal_assets` | Every 6h (offset) | Personal assets (only pilots who granted the scope) |
| `erp.sync_blueprints` | Every 6h (offset) | Corp owned blueprints (ME/TE) |
| `erp.sync_industry_jobs` | Every 3h (offset) | Corp industry jobs in production |
| `erp.sync_character_industry` | Every 6h (offset) | Per-pilot industry jobs + blueprints (opt-in scope) |

## Operations

| Task | Cadence | What it does |
|---|---|---|
| `operations.sync_sovereignty` | Hourly (:52) | Alliance sov structures + ADM (public ESI) |
| `operations.auto_cancel_expired` | Every 10 min | Auto-cancel under-signed ops past RSVP deadline |
| `operations.formup_reminders` | Every 5 min | One T-minus form-up reminder per committed pilot |
| `operations.materialize_recurring_ops` | Hourly (:25) | Spawn op instances from recurring templates |

## Logistics and SRP

| Task | Cadence | What it does |
|---|---|---|
| `logistics.reconcile_courier_contracts` | Every 20 min (offset) | Verify self-reported hauls against in-game contracts |
| `logistics.sync_corp_contracts` | Hourly (:48) | Snapshot corp contracts for oversight (inert until scope) |
| `logistics.sweep_hauls` | Every 15 min | Remind haulers, auto-release overdue hauls |
| `srp.scan_sla` | Every 2h | Deduped SRP-officer digest on SLA/solvency breach |
| `srp.auto_draft_claims` | Hourly (:18) | Auto-draft SRP claims for eligible attended-op losses (never auto-pays; inert until armed) |
| `buyback.reconcile_guaranteed` | Every 20 min (offset) | Reconcile corp-funded guaranteed buyouts vs wallet journal (inert until armed) |

## Navigation

| Task | Cadence | What it does |
|---|---|---|
| `navigation.warm_map_overlays` | Twice hourly | Pre-fetch public ESI map overlays (jumps/kills/sov/FW) |
| `navigation.scan_route_watches` | Twice hourly (offset) | Opt-in saved-route camp/incursion push (inert until watched) |
| `navigation.sync_jump_network` | Nightly 04:25 | Refresh Ansiblex + cyno network (inert until scope granted) |

## Mining and planetary

| Task | Cadence | What it does |
|---|---|---|
| `mining.sync_ledger` | Every 6h (offset) | Corp mining ledger (inert until scope granted) |
| `mining.scan_milestones` | Every 6h (offset) | Award recognition for mining milestones |
| `planetary.sync_colonies` | Every 6h (offset) | Refresh imported PI colonies (opt-in scope) |
| `planetary.recost_active_plans` | Daily 12:20 | Re-cost active PI plans from current prices |

## Mentorship

| Task | Cadence | What it does |
|---|---|---|
| `mentorship.refresh_eligibility` | Daily 05:40 | Recompute mentor/mentee eligibility (public ESI) |
| `mentorship.auto_suggest_pairings` | Daily 05:45 | Suggest a best-match mentor per unpaired cadet |
| `mentorship.sweep_api_validations` | Every 30 min | Re-run auto-checks awaiting synced data |
| `mentorship.scan_anomalies` | Hourly (:15) | Anomaly sweep → leader flags |
| `mentorship.expire_stale_pairings` | Daily 05:50 | Expire stale suggested/requested pairings |
| `mentorship.reward_active_days` | Daily 05:55 | Grant "pairing stayed active" rewards |
| `mentorship.poll_session_presence` | Every 10 min | Optional live presence check during booked sessions (opt-in scope) |

## Pingboard

| Task | Cadence | What it does |
|---|---|---|
| `pingboard.dispatch_due` | Every minute | Fire scheduled alerts whose time has come |
| `pingboard.retry_failed` | Every 2 min | Re-dispatch retryable failed deliveries with backoff |
| `pingboard.sync_calendar` | Every 10 min | Sweep source tables into calendar events |
| `pingboard.materialise_reminders` | Every minute | Materialise due reminder schedules into alerts |
| `pingboard.evaluate_automation` | Every 15 min | Evaluate threshold/scan automation rules (inert until armed) |
| `pingboard.housekeeping` | Nightly 03:45 | Prune old terminal alerts and past events |

## Raffle

| Task | Cadence | What it does |
|---|---|---|
| `raffle.lifecycle` | Every 5 min (offset) | Open scheduled contests / close ended ones |
| `raffle.process_sources` | Every 15 min (offset) | Sweep enabled ticket sources into the ledger |
| `raffle.recompute_summaries` | Every 15 min (offset) | Rebuild leaderboard summaries |
| `raffle.draw_due` | Every 5 min | Execute automatic draws for closed contests |
| `raffle.integrity_scan` | Hourly (:38) | Flag suspicious ticket events for review |
| `raffle.refresh_adoption` | Twice hourly | Warm ESI-adoption + contest stat caches |

## Comms access, doctrines, and housekeeping

| Task | Cadence | What it does |
|---|---|---|
| `commsaccess.reconcile_all` | Every 30 min (offset) | Full reconcile of external-platform roles vs membership/RBAC (catches grant expiries) |
| `characters.sync_all_member_skills` | Every 12h | Sync member skills |
| `doctrines.housekeeping` | Nightly 03:55 | Prune abandoned doctrine-import previews and old batches |
| `admin_audit.enforce_retention` | Nightly 03:00 | Apply data-retention policy |
| `admin_audit.enforce_member_leave` | Nightly 03:05 | Apply member-leave retention (report-only until armed) |
| `admin_audit.audit_dependencies` | Monday 06:30 | Weekly dependency vulnerability scan (`pip-audit`) |
| `admin_audit.scan_integration_health` | Every 30 min (offset) | Deduped integration-health director alert |

## Operational notes

- If the `beat` service is down, nothing is scheduled; if the `worker` service is down,
  tasks queue in Redis and run when it returns. `admin_audit.scan_integration_health`
  surfaces stopped beats.
- Many jobs are safe to miss: they are due-table sweeps or age-based prunes that
  self-heal on the next run.
- To confirm a task is registered:
  `docker compose -f docker-compose.prod.yml exec worker celery -A config inspect registered`.
