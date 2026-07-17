# Changelog

All notable changes to [FORCA] Command Grid are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **MRP v1 — the corp-wide Material Plan (supply-chain P3)** — one planning run now
  answers "what does the corp actually need to build, buy or import?" It merges every
  demand signal (P2's composed per-fit demand plus live Shipyard backorder needs),
  explodes it through the build tree, and nets **each material exactly once at its
  deepest BOM level** against truthful availability, reservations and in-flight work —
  two doctrine restocks sharing components produce one netted requirement whose
  provenance lists both. Incoming supply counts exactly once, with the right rules:
  corp ESI jobs (active/paused, finished-but-undelivered behind a knob), board build
  jobs and plan lines — never character jobs, never invention, and never the same
  physical build twice (a claimed board job can be linked to the in-game job it became
  via a new "started in game" action; the ESI side then carries the schedule). A
  promised-but-unstarted internal build also demands its own materials, so the plan
  never eats its own shopping list. The officer **Material Plan** page
  (`/industry/mrp/`) shows every requirement with a full drill-down (which fits, needs,
  parents and jobs produced each number), required-by and earliest-feasible dates
  (honestly labelled — no capacity model yet), one-click idempotent fan-out to an
  industry plan, build job, hauling job or claimable BUY task (with an inline multibuy
  block), a re-run that provably changes nothing when nothing changed (input digest),
  and CSV export. Re-runs refresh unclaimed vehicles in place and flag claimed ones
  that drifted instead of minting duplicates. The nightly planning beat ships
  **disarmed**; manual runs are the v1 workflow. Fully translated in the nine locales.

- **Demand planning that merges signals (supply-chain P2)** — doctrine-ship demand is no
  longer one number derived from raw 30-day hull losses. A composed, per-fit demand
  service (`apps.store.demand`) merges four independently-visible sources: loss
  replacement from the killboard's ingest-time doctrine-fit tags (untagged hull losses
  allocated proportionally, as their own labelled line; NPC/awox losses count — the ship
  is destroyed either way), upcoming **non-recurring** fleet ops with fit-linked slots
  (a recurring CTA's attrition is already inside the loss history — an
  `include_recurring_ops` knob exists for corps without that history), the stock-target
  build-up gap, and officer-entered **manual demand lines** with dates and campaign
  links (full CRUD on the fit page, audited). Rates carry a real volatility band
  (mean ± σ at a leadership-set service level) — and no band at all below five weeks of
  observed history, never a fabricated ±0. The officer console's days-of-cover is now a
  **runout projection** (rate + dated events vs ATP) at fit grain — shared hulls no
  longer double-count — with a demand column, source tooltips, trend arrows and
  slow-mover / obsolete / upcoming / no-history chips; the CSV gains the demand,
  cover-band and suggestion columns. **(s, S) reorder suggestions** honour lead time,
  service level and safety stock, always keep the order-up-to strictly above the
  trigger (no churn), and are offset by incoming supply (which still never counts as
  available). A weekly snapshot task starts recording demand history for trends and
  later phases. Recurring op templates can finally carry doctrine-fit links, so
  materialised weekly fleets feed planning. All of it translated in the nine locales.

- **One truthful per-type availability (supply-chain P1)** — the five competing
  definitions of "available stock" (manual-only, two manual+ESI double counts, a dead
  ESI-only module, and the BOM's private view) are replaced by a single authority in
  `apps.stockpile.availability`: effective on-hand (ESI-wins per covered location,
  counted once per asset location, home corp only) minus ACTIVE reservations, floored
  at zero. Doctrine supply plans, command intel, the ERP job board and the industry BOM
  engine all read the same number now. **Numbers visibly move**: planners may show
  less (the double count and reserved stock are gone) or more (ESI-covered hangars now
  count) — queued ERP jobs can flip BLOCKED at the first re-check after deploy; that is
  the truthful state arriving. The stockpile dashboard shows per-row reserved,
  available and over-reserved claims.
- **Reservations finally have a lifecycle** — delivering a plan-linked build consumes
  the plan's reservations first (status-guarded, exactly once, splitting a partially
  needed claim); the free-stock remainder can no longer eat stock another plan
  reserved; closing, cancelling or archiving a plan releases whatever it still holds
  (audit-logged). A data migration releases the reservations historically stranded on
  closed plans; `manage.py audit_stock_integrity` reports what will change before
  migrating. Non-negative stock and ≥1-unit reservations are now database constraints,
  and stockpile row locks follow one global order (ascending pk) proven by threaded
  deadlock tests.
- **BOM correctness** — two plan lines sharing a material now split the available pool
  instead of each netting the full stock; *Reserve stock* sums demand across lines
  (was: max), is idempotent under concurrent double-clicks, is capped at the truthful
  availability (a stale manual count can't mint claims beyond real stock), and is
  refused on a closed or archived plan.

- **Shipyard availability, reservations and backorders** — the Shipyard no longer
  presents every doctrine ship as immediately available. Complete fitted ships are now
  tracked per fit and delivery location in a ledger-backed inventory (every movement is
  an immutable, attributed entry), and each card shows an honest, accessible state:
  ready for delivery (with the count), limited stock, available on backorder with an
  estimated delivery date, temporarily unavailable, or not offered. Ordering reserves
  real stock under database row locks — two pilots can never oversell the last ship —
  and anything beyond stock becomes a backorder the buyer explicitly acknowledges on a
  confirm page that disclosed the split and the estimate first. Order-time availability,
  split quantities, delivery location, fit revision and the promised date are frozen on
  the order for audit; cancellations release the hold, delivery consumes it exactly
  once, and a leadership-configurable sweep can expire holds nobody claims. Backorders
  consolidate into one supply need per fit and location, which officers turn into an
  Industry Project (finally wiring the store→industry link), an ERP build job, or a
  claimable task — production completion pings the officers to assemble and receipt the
  ships, which auto-allocates them to waiting orders oldest-first and can notify a
  waitlist. New officer surfaces: a **Shipyard inventory console**
  (`/store/inventory/`) with search, filters, bulk offer control, CSV export, audited
  stocktakes with mandatory reasons, an advisory ESI hull cross-check, and per-fit
  policy overrides with visible inheritance; and a **Shipyard fulfilment policy** page
  (`/store/inventory/policy/`) for backorders, lead times, reservation expiry, order
  caps and shop-front visibility. Stock recorded against an older fit revision stops
  counting until an officer revalidates it. New Shipyard filters (availability,
  delivery location) and sorts (availability, fastest delivery, price). Fully
  translated into all nine languages.
- **Per-class Corp Store pricing for capital hulls** — capital and supercapital
  made-to-order hulls are no longer priced off Jita sell × markup (a basis that barely
  exists for hulls that never trade in Jita). They are now priced off their **estimated
  build cost** (EVE Ref's full job cost, falling back to the local SDE material estimate)
  times a per-class profit multiplier leaders configure in **Store settings**
  (`/store/settings/`): separate markups for sub-capital, capital and supercapital hulls.
  Each order freezes its price basis and the build-cost estimate it was quoted from; when
  no build-cost source can answer, a capital order is refused rather than silently quoted
  off a misleading market reference. Sub-capital hulls and doctrine fits keep the classic
  live-Jita-sell markup. Fully translated into all nine languages.
- **Linked Pilots and pilot switching** — a pilot can link several EVE characters to one
  account and switch between them without logging out. New **Pilot → Linked Pilots** page
  (`/pilot/linked-pilots/`), a persistent pilot selector in the sidebar and the mobile drawer,
  and a selector on the Command Center. Every pilot is authorised individually through EVE SSO
  (CCP exposes no way to discover which characters share an EVE account, so nothing is
  inferred). Fully translated into all nine languages.
- **Pilot ESI health** — each linked pilot shows its own authorisation status, missing scopes
  and last synchronisation, with a per-pilot **Reauthorise** action. A dead token on one pilot
  never blocks switching to another.
- **Unlinking** — releases a pilot and destroys its ESI authorisation, keeping its historical
  records. The last remaining pilot cannot be unlinked (an EVE pilot is how you sign in).

### Removed

- The dead `industry.Blueprint` table (readiness build-capacity now reads the
  ESI-synced ERP blueprint library), the unused `apps/industry/availability.py`
  module, and the never-rendered `Doctrine.is_public_preview` /
  `DoctrineFit.estimated_cost` fields (with their admin columns).

### Changed

- The Shipyard console's universe now includes fits of retired doctrines that still
  hold stock (flagged obsolete) — that stock used to vanish from the console silently.
  Days-of-cover values move: mostly up for fits that shared a hull (double count
  removed), down where dated ops or manual demand pulls the runout in. The member
  supply-forecast page shows composed demand with the band and breakdown, and stops
  flooring forecasts at 1/week — slow-tail rows now show honest sub-unit rates and
  smaller monthly totals. A new `suggested` reorder alert exists but ships **disarmed**
  (`DemandConfig.use_suggested_reorder_alerts`).
- **Corporation authority now follows the active pilot, not the account.** Previously a role
  was granted to the *account* if **any** of its characters qualified — so linking one Director
  alt made every pilot on that account a Director, including pilots in unrelated corporations.
  Authority is now the lesser of what the human was granted and what the pilot they are flying
  can substantiate: an out-of-corp pilot carries no corp standing at all, and Director authority
  requires the pilot to actually hold the in-game Director role. Officer still follows the
  person across their corp pilots; `admin` and superuser are unaffected. **Directors keep their
  access across the upgrade** — a data migration attaches the in-game Director seat to each
  current director's corp pilots, and the six-hourly ESI reconcile then narrows it to the pilots
  that genuinely hold the role.
- **Pilot quest logs are now per-pilot.** `PilotDirective` and `PilotRecommendation` were keyed
  on the account while being computed from one character, so regenerating one pilot's quest log
  overwrote another's. Both are now scoped to the pilot they describe; existing rows are
  attached to each account's main.
- The Command Center, "my assets", skills, readiness, operations, doctrines, killboard, buyback,
  store, freight, mentorship and navigation surfaces all resolve *the pilot you are flying*
  rather than the account's main.

### Fixed

- Build-vs-buy maths no longer treats units as blueprint runs for batch recipes
  (ammo, drones, reaction feedstock) — building 100 units of a 100-per-run item is
  now costed at one run, not one hundred. This corrects doctrine supply lines,
  recommendations and the store's capital-pricing fallback, which may flip
  build↔buy for batch items: the corrected maths is the truth arriving. A
  legitimate zero-second build time is no longer treated as unknown, and reaction
  durations are now readable for scheduling. Creating a supply plan from the corp
  demand page twice no longer mints twin projects.
- The pilot digest cache (`briefing:pilot:*`) was keyed by account while holding one pilot's
  prose, and was not language-keyed — so it served the Celery warmer's English to every
  non-English reader for the life of the entry.
- The htmx history cache kept rendered, pilot-specific pages in `localStorage`, where no
  `Cache-Control` header governs them; a Back press could repaint a previous pilot's page.

- **Localisation** — the interface is available in nine languages: English, Portuguese
  (Brazil), Spanish, French, Russian, German, Simplified Chinese, Korean, and Japanese.
  English is the canonical source language and cannot be turned off. The translations are
  machine drafts with an LLM native-review pass, not professional human translation.
- **Language selector and account preference** — a selector at the foot of the sidebar lets
  a pilot pick their interface language. The choice is written to the `forca_language`
  cookie, so an anonymous visitor's pick survives; a signed-in pilot's is also stored on
  their account (`identity.User.language`). For a signed-in pilot the active language is
  resolved from the account preference, then the cookie, then `Accept-Language`, then the
  configured default.
- **Localisation policy console** — a Director-only page at `/ops/admin/i18n/` controls
  which locales the selector offers, the default locale, whether the browser's
  `Accept-Language` header is honoured, and whether anonymous visitors may choose a
  language. It ships with English only enabled, so nothing user-visible changes until a
  Director turns a locale on. Browser detection is on by default, so enabling a locale
  immediately serves it to every pilot whose browser asks for it.
- **Per-reader notification text** — notifications and other database-backed prose are no
  longer stored as translated text. They are persisted as a message key plus its parameters
  and rendered in the reader's own language at display time; a group broadcast with no
  single recipient uses the configured broadcast locale.
- **`I18N_ENABLED`** — an environment kill switch, default on. Turning it off
  short-circuits locale resolution to English and hides the selector.

## [1.0.0]

The first public release of [FORCA] Command Grid — a free, self-hostable operations hub for
an EVE Online corporation.

### Included

- **Authentication & identity** — EVE Single Sign-On (OAuth2 authorization-code + PKCE),
  character linking with ownership-change protection, encrypted token storage, and a
  role-based access control system with lateral capabilities and dual-control Director
  grants.
- **Community & intel** — killboard with valuation and rankings, combat ranks, Hall of
  Fame, knowledge base, new-player onboarding, mentorship programme, and raffle contests.
- **Ships & doctrines** — doctrine library, per-pilot readiness engine, Shipyard, and skill
  plans.
- **Fleet & combat** — operations planner (RSVP, sign-ups, attendance), intel/watchlists,
  standings board, and structure monitoring.
- **Navigation** — route, jump, and range planners with region maps.
- **Industry & economy** — the Industry Center (BOM, invention, chains, jobs), ERP build
  jobs, market intelligence, stockpile and asset mirrors, mining ledger and payouts,
  planetary industry planner, corp finance, and corp contracts.
- **Member services** — freight, buyback, and corp store, each with configurable audiences.
- **Pilot tools** — the Command Center dashboard, contribution ledger, SRP, tasks, and a
  daily briefing.
- **Command & readiness** — the readiness platform, LLM-backed Command Intelligence,
  explainable recommendations, and the Pingboard alerting and calendar system.
- **Operations** — a containerised Docker Compose stack (nginx, gunicorn, Celery worker and
  beat, PostgreSQL, Redis), an idempotent provisioning script for Ubuntu, and a `Makefile`
  plus `scripts/` operator command surface.
- **Documentation** — a complete handbook set for end users, administrators, contributors,
  and operators, plus reference material.

[1.0.0]: https://github.com/ebrandi/forca-command-grid/releases/tag/v1.0.0
