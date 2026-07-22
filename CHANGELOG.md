# Changelog

All notable changes to [FORCA] Command Grid are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Combat Signatures (pilot banner images)** — home-corp pilots build personalised PNG
  banner images from their own killboard and profile data and embed them in forums, Discord,
  and websites. A private builder on `/killboard/signatures/` composes a banner from a size
  preset (compact/standard/wide/card), a layout (identity/tactical/minimal), an ordered set of
  up to twelve components (portrait, corp/alliance, kills/losses/ISK/efficiency/K-D, rank and
  progress, featured trophies, last/best kill, favourite ship, and more), an activity period,
  a language, a theme, and one of twenty-five original procedural backgrounds — with a live
  preview and copy-paste Direct URL / BBCode / Markdown / HTML embed snippets. The finished
  image is served publicly at an unguessable, stable URL (`/s/<token>.png`), pre-rendered
  off-request by a coalesced Celery beat to a persistent media volume and served straight off
  disk by nginx (the Django view is only a pending/placeholder and constant-shape-404
  fallback). Banners are **live** (auto-refreshing as stats change) or a one-way frozen
  **snapshot**; pilots can regenerate, rotate the URL (the old link dies immediately),
  disable, or delete. Portraits and corp/alliance logos are fetched worker-side from CCP's
  official image server and mirrored on disk (never during a public request); the renderer is
  a deterministic Pillow compositor with a DejaVu → Noto Sans CJK font chain for
  Latin/Cyrillic/CJK glyphs (new `fonts-noto-cjk` package in the image). The feature ships
  **dark** behind the `killboard` flag plus a leadership master switch, with a Director admin
  console (render-health dashboard, per-pilot moderation, background curation with no upload
  path, quotas, refresh interval, and a freeze-or-revoke membership-loss policy) and a full
  audit trail. Fully translated across the nine locales. Known v1 limitations: no kill-streak
  component (no authoritative source), rank emblems and trophy medals are drawn glyphs rather
  than raster art, and the live→snapshot conversion is one-way.
- **Tocha's Lab (ship fitting & simulation)** — a new fitting workspace on `/lab/`
  (`Laboratório do Tocha` in Brazilian Portuguese). Build a fit from scratch or import one
  (EFT paste, a killmail, or a doctrine), apply a pilot's real skills or an All-V/untrained
  profile, and read server-computed telemetry: fitting resources (CPU/PG/calibration/slots/
  hardpoints), EHP and resists with stacking penalties, turret and drone DPS with ship/skill/
  module bonuses, capacitor stability, mobility and targeting — plus fit diagnostics, a
  skill-readiness overlay with training estimates, an estimated cost (from the market
  authority, with an "as of" stamp) and corp-stock coverage. Save immutable revisions, fork,
  compare, export EFT, and share via an unguessable, revocable public link; officers can
  promote a revision into a doctrine (deliberate and audited). The calculation engine is an
  **independent, server-side dogma evaluator** derived from documented EVE mechanics and
  sourced from the CCP SDE (no third-party engine, WASM or JS framework); the EVEShipFit
  projects were evaluated and not adopted (see `THIRD_PARTY_NOTICES.md`). New dogma reference
  tables in the SDE are loaded by `manage.py load_dogma`.
- **Cost & profitability + Supply Command board (cross-cutting)** — the supply-chain
  phases now roll up into one leadership surface and one honest margin story. A new
  **Supply Command board** (`/supply-board/`, on the /ops/ hub) composes, from persisted
  rows only, every family that needs attention — doctrine readiness & low stock, at-risk
  and overdue orders, material shortages, production bottlenecks, restock commitments,
  in-transit hauls, stock discrepancies, obsolete/slow-moving stock, and (Director-only)
  margin erosion & drifted quotes. Every row deep-links to the console that fixes it and
  names the clearing action — no metric without an action. It aggregates and links; it
  never re-implements netting, availability or margin math. One page, section-gated
  **server-side** (ISK-bearing sections render only for Directors, filtered in the view —
  not merely hidden in the template); cached and beat-warmed, so a warm view is a cache
  read. A new Director **Margin & profitability** console (`/store/margin/`) shows
  actual-vs-estimated margin per delivered order by fulfilment method, with every
  assumption inline (basis, source, as-of, fee/index defaults) and revenue shown as
  **evidence** — a payment-token-matched wallet line or an officer-recorded completed
  contract — never a fabricated actual. Fully translated in the nine locales.

  - **Fulfilment method is now recorded per delivered order.** A delivery whose
    reservations fully consume the ordered quantity auto-stamps `stock` (consumption
    evidence, not the frozen reservation promise — a fully-reserved order whose holds
    *expired* before delivery consumed nothing and is never mislabelled). Otherwise the
    claimer's DELIVERED form carries an optional method select (prefilled from evidence);
    an unattended transition leaves it blank. **Every pre-phase order stays blank and
    reports "unrecorded" — never guessed or backfilled.**
  - **The claimer's READY form gains an optional contract-id field** and the DELIVERED
    form the method select. Both optional — skipping them is legal; nothing blocks the
    advance. A **payment token** (`SO-{order}-`) now appears on order surfaces so a
    buyer's ISK transfer can be matched to the order. Purely advisory — the app plans and
    evidences; it never moves ISK.
  - **Quote drift is flagged from persisted nightly snapshots**, never a live pricing call
    in a request. An open order whose frozen basis drifts beyond a configurable percent
    **and** ISK floor is flagged (against the same frozen basis, never cross-basis); a
    missing re-estimate is "unknown", not "no drift". Drift is **informational** — no
    auto-cancel, no auto-requote, and **no frozen price is ever changed**; drift and
    settlement live in their own tables beside the order.
  - **Three inert beats** (settlement reconcile, quote drift, board sweep/digest) ship
    disabled behind their config flags — arming each is an explicit officer/Director
    decision. Alerts are all **registered governance events**: an officer supply-digest
    and quote-drift alert, plus a leadership-classified **margin-erosion** alert that can
    never reach an undesignated mass channel.
  - `_inventory_rows` was promoted to the public `inventory_rows` (the board consumes the
    exact low-stock alert composition); the private alias is kept one release.

- **Freight pipeline & in-transit inventory (supply-chain P6)** — a Jita purchase is now
  visibly *incoming*, never *available*, until it is receipted at the destination. A new
  officer **Freight Pipeline** page (`/freight/pipeline/`, also on the /ops/ hub) lets
  leadership consolidate purchase/import lines per lane into a **freight batch**, fit and
  price the leg against the corp's own rate card, assign it to the existing courier flow
  (one `CourierContract`, hauler alert fires) or to a member haul, track ETD/ETA, and
  **receipt** landed stock one deliberate, audited transaction per line — writing an
  immutable evidence row that carries the **landed unit cost** (purchase + freight share)
  and posting real stock at the destination. Between the ISK leaving the wallet and the
  goods landing, every unreceipted line quantity feeds the Material Plan (MRP) as a
  destination-pinned scheduled receipt, so the plan stops re-demanding bought goods and
  officers stop double-buying. The freight-share split, the capacity fit and the landed
  cost all reuse the existing rate-card/quote authorities — no second pricing engine.
  A second tab shows the derived (type, destination) in-transit bucket with each line's
  covering requirement, and the batch detail reports actual landed cost next to the
  forecaster's import basis. Fully translated in the nine locales.

  - **In-transit never counts as available.** P1's availability rule ("incoming supply
    never counts") is untouched; arrival flips availability solely by posting real stock
    at the destination. Import requirements covered by a batch show the batch **ETA** as
    their feasible date (`feasible_source = "in_transit"`); uncovered ones keep the flat
    import lead time.
  - **Material Plan change:** the one-click standalone import haul is replaced by
    **“Add to freight batch”** on import rows (legacy linked hauls keep working until they
    drain). A row on a freight batch shows an *in transit* chip and a batch link; the CSV
    is unchanged. A requirement can hold a freight batch **or** a BUY task, never both.
  - **Member-posted hauls now book *packaged* volume** (`create_haul`), not assembled — a
    Rifter is 2,500 m³, not 27,289 m³. The old numbers were wrong, not the new ones.
  - **Covered-destination receipts:** at an ESI-covered location a receipt is *evidence*,
    and `available()` reflects it only after the next corp-asset sync (≤6 h); the receipt
    screen says so. During that window the receipted units stay in the MRP pool as a
    bridge lot, so the requirement never reopens and no double-buy is re-offered. At an
    uncovered location the receipt is the truth immediately.
  - **Ships inert.** A new hourly batch sweep (flip to *arrived* from a verified contract
    or a completed haul, flag late batches) is **disarmed by default**
    (`FreightConfig.eta_sweep_enabled` off); manual “arrived” clicks are the v1 workflow.
    Three officer pingboard scaffolds (`logistics.batch_arrived` / `batch_late` /
    `batch_delayed`) are event-gated and idempotent. No stock is ever auto-moved — the
    receipt is always the deliberate human step.

- **Manufacturing capacity — honest build dates (supply-chain P5)** — the Material Plan
  can now refuse to promise a date the corp cannot hit, and name the bottleneck. A new
  officer **Production Capacity** board (`/industry/capacity/`) shows theoretical vs
  committed vs remaining manufacturing/reaction/science slots by activity class, committed
  load by location, per-pilot detail, an unmeasured-work aggregate and a blocked-work
  panel — every figure carrying an as-of label from the underlying sync cadence (skills
  12 h, corp jobs 3 h, pilot jobs 6 h). Capacity is derived from **opted-in pilots only**
  (an active `my_industry` grant plus home-corp membership): slots come from the Mass
  Production / Mass Reactions / Laboratory Operation skill lines, committed load is every
  in-flight ESI job deduped by `job_id` and counted once, and officers can override a
  pilot's slot count, set a weekly-output cap, mark a maintenance window or pause a pilot.
  When armed, a build row's earliest-feasible date is held to committed capacity and, when
  no honest date exists (no measured capacity, no qualified pilot, no usable blueprint, a
  reinforced or unfuelled facility), the row **refuses** (`feasible = unknown`) and carries
  a translated bottleneck chip (`slots` / `skills` / `blueprint` / `facility` / `materials`
  / `unmeasured`) on the Material Plan, the board, the CSV and the officer ping. The
  planning run never starts, pauses or delivers an in-game job — it schedules promises, not
  work. Fully translated in the nine locales.

  - **Ships inert.** `capacity_enabled` defaults **off**; with it off the feasible pass,
    the input digest, the Material Plan and the beat are byte-for-byte identical to the
    previous (unconstrained-slots) behaviour. No new background beat is added — the
    existing nightly planning run computes capacity for free.
  - **When armed**, build-row feasible dates move later (or to *unknown*) to reflect
    committed capacity; `feasible_source = "capacity"` and bottleneck chips appear; and the
    Material Plan CSV gains a **trailing `bottleneck` column** — consumers that append by
    header are safe; positional CSV consumers are the disclosed break. The armed shortfall
    beat's build lane switches from an import-lead stand-in to real feasibility lateness,
    and a new officer-only `industry.capacity_bottleneck` ping exists (idempotent per
    requirement/day/code; manual runs never ping).
  - **Consent-surface change (please read):** officers now see **named** per-pilot capacity
    (name, slot counts, used/free, next-slot-frees-at) for any pilot holding an active
    `my_industry` grant — data that was owner-only before P5. The feature's consent text is
    rewritten to say so; pilots who granted under the old wording are informed here, and
    revoking the grant removes the named rows on the next planning run.
  - **Operator note:** capacity figures are only as fresh as the syncs and only as complete
    as the opt-in set — read them as *measured* capacity, never *total corp* capacity. A
    skills snapshot shows attention-stale at 7 days while the pilot's slots stay measured
    until `capacity_skill_stale_days` (default 14), then become *unknown* (excluded), never
    zero. Both thresholds are stated on the page.

- **Suppliers, agreements and purchase orders (supply-chain P4)** — imports and
  third-party builds are now tracked commitments instead of tribal knowledge. A new
  **Procurement** area (`/procurement/`) holds supplier profiles (pilot/corp/hub, with a
  per-item catalogue: MOQ, fixed or Jita-indexed price, lead time) and their computed
  reliability. **Supply agreements** carry a term, per-cycle lines and payment terms;
  one whose estimated cycle value crosses the Director threshold needs a **second
  Director's approval** (the buyback separation-of-duties posture — the requester can
  never approve their own, superuser included), and a purchase order only claims an
  agreement's pre-authorisation when its lines genuinely fit the agreement's catalogue,
  volumes, prices and current term. **Purchase orders** run a full evidence-driven
  lifecycle — draft → submitted → approved → contract-expected → contract-available →
  accepted → partially/ fully delivered → reconciled, plus cancelled/disputed/overdue —
  where everything right of "approved" is read-only observation: the app **plans and
  evidences, it never moves ISK and never creates in-game contracts or jobs**. A matcher
  soft-links a PO to the hourly corp-contracts snapshot by bare contract id (copying the
  price, status, dates and, once, the landed item list onto the PO so the evidence
  survives the snapshot's hourly rebuild); a payment reconcile settles a PO only on an
  exact wallet-journal `context_id` == contract-id match; receipts post landed
  quantities through the one type-level stock authority or the one fit-inventory ledger,
  never a parallel counter. Raising a PO is a **fourth supply vehicle** on both the
  Material Plan and the Shipyard, counted exactly once as incoming (and it stops counting
  the moment its receipt posts) so goods already on order are never re-suggested. A
  Director **procurement board** surfaces open ISK/item obligations, due and late
  deliveries, agreement utilisation and supplier reliability, with sync-freshness shown
  honestly (a dead director token reads as a stale chip, never silently green). All four
  background jobs — contract matcher, payment reconcile, overdue sweep and reliability
  rollup — ship **disarmed** behind config flags; manual officer workflows are the v1
  path. Fully translated in the nine locales.

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
