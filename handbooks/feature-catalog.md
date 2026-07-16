# Feature Catalog

This catalogue documents every implemented feature area of [FORCA] Command Grid, grouped
as they appear in the application's **Services & features** console. Each entry states who
it is for, what it does, the key workflows, the roles required, the data and background
jobs involved, and any external integration.

Access terms used below: **public** (anyone), **member** (home-corp pilot),
**officer**/**director** (leadership tiers), and **audience** (a leadership-set
`disabled`/`corp`/`alliance`/`public` visibility). See
[permissions-and-roles.md](./permissions-and-roles.md).

## Table of contents

- [Community and intel](#community-and-intel)
- [Ships and doctrines](#ships-and-doctrines)
- [Fleet and combat](#fleet-and-combat)
- [Navigation](#navigation)
- [Industry and economy](#industry-and-economy)
- [Member services](#member-services)
- [Pilot tools](#pilot-tools)
- [Command and readiness](#command-and-readiness)
- [Leadership](#leadership)
- [Platform and account](#platform-and-account)

---

## Community and intel

### Killboard

- **Users:** everyone (public board); members and alliance for analytics.
- **Purpose:** A zKillboard-style board for the home corporation. Ingests killmails,
  values them, ranks pilots, tags losses against doctrines, and drives combat-rank
  progression, a Discord kill feed, and battle reports. It is the combat-record backbone
  other features read from.
- **Workflows:** Browse the public board and drill-down filters; view a killmail; see
  rankings; (member/alliance) open combat stats, the roster, a per-pilot page, and pilot
  comparison.
- **Roles:** Public: board, rankings, killmail detail. Member/alliance: stats, roster,
  pilot analytics, compare. Officer: kill-feed settings.
- **Data:** Killmails, participants, items, fit deviations, battle reports, per-member
  combat metrics, monthly pilot kill stats.
- **Background jobs:** Killmail discovery (ESI + zKillboard), stat rebuilds, cache
  warmers, name resolution, kill feed, battle auto-clustering, rank/milestone scans.
- **Integrations:** ESI corporation/character killmails (corp feed needs an in-game
  Director token), zKillboard.
- **Configurable:** Kill-feed thresholds, newbro framing, the combat-rank ladder, the
  reward engine (ships dark-launched off).

### Combat ranks and rewards

- **Users:** members (progression); leadership (configuration).
- **Purpose:** A configurable 17-rung combat rank ladder based on threshold metrics, with
  one-time rank-up celebrations and an optional reward engine. The reward engine ships
  **off** and never auto-pays — it generates ledger events an officer approves and marks
  paid (separation of duties), and baselines existing pilots so rewards are never
  retroactive.
- **Roles:** Member (see rank); officer/director (ladder CRUD, reward review).
- **Background jobs:** Reward scans, rank-up notifications, newbro milestone scans.

### Hall of Fame

- **Users:** members.
- **Purpose:** A monthly corp recognition leaderboard built from the contribution ledger,
  overall and per category. Completed months are frozen so past boards do not shift when
  leadership retunes weights.
- **Background jobs:** Hall-of-Fame cache warmer, monthly freeze safety net.

### Knowledge base

- **Users:** everyone (public pages are a recruiting surface); members/officers for
  restricted pages.
- **Purpose:** Versioned corp documentation. Markdown pages carry a visibility tier
  (public/member/officer) and a revision history, and support a small allowlist of live
  embeds that render the reader's own readiness/SRP so guides never go stale.
- **Roles:** View is visibility-filtered; officers create/edit/delete pages.

### New-player onboarding

- **Users:** newbros and prospective recruits.
- **Purpose:** A checklist of milestones (account/skills/doctrine/activity) that are
  auto-detected from synced ESI data or checked off manually, plus a searchable glossary
  and a "what to do today" surface.
- **Roles:** Dashboard is viewable by anyone (personalised when logged in); pilots toggle
  their own manual milestones. Leadership edits milestones and glossary terms in the
  console.

### Mentorship Program

- **Users:** cadets, veteran mentors, and leadership.
- **Purpose:** A structured cadet↔veteran mentorship programme: eligible veterans
  volunteer, new pilots register, and leadership (or auto-suggestion) pairs them. Pairs
  work through seeded learning tracks and field exercises that are honestly validated
  against already-synced data (no live ESI in the request path), earning cosmetic badges
  and an officer-approved reward ledger that never moves ISK itself.
- **Roles:** Member self-service (register, work a pairing); officer/director configure
  the programme, approve pairings, and pay rewards in the console.
- **Background jobs:** Eligibility refresh, auto-suggest pairings, validation sweeps,
  anomaly scans, stale-pairing expiry, active-days rewards, optional session-presence
  polling (opt-in scope).
- **Integrations:** Public ESI; optional `mentorship_presence` scope for real-time,
  never-stored session check-in.

### Raffle contests

- **Users:** members (default); audience-configurable to alliance/public.
- **Purpose:** Engagement raffles where pilots earn tickets from real in-game activity
  (PvP, mining, fleets, manual recognition), and leadership runs fair, reproducible
  **commit-reveal** draws with a full audit trail. Doubles as an "adopt the app" driver
  via ESI-adoption metrics.
- **Roles:** View within audience; members see their own performance and can opt out of
  nudges; officers run everything in the console.
- **Background jobs:** Lifecycle, source sweeps, summary recompute, automatic draws,
  integrity scans, adoption cache warmers.

---

## Ships and doctrines

### Doctrines and Shipyard

- **Users:** members (readiness/prep); audience-controlled browse; officers for the
  library.
- **Purpose:** The corp doctrine library and fleet-readiness engine. Leadership publishes
  standard fits grouped into named doctrines; every pilot sees what they can fly today,
  what they are closest to, and the exact skills and modules still needed. It drives corp
  supply planning and a browse-and-order Shipyard.
- **Workflows:** Browse the library and Shipyard; view a doctrine and per-fit skill
  verdict; see personal readiness ranked by closeness; run a pre-fleet checklist; view or
  action a corp supply plan.
- **Roles:** Browse/detail/export follow the "Ships & doctrines" audience (default corp);
  personal tools are member-only; coverage and supply-tasking are officer; officer
  inspection of another member is audited.
- **Data:** Categories, doctrines, fits, requirements, dogma-derived skill requirements,
  import staging batches.
- **Background jobs:** Doctrine import-staging housekeeping.
- **Integrations:** ESI character saved fittings (`fittings` scope) — there is no
  corp-fittings endpoint, so doctrines are seeded from a director's own saved fits; fits
  can also be imported from a killmail or EVE-client XML.

### Skill plans

- **Users:** members; officers for corp gap analysis.
- **Purpose:** Ordered, ETA-estimated training plans toward doctrine goals, a corp-wide
  skill-gap view for leadership, and an opt-in nudge when a training queue runs dry.
- **Roles:** Personal plans are member/owner; the gap view is officer.
- **Background jobs:** Idle-queue nudge (opt-in, gated by the notification event).

---

## Fleet and combat

### Fleet operations

- **Users:** members (sign-up); officers/FCs (management).
- **Purpose:** An operations and fleet planner. Leadership declares an objective
  (deployment, home defence, structure timer, doctrine rollout, or a combat/mining fleet)
  with a target date, doctrines, and a ship composition; the system scores readiness,
  turns gaps into prep tasks, and runs sign-ups, RSVP, and participation (PAP). It also
  maintains a structure-timer board and a sovereignty (ADM) board.
- **Workflows:** Browse and open ops; RSVP; commit to a ship slot (race-safe claims);
  self-mark attendance; (officer) create/edit ops, generate prep tasks, override or
  cancel, announce, pull the live fleet for attendance, manage timers and recurring
  templates.
- **Roles:** Members view/sign up; officers and holders of the `fc` capability manage.
- **Background jobs:** Sovereignty sync, form-up reminders, auto-cancel of under-signed
  ops, recurring-op materialisation.
- **Integrations:** ESI fleet roster (`fleet_tracking` scope) for automatic PAP; public
  ESI sovereignty.

### Intel tools

- **Users:** members (view); officers (management).
- **Purpose:** Watchlists of entities (character/corp/alliance) with a tripwire when a
  watched entity appears on a fresh killmail, plus roaming-target and gate-camp-risk
  analysis built from public data.
- **Roles:** Members view; officers manage watchlists and battle reports.
- **Background jobs:** Watchlist activity scan (inert until armed).

### Standings board

- **Users:** members.
- **Purpose:** A blue/red standings board driven by corp contacts.
- **Roles:** Members view; officers sync.
- **Integrations:** ESI corp contacts (`corp_contacts` scope).

### Structures

- **Users:** officers.
- **Purpose:** A board of corp Upwell structures — fuel remaining, online/low-power state,
  and reinforcement timers — with a deduped officer alert on a breach-set change, and an
  urgency-ranked infrastructure view combining structure fuel, sovereignty ADM, and
  timers.
- **Roles:** Officer.
- **Background jobs:** Structure sync, infrastructure alert scan.
- **Integrations:** ESI corp/universe structures (`corp_structures` scope).

---

## Navigation

### Navigation and maps

- **Users:** everyone by default (audience-controlled, default public).
- **Purpose:** A self-hosted navigation suite built on public SDE and public ESI: a gate
  route planner, a hull-aware cyno jump planner (with high-sec-exit handling and fuel
  maths), a jump-range reach finder, region/universe maps, per-system dossiers, and PvP
  intel. It also holds the corp's player-owned jump network (Ansiblex bridges and cyno
  beacons) that the planners route through.
- **Workflows:** Plan a gate route or a jump route; find jump range; browse maps and
  system dossiers; (member) save routes and set route watches; (officer) register or
  ESI-sync the jump network.
- **Roles:** Public tools follow the navigation audience; roaming/gate-camp intel maps to
  the `intel` feature and is member-only; beacon management is officer.
- **Background jobs:** Map-overlay warmers, jump-network sync, opt-in route-watch scan.
- **Integrations:** Public ESI routes/overlays; ESI corp structures (`jump_network`
  scope) for the network; reuses synced skills for the jump planner (no new scope).
- **Configurable:** Jump-planner defaults, exit strategy, corp avoidance lists, and
  pilot-override toggles.

---

## Industry and economy

### Industry and production (Industry Center)

- **Users:** members.
- **Purpose:** The unified Industry Center: a production-planning suite that turns corp and
  doctrine demand into costed, trackable build plans. It provides a manufacturing
  calculator, a T2 invention planner, a production-chain explorer, a blueprint browser, and
  a job tracker driven by the SDE plus live market prices, with saveable multi-item
  projects, recursive bills of materials, corp-stock reservation, shopping lists, and
  profit estimates. Estimates are always explicit about their assumptions.
- **Workflows:** Use the calculator/invention/chain tools; browse blueprints and jobs;
  turn doctrine-supply shortfalls or imported ESI jobs into plans; build and manage a
  project (add items, recompute BOM, generate a shopping list, reserve stock, push to
  build jobs).
- **Roles:** Member; per-plan visibility (private/leadership/corp) and manage rights
  (creator/assignee/officer).
- **Configurable:** Market/tax/fee/facility defaults and governance toggles in the
  Industry settings console page.

### Industrial ERP (build jobs)

- **Users:** members (claim/build/deliver); officers (create).
- **Purpose:** A demand-scoped industrial ERP that turns "we need N of X" into claimable
  build jobs with a BOM and material readiness, tracks blueprint coverage against active
  doctrines, and on delivery adds product to corp stock and credits the builder. By default
  `/erp/` is subsumed into the Industry Center's job tracker.
- **Background jobs:** Corp blueprint sync, corp industry-job sync, per-pilot industry
  sync (opt-in).
- **Integrations:** ESI corp blueprints/jobs (`corp_industry`, Director) and personal
  jobs/blueprints (`my_industry`, opt-in).

### Market

- **Users:** members (dashboard); officers (locations).
- **Purpose:** Market intelligence — tracked market locations, live Jita/CCP prices, order
  snapshots, and regional history — surfacing seeding shortfalls, trade margins, and
  build-vs-buy opportunities. It is also the platform's **canonical pricing backbone**: its
  prices feed store quotes, SRP payouts, industry BOM costs, and killmail valuation.
- **Background jobs:** Adjusted-price sync, live Jita price sync (re-values the killboard
  and BOMs), history sync with a catch-up guard, dashboard warmers.
- **Integrations:** Public ESI markets; Fuzzwork prices; optional EveRef history backfill.
- **Note:** ISK value resolves through a shared pricing function (live Jita sell → CCP
  adjusted → 0); the SDE base price is deliberately never used for valuation.

### Stockpile and assets

- **Users:** members; officers for corp views.
- **Purpose:** Leadership defines target stockpiles at locations; the app tracks manual
  stocktakes, FIFO project reservations, and a member-run hauling/courier board to move
  goods where short. It also mirrors live ESI assets (corp via a Director token, personal
  via each pilot's token) grouped by location with ISK value.
- **Roles:** Members manage personal assets and hauls; officers create stockpiles, sync
  corp assets, and search corp-wide.
- **Background jobs:** Corp asset sync, personal asset sync.
- **Integrations:** ESI corp/personal assets (`corp_assets` Director, `personal_assets`
  opt-in).

### Mining

- **Users:** members (self view); officers (ledger and payouts).
- **Purpose:** Pulls the corp mining ledger from ESI refinery observer records — who mined
  what ore per day — values it at Jita, applies a corp-set mining tax, and lets leadership
  split operation proceeds among participants. Members get a self-scoped "My mining" view.
- **Roles:** "My mining" is self-scoped; ledger, tax, and payouts are officer. Finalised
  payouts are frozen; payout lines are IDOR/race-safe.
- **Background jobs:** Ledger sync, mining-milestone scan.
- **Integrations:** ESI corp mining (`moon_mining` scope).

### Planetary Industry

- **Users:** members.
- **Purpose:** A guided Planetary Industry guide and planner: it teaches PI, walks a pilot
  through a plan wizard, and produces a transparent, price-driven profit estimate across
  planets (chains, taxes, hauling, customs). It is advisory only — it moves no ISK and
  writes nothing to the game — and can optionally import a pilot's live colonies to
  health-monitor a plan.
- **Roles:** Member; per-plan visibility.
- **Background jobs:** Colony sync (opt-in), active-plan re-costing.
- **Integrations:** ESI planets (`planetary_industry`, opt-in).

### Corp finance

- **Users:** directors.
- **Purpose:** Corp wallet balances, income/expense analytics, a forecast, and the journal.
- **Roles:** Director.
- **Background jobs:** Wallet sync, finance dashboard warmer.
- **Integrations:** ESI corp wallet (`corp_finance` scope).

### Corp contracts

- **Users:** officers.
- **Purpose:** An officer browser of all corp contracts (item exchange, courier, auction)
  for oversight.
- **Integrations:** ESI corp contracts (`corp_contracts` scope).

---

## Member services

These three services have their own **audience** configuration (disabled/corp/alliance/
public), set on the Services & features console page. They are external-facing services a
corporation offers to its members (and optionally allies or the public).

### Freight service

- **Users:** members and, per audience, allies or the public.
- **Purpose:** A PushX / Red Frog-style courier service. Customers price a haul on a rate
  calculator, post it as an outstanding contract, and corp haulers claim, fly, and deliver
  it for ISK. Quotes use the standard per-warp courier model against a single officer-tuned
  rate card, and self-reported deliveries are cross-checked against the real in-game
  contract before earning full credit.
- **Roles:** Calculator/board follow the audience; posting/claiming/transitions are
  member; the rate card and corp-contract oversight are officer.
- **Background jobs:** Contract reconcile, corp-contract snapshot, overdue-haul sweep.
- **Integrations:** Public ESI routes; ESI corp/character contracts for verification;
  structure search for precise pickup/drop-off.

### Buyback service

- **Users:** members and, per audience, allies or the public.
- **Purpose:** A paste-to-appraise buyback tool plus a member-to-member offer board. A
  pilot pastes items, gets an appraisal priced off Jita sell with a location haircut
  (highsec/lowsec/nullsec), and can post the lot for a corpmate to buy and settle in-game.
  An optional corp-funded **guaranteed** buyback path ships inert and is read-only
  reconciled against the corp wallet — the app never moves ISK.
- **Roles:** Appraisal follows the audience; the offer board is member; rates and the
  guaranteed queue are officer, with separation of duties, per-lot caps, and a rolling
  daily budget.
- **Background jobs:** Guaranteed-buyout wallet reconcile (inert until armed).

### Corp Store

- **Users:** members and, per audience, allies or the public.
- **Purpose:** A built-to-suit ship ordering service. Pilots order ready-to-fly doctrine
  fits (priced off live Jita sell × markup) or made-to-order hulls up to capitals (built on
  demand, secured by an upfront deposit). Sub-capital hulls price off live Jita sell ×
  markup; capital and supercapital hulls price off their estimated build cost (EVE Ref job
  cost, local SDE material estimate as fallback) × a per-class profit multiplier, each
  configurable in the store settings. Orders land on a corp-only fulfilment board where
  members claim, build, and deliver them by in-game contract.
- **Roles:** Storefront follows the audience; the fulfilment board is member (a buyer
  cannot fulfil their own order); markups/deposit are officer.
- **Integrations:** Reads synced pricing, doctrines, killboard loss demand, industry cost,
  and freight routing (for a supply forecast).

---

## Pilot tools

### Command Center dashboard

- **Users:** members.
- **Purpose:** The pilot's single home page, consolidating combat rank, readiness,
  services, a unified quest queue, onboarding milestones, and an officer deck into one
  view. Pilots can choose which panels to show.
- **Background jobs:** Per-pilot briefing warmers.

### Contribution ledger

- **Users:** members; leadership for totals.
- **Purpose:** A "My Contribution" activity ledger recording what a pilot did that helped
  the corp, in native units (no combined ISK score), feeding the Hall of Fame and a
  recognition feed. Pilots opt in/out of corp-wide recognition.
- **Configurable:** Per-kind contribution weights (leadership-tunable, with past
  Hall-of-Fame months frozen on change).

### Ship replacement (SRP)

- **Users:** members (claims); officers (queue).
- **Purpose:** A ship-replacement programme. It is planning and record-keeping only —
  nothing here moves ISK. Leadership tunes one program (how losses are valued and
  compensated); an eligible loss lets a pilot file a claim; an SRP manager approves/denies
  (optionally adjusting the payout) and records payment. Separation of duties prevents a
  claimant self-approving.
- **Background jobs:** SLA/solvency scan, optional auto-draft of eligible claims (never
  auto-pays).

### Tasks

- **Users:** members (board); officers (create).
- **Purpose:** The corp execution backbone: every corp gap becomes an owner-sized task —
  assigned or open-to-claim — and completing one credits the doer via the contribution
  ledger. Tasks link back to the doctrine, op, shopping list, or build job they serve.

### Daily briefing

- **Users:** members (digest); officers (command deck).
- **Purpose:** The Command Center's time-sensitive digest plus the officer command deck.
- **Background jobs:** Daily leadership-briefing delivery to Discord and email.

### Pilot Intelligence

- **Users:** members.
- **Purpose:** Corp orders in the Command Center quest log — each member's ranked "next
  best actions" toward the corp's binding constraints, produced by Command Intelligence.

---

## Command and readiness

### Readiness platform

- **Users:** officers (dashboard); members (personal panel).
- **Purpose:** A configurable readiness intelligence platform. It periodically computes a
  composite readiness index across weighted dimensions (scores only, never raw personal
  data), records durable findings (gaps/risks/forecasts), turns them into prep tasks, fires
  configurable alerts, produces a weekly executive report, and gives each pilot a personal
  readiness and quest view. It includes a risk register, a timeline, a what-if fleet
  simulator, and a forecast.
- **Roles:** Dimensions, findings, tasks, alerts, report, simulator, and timeline are
  officer; the personal panel and quest actions are member.
- **Background jobs:** Index warm, history snapshot, per-pilot warm, alert evaluation,
  task generation, weekly report, retention housekeeping.
- **Configurable:** ~15 dimensions each with enable/weight/thresholds, scoring method,
  responsibilities, alert rules, finance/SRP/recruitment inputs, mandatory ships, strategic
  role targets, staging system, and the readiness EVE-mail sender.

### Command Intelligence

- **Users:** officers and directors (by classification).
- **Purpose:** An LLM-backed strategic command subsystem. It assembles an immutable
  intelligence snapshot from every source, computes operational constraints (capability
  ceilings), and generates classification-tiered staff briefings with prioritised Courses
  of Action. It also runs campaigns, a readiness what-if simulator, conversational Q&A over
  the archive, per-pilot directives, and battle after-action reviews. All LLM calls run in
  background workers only, and the subsystem is disabled cleanly when no API key is set.
- **Roles:** Officer surface; report and Course-of-Action visibility are further gated by
  classification clearance (corp-internal → member floor, up to director-eyes-only).
- **Background jobs:** Report generation, snapshot builds, outcome calibration, weekly
  scheduled report, auto after-action reviews (off by default), a guard-railed autonomous
  proposer (kill-switched off by default), housekeeping.
- **Integrations:** An external LLM provider (MiniMax reference adapter) over an
  OpenAI-compatible endpoint, host-allowlisted, workers only.
- **Configurable:** Model, token budgets, rate limits, thresholds, classification floors,
  notification arming, and the autonomous kill switch — all in the console.

### Recommendations and alerts

- **Users:** officers (board); members (personal recommendations).
- **Purpose:** Explainable, ranked officer suggestions (doctrine readiness, skill training,
  stock shortage, build-vs-buy, hauling, newbro next-step, combat-loss patterns, officer
  actions) with an action queue, plus a relay of interesting in-game notifications and corp
  mailing-list mail to the site and Discord.
- **Background jobs:** Engine run + alert dispatch, notification relay, mail relay,
  retention housekeeping.
- **Integrations:** ESI notifications (`notifications` scope) and mail (`mail_relay`
  scope).

### Pingboard

- **Users:** members (calendar, DM linking); officers (compose/history/automation).
- **Purpose:** The unified alerting and calendar system. One data model handles all pings
  (in-app / EVE-mail / Discord / Slack / Telegram / WhatsApp) with per-channel encrypted
  secrets, priority/classification gating, a deliver-once ledger, automation rules, and an
  events calendar that syncs from source services and materialises reminder alerts. Pilots
  link and verify their own DM handles and set per-category mutes (EMERGENCY cannot be
  muted).
- **Roles:** Compose/history/automation are officer (dispatch floors for urgent/emergency
  are configurable, default director); DM linking and preferences are member.
- **Background jobs:** Due dispatch, retry, calendar sync, reminder materialisation,
  automation evaluation, housekeeping.
- **Integrations:** Discord, Slack, Telegram (bot + inbound webhook), WhatsApp (Meta or
  Twilio), and EVE-mail via a sender character.

---

## Leadership

### Recruitment

- **Users:** recruiters (the `recruiter` capability) and officers.
- **Purpose:** A recruiter's evidence desk. It works on public data by default (corp
  history, character age, killboard), presenting evidence with confidence levels — never an
  automated accept/reject. An optional, time-boxed candidate ESI consent (via a separate
  EVE application) can derive richer claims; the raw token is used once and discarded.
- **Roles:** The desk requires the `recruitment.manage` capability (the lateral `recruiter`
  role or officer+); candidate OAuth endpoints are public.
- **Background jobs:** Candidate evidence refresh.
- **Integrations:** A second, optional recruitment SSO application.

### Corporation data and access governance

- **Users:** officers and directors.
- **Purpose:** Mirrors corp-level ESI data (roster/member tracking, wallet, contacts,
  structures, moon extractions) into local tables for a "state of the corp" surface —
  roster and registration compliance, finance, standings, and infrastructure — and manages
  the partner-alliance / friendly-corporation records that widen alliance-service access.
- **Roles:** Roster/structures/standings are officer; finance is director; access
  governance is director.

---

## Platform and account

### EVE SSO and account

- **Users:** everyone who logs in.
- **Purpose:** EVE Single Sign-On login (OAuth2 + PKCE), character linking with
  ownership-change protection, an ESI Scopes page where pilots and directors grant feature
  scopes, self-service scope reconciliation, and character disconnect. Application roles
  are synchronised from corp membership and the in-game Director role.
- **Background jobs:** Affiliation refresh, Director-role reconcile, token pruning, scope
  reconcile, ingestion-token liveness alerts, post-login warming.

### Admin console and audit

- **Users:** officers and directors.
- **Purpose:** The native role-gated console at `/ops/` that replaces the stock Django
  admin for day-to-day configuration: services & features, members and roles (with
  dual-control Director grants), access governance and character recovery, doctrines and
  content, per-subsystem settings, localisation policy, data-retention policy, a
  maintenance-task launcher, an investigable audit log, and an integration-health page.
- **Roles:** Hub is officer; most configuration is director. Every sensitive action is
  audit-logged.
- **Background jobs:** Retention enforcement, member-leave enforcement (report-only until
  armed), weekly dependency audit, integration-health scan.

### Comms access sync

- **Users:** directors (configuration); members (account linking).
- **Purpose:** Keeps a pilot's external comms access (Discord first) in lockstep with corp
  membership and RBAC, auto-revoking on corp-leave. Credentials are console-managed and
  stored encrypted. It ships inert until leadership arms a platform, and enforces
  managed-set, additive-default, dry-run, and pin safety rails.
- **Background jobs:** Periodic full reconcile (catches grant expiries) and targeted
  fast-revoke.

### EVE reference data (SDE)

- **Users:** operators (import).
- **Purpose:** A relational subset of CCP's Static Data Export that every feature reads to
  turn EVE ids into names, ship hierarchies, skill and build requirements, and map
  geometry. Loaded by operators via management commands; no user-facing routes.

### Languages and localisation

- **Users:** every pilot; directors for the policy.
- **Purpose:** The interface is localised into nine languages: English (canonical, and
  always enabled), Portuguese (Brazil), Spanish, French, Russian, German, Simplified
  Chinese, Korean, and Japanese. A language selector appears in the sidebar user block
  once more than one locale is enabled, and an authenticated pilot's choice is saved to
  their account (`identity.User.language`) as well as to the `forca_language` cookie. The
  active language resolves in order: account preference, then the cookie, then the
  browser's `Accept-Language` (only while leadership leaves browser detection on), then
  the configured default — anonymous visitors skip the account step. Notifications are
  rendered in each recipient's own language; a group broadcast with no single recipient
  uses the configured broadcast locale. The translations are machine drafts with an LLM
  native-review pass, not professional human translation.
- **Roles:** Pilots set their own language. The localisation policy page
  (`/ops/admin/i18n/`) — which locales the selector offers, the default, browser
  detection, and the broadcast locale — is Director-only and audit-logged. A fresh
  install enables English only.
